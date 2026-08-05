#!/usr/bin/env python3
import argparse
import base64
import hashlib
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import time
import unicodedata
import urllib.parse
from pathlib import Path

import xml.etree.ElementTree as ET

try:
    import requests
except ImportError:
    requests = None

try:
    import inkdrop_state
except Exception:
    inkdrop_state = None

import inkdrop_runtime_config
import inkdrop_qbittorrent_auth
import inkdrop_download_clients

CONFIG_DIR = inkdrop_runtime_config.config_dir()
STATE_DIR = inkdrop_runtime_config.state_dir()
LOG_DIR = inkdrop_runtime_config.log_dir()
PROWLARR_CONFIG = Path(os.environ.get("INKDROP_PROWLARR_CONFIG") or CONFIG_DIR / "prowlarr" / "config.xml")
QBIT_CONFIG = Path(os.environ.get("INKDROP_QBITTORRENT_CONFIG") or CONFIG_DIR / "qbit_manage" / "config.yml")
MYLAR_CONFIG = Path(os.environ.get("INKDROP_MYLAR_CONFIG") or CONFIG_DIR / "mylar" / "config.ini")
INKDROP_STATE_DB = STATE_DIR / (inkdrop_state.STATE_DB_NAME if inkdrop_state else "inkdrop-state.sqlite3")
AUDIT_LOG = LOG_DIR / "inkdrop-acquire.log"
PENDING_IMPORTS_LOG = STATE_DIR / "pending-imports.jsonl"
QBIT_BROAD_TAG = "inkdrop"
QBIT_LEGACY_BROAD_TAG = "kavita-acquire"

# Comics are usually 7030, but manga on anime-oriented trackers such as Nyaa
# often appears under the broader 7000 literature/manga bucket.
COMIC_CATEGORIES = ["7030", "7000"]
EBOOK_CATEGORIES = ["7020"]
DEFAULT_PROTOCOL_ORDER = ["usenet", "torrent", "direct"]


def env_float(name, default):
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return float(default)


def normalize_protocol_name(value):
    text = str(value or "").strip().lower()
    aliases = {
        "nzb": "usenet",
        "nzbs": "usenet",
        "sab": "usenet",
        "sabnzbd": "usenet",
        "usenet": "usenet",
        "torrent": "torrent",
        "torrents": "torrent",
        "qbit": "torrent",
        "qbittorrent": "torrent",
        "qb": "torrent",
        "direct": "direct",
        "directdownload": "direct",
        "direct_download": "direct",
        "http": "direct",
        "https": "direct",
    }
    return aliases.get(text, text)


def normalize_protocol_order(value):
    if isinstance(value, str):
        raw = [part.strip() for part in value.split(",")]
    elif isinstance(value, (list, tuple)):
        raw = list(value)
    else:
        raw = []
    order = []
    seen = set()
    for item in raw:
        protocol = normalize_protocol_name(item)
        if protocol not in {"usenet", "torrent", "direct"} or protocol in seen:
            continue
        order.append(protocol)
        seen.add(protocol)
    for protocol in DEFAULT_PROTOCOL_ORDER:
        if protocol not in seen:
            order.append(protocol)
    return order or list(DEFAULT_PROTOCOL_ORDER)


def configured_protocol_order():
    if os.environ.get("INKDROP_PROTOCOL_ORDER"):
        return normalize_protocol_order(os.environ.get("INKDROP_PROTOCOL_ORDER"))
    if inkdrop_state is not None:
        try:
            setting = inkdrop_state.app_setting(INKDROP_STATE_DB, "automation.protocol_order") or {}
            value = setting.get("value")
            if value not in (None, "", [], {}):
                return normalize_protocol_order(value)
        except Exception:
            pass
    return list(DEFAULT_PROTOCOL_ORDER)


def protocol_rank(protocol, order=None):
    order = normalize_protocol_order(order if order is not None else configured_protocol_order())
    protocol = normalize_protocol_name(protocol)
    try:
        return order.index(protocol)
    except ValueError:
        return len(order)


PROWLARR_SEARCH_TIMEOUT_SECONDS = max(
    5.0,
    min(env_float("INKDROP_PROWLARR_SEARCH_TIMEOUT_SECONDS", 45), 45.0),
)
DEFAULT_PROWLARR_BASE_URL = str(os.environ.get("INKDROP_PROWLARR_URL") or "").strip().rstrip("/")
PROWLARR_PUBLIC_BASE_URL = str(os.environ.get("INKDROP_PROWLARR_PUBLIC_BASE_URL") or "").strip().rstrip("/")
PROWLARR_INTERNAL_BASE_URLS = tuple(
    item.strip().rstrip("/")
    for item in str(os.environ.get("INKDROP_PROWLARR_INTERNAL_BASE_URLS") or "").split(",")
    if item.strip()
)
SAB_NZB_FETCH_TIMEOUT_SECONDS = max(
    5.0,
    min(env_float("INKDROP_SAB_NZB_FETCH_TIMEOUT_SECONDS", 30), 60.0),
)
SAB_NZB_MAX_BYTES = max(
    64 * 1024,
    min(int(env_float("INKDROP_SAB_NZB_MAX_BYTES", 8 * 1024 * 1024)), 32 * 1024 * 1024),
)
SAB_NZB_MAX_SEGMENT_BYTES = 2 * 1024 * 1024 * 1024
SAB_NZB_MAX_MESSAGE_ID_CHARS = 998
QBIT_TORRENT_FETCH_TIMEOUT_SECONDS = max(
    5.0,
    min(env_float("INKDROP_QBIT_TORRENT_FETCH_TIMEOUT_SECONDS", 30), 60.0),
)
QBIT_TORRENT_MAX_BYTES = max(
    64 * 1024,
    min(int(env_float("INKDROP_QBIT_TORRENT_MAX_BYTES", 8 * 1024 * 1024)), 32 * 1024 * 1024),
)

PACK_RANGE_RE = re.compile(
    r"\b(?P<prefix>v|vol(?:ume)?\.?|ch(?:apter)?\.?|issue\s*)?\s*0*(?P<start>\d{1,4})\s*[-–]\s*(?P=prefix)?\s*0*(?P<end>\d{1,4})\b"
    r"|\(\s*0*(?P<pstart>\d{1,4})\s*[-–]\s*0*(?P<pend>\d{1,4})\s*\+?\s*\)"
    r"|\b(?P<keyword>complete|omnibus|compendium)\b",
    re.I,
)
PACK_COLLECTED_UNIT_RE = re.compile(
    r"\b(?P<unit>book|books|tpb|trade\s+paperback|hardcover|hc|v|vol(?:ume)?)\.?\s*0*(?P<number>\d{1,3})\b",
    re.I,
)
WEEKLY_COMICS_PACK_RE = re.compile(
    r"\b(?:weekly[\W_]+comics?[\W_]+pack|comics?[\W_]+weekly[\W_]+releases|(?:dc|image|indie)[\W_]+week\+?)\b"
    r"|\b\d{4}[\W_]+\d{2}[\W_]+\d{2}[\W_]+(?:dc|image|indie)?[\W_]*(?:week|weekly)\b",
    re.I,
)
NON_ENGLISH_RE = re.compile(
    r"(^|[\W_])("
    r"raw|japanese|jpn|jp|chinese|mandarin|korean|spanish|espanol|"
    r"french|german|italian|portuguese|russian|arabic|hindi|thai|vietnamese|"
    r"polish|dutch|turkish|indonesian|multi(?:[\W_]*language)?"
    r")($|[\W_])",
    re.I,
)
ENGLISH_RE = re.compile(r"(^|[\W_])(english|eng|scanlation|digital|official|viz|vlt|empire)($|[\W_])", re.I)
LANGUAGE_METADATA_KEYS = {
    "language",
    "languages",
    "language_name",
    "language_names",
    "languageName",
    "languageNames",
    "release_language",
    "releaseLanguage",
}
NORMALIZED_LANGUAGE_METADATA_KEYS = {
    re.sub(r"[^a-z0-9]+", "", key.lower()) for key in LANGUAGE_METADATA_KEYS
}
ENGLISH_LANGUAGE_TOKENS = {"en", "eng", "english"}
NON_ENGLISH_LANGUAGE_TOKENS = {
    "ar",
    "arabic",
    "chinese",
    "de",
    "deu",
    "deutsch",
    "dut",
    "dutch",
    "es",
    "espanol",
    "fra",
    "francais",
    "fre",
    "french",
    "german",
    "ger",
    "hindi",
    "id",
    "indonesian",
    "ita",
    "italian",
    "ja",
    "japanese",
    "jpn",
    "ko",
    "kor",
    "korean",
    "multi",
    "multilanguage",
    "nl",
    "pl",
    "polish",
    "por",
    "portuguese",
    "pt",
    "raw",
    "raws",
    "ru",
    "rus",
    "russian",
    "spa",
    "spanish",
    "thai",
    "tr",
    "turkish",
    "vi",
    "vietnamese",
}
DEFAULT_BLOCKED_RELEASE_TERMS = {
    "spanish",
    "espanol",
    "español",
    "german",
    "french",
    "italian",
    "portuguese",
}


def ascii_fold(value):
    text = str(value or "")
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def language_token(value):
    return re.sub(r"[^a-z0-9]+", "", ascii_fold(value).lower())


def setting_value(key, default=None):
    if inkdrop_state is None:
        return default
    try:
        setting = inkdrop_state.app_setting(INKDROP_STATE_DB, key) or {}
    except Exception:
        return default
    return setting.get("value", default)


def configured_blocked_release_terms():
    value = setting_value("quality.blocked_release_terms", None)
    if isinstance(value, str):
        terms = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, (list, tuple, set)):
        terms = [str(part).strip() for part in value if str(part or "").strip()]
    else:
        terms = []
    if not terms:
        terms = sorted(DEFAULT_BLOCKED_RELEASE_TERMS)
    return terms


def blocked_release_term_reason(result, terms=None):
    if not isinstance(result, dict):
        return ""
    values = []
    for key in ("title", "indexer", "filename", "release_title"):
        value = str(result.get(key) or "").strip()
        if value:
            values.append(value)
    text = ascii_fold(" ".join(values)).lower()
    if not text:
        return ""
    for term in terms or configured_blocked_release_terms():
        raw = str(term or "").strip()
        folded = ascii_fold(raw).lower()
        words = normalize_words(folded)
        if not words:
            continue
        pattern = r"[^a-z0-9]+".join(re.escape(word) for word in words)
        if re.search(rf"(^|[^a-z0-9]){pattern}([^a-z0-9]|$)", text, re.I):
            return f"blocked release term: {raw}"
    return ""


def collect_language_metadata(value, out=None):
    if out is None:
        out = []
    if value is None:
        return out
    if isinstance(value, str):
        if value.strip():
            out.append(value.strip())
        return out
    if isinstance(value, (list, tuple, set)):
        for item in value:
            collect_language_metadata(item, out)
        return out
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in {"name", "label", "title", "value", "code"}:
                collect_language_metadata(nested, out)
        return out
    return out


def result_language_metadata(result):
    values = []
    if not isinstance(result, dict):
        return values
    for key, value in result.items():
        normalized_key = re.sub(r"[^a-z0-9]+", "", str(key or "").lower())
        if key in LANGUAGE_METADATA_KEYS or normalized_key in NORMALIZED_LANGUAGE_METADATA_KEYS:
            collect_language_metadata(value, values)
    return list(dict.fromkeys(values))


def non_english_script_marker(text):
    labels = []
    for char in str(text or ""):
        code = ord(char)
        if (
            0x3040 <= code <= 0x30FF
            or 0x31F0 <= code <= 0x31FF
            or 0xFF66 <= code <= 0xFF9F
        ):
            labels.append("japanese script")
        elif 0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF:
            labels.append("cjk ideograph")
        elif 0xAC00 <= code <= 0xD7AF or 0x1100 <= code <= 0x11FF or 0x3130 <= code <= 0x318F:
            labels.append("korean script")
        elif 0x0400 <= code <= 0x04FF:
            labels.append("cyrillic script")
    labels = list(dict.fromkeys(labels))
    return ", ".join(labels[:3])


def category_ids(result):
    ids = set()
    for category in result.get("categories") or []:
        if isinstance(category, dict):
            if category.get("id") is not None:
                ids.add(int(category["id"]))
            for sub in category.get("subCategories") or []:
                if isinstance(sub, dict) and sub.get("id") is not None:
                    ids.add(int(sub["id"]))
        elif isinstance(category, int):
            ids.add(category)
    return sorted(ids)


def normalize_words(text):
    import re

    return re.findall(r"[a-z0-9]+", str(text or "").lower())


def title_has_series(title, series):
    words = normalize_words(series)
    if not words:
        return False
    pattern = r"[\W_]+".join(re.escape(word) for word in words)
    return bool(re.search(rf"(^|[^a-z0-9]){pattern}([^a-z0-9]|$)", str(title or ""), re.I))


def classify_english_result(result):
    title = str((result or {}).get("title") or "")
    indexer = str((result or {}).get("indexer") or "")
    text = f"{title} {indexer}"
    folded_text = ascii_fold(text)
    blocked_term = blocked_release_term_reason(result)
    if blocked_term:
        return {
            "status": "non_english",
            "score": 0,
            "reason": blocked_term,
        }
    script_marker = non_english_script_marker(text)
    if script_marker:
        return {
            "status": "non_english",
            "score": 0,
            "reason": f"title/indexer metadata contains non-English script: {script_marker}",
        }
    language_values = result_language_metadata(result or {})
    language_tokens = {language_token(value) for value in language_values}
    language_tokens.discard("")
    non_english_languages = sorted(language_tokens & NON_ENGLISH_LANGUAGE_TOKENS)
    if non_english_languages:
        return {
            "status": "non_english",
            "score": 0,
            "reason": "language metadata is not English: " + ", ".join(non_english_languages[:3]),
        }
    if NON_ENGLISH_RE.search(folded_text):
        return {
            "status": "non_english",
            "score": 0,
            "reason": "title/indexer metadata contains non-English or raw language marker",
        }
    if language_tokens & ENGLISH_LANGUAGE_TOKENS:
        return {
            "status": "confirmed_english",
            "score": 100,
            "reason": "language metadata is English",
        }
    if ENGLISH_RE.search(folded_text):
        return {
            "status": "confirmed_english",
            "score": 100,
            "reason": "title/source has English, digital, or known English release marker",
        }
    cats = set(category_ids(result or {}))
    if 7000 in cats or 7030 in cats or any(100006 <= cid <= 100008 for cid in cats):
        return {
            "status": "likely_english",
            "score": 75,
            "reason": "comic/manga/literature category with no non-English marker",
        }
    return {
        "status": "unknown",
        "score": 35,
        "reason": "no English marker and no trusted comic/manga category",
    }


def detect_pack_info(title):
    text = str(title or "")
    ranges = []
    keywords = []
    collected_units = []
    for match in PACK_RANGE_RE.finditer(text):
        if match.group("keyword"):
            keyword = match.group("keyword").lower()
            if keyword not in keywords:
                keywords.append(keyword)
            continue
        start = match.group("start") or match.group("pstart")
        end = match.group("end") or match.group("pend")
        if not start or not end:
            continue
        start_int = int(start)
        end_int = int(end)
        if start_int >= 1900 and end_int >= 1900:
            continue
        if end_int < start_int:
            start_int, end_int = end_int, start_int
        prefix = (match.group("prefix") or "").lower()
        if prefix.startswith("ch"):
            kind = "chapter"
        elif prefix.startswith("issue"):
            kind = "issue"
        elif start_int > 200:
            kind = "chapter"
        else:
            kind = "volume"
        item = {"kind": kind, "start": start_int, "end": end_int, "label": f"{kind} {start_int:03d}-{end_int:03d}"}
        if item not in ranges:
            ranges.append(item)
    for match in PACK_COLLECTED_UNIT_RE.finditer(text):
        unit = re.sub(r"\s+", " ", (match.group("unit") or "").lower()).strip()
        try:
            number = int(match.group("number"))
        except (TypeError, ValueError):
            continue
        if number <= 0:
            continue
        if unit in {"book", "books"}:
            keyword = "book"
        elif unit in {"v", "vol", "volume"}:
            keyword = "volume"
        else:
            keyword = unit
        if keyword not in keywords:
            keywords.append(keyword)
        item = {
            "kind": "collected_edition",
            "unit": keyword,
            "number": number,
            "label": f"{keyword} {number:03d}",
        }
        if item not in collected_units:
            collected_units.append(item)
    if WEEKLY_COMICS_PACK_RE.search(text):
        keyword = "weekly_pack"
        if keyword not in keywords:
            keywords.append(keyword)
    if not ranges and keywords:
        kind = "weekly_pack" if "weekly_pack" in keywords else "collection"
        ranges.append({"kind": kind, "start": None, "end": None, "label": ", ".join(keywords)})
    summary_parts = [item["label"] for item in ranges]
    if collected_units:
        summary_parts.extend(item["label"] for item in collected_units)
    return {
        "is_pack": bool(ranges or keywords),
        "ranges": ranges,
        "keywords": keywords,
        "collected_units": collected_units,
        "summary": "; ".join(summary_parts or keywords),
    }


def classify_probe_result(result, series=None, issue=None):
    title = result.get("title") or ""
    cats = set(category_ids(result))
    reasons = []
    kind = "unknown"
    safe_to_auto = False
    english = classify_english_result(result)
    reasons.append(f"English gate: {english['status']} - {english['reason']}")
    if english["status"] in {"non_english", "unknown"}:
        return {
            "classification": "hidden_language_mismatch",
            "safe_to_auto": False,
            "reasons": reasons,
            "pack_info": None,
            "english_confidence": english,
        }
    if 7000 in cats or 7030 in cats or any(cid >= 100006 and cid <= 100008 for cid in cats):
        reasons.append("book/comic/manga category")
    pack_info = detect_pack_info(title)
    if pack_info["is_pack"]:
        kind = "pack_or_range"
        reasons.append(f"pack/range requires manual approval: {pack_info.get('summary') or 'collection'}")
    if series:
        if title_has_series(title, series):
            reasons.append("series title match")
        else:
            kind = "wrong_series_or_noise"
            reasons.append("series title mismatch")
    if issue is not None:
        try:
            n = int(float(str(issue)))
            number_patterns = [
                rf"(^|[^0-9])0*{n}([^0-9]|$)",
                rf"\bv0*{n}([^0-9]|$)",
                rf"\bvol(?:ume)?\.?[\W_]+0*{n}([^0-9]|$)",
                rf"\bchapter[\W_]+0*{n}([^0-9]|$)",
                rf"\bch\.?[\W_]+0*{n}([^0-9]|$)",
                rf"\b#\s*0*{n}([^0-9]|$)",
            ]
            if any(re.search(pattern, title, re.I) for pattern in number_patterns):
                reasons.append("issue/volume/chapter number match")
                if kind == "unknown":
                    kind = "exact_candidate"
                    safe_to_auto = True
            else:
                if kind == "unknown":
                    kind = "ambiguous"
                reasons.append("issue/volume/chapter number unclear")
        except (TypeError, ValueError):
            reasons.append("issue number not numeric")
    if result.get("protocol") == "torrent" and (result.get("seeders") or 0) < 1:
        safe_to_auto = False
        reasons.append("zero-seed torrent")
    if kind == "unknown":
        kind = "candidate"
    if kind == "pack_or_range":
        safe_to_auto = False
    return {
        "classification": kind,
        "safe_to_auto": safe_to_auto,
        "reasons": reasons,
        "pack_info": pack_info if pack_info["is_pack"] else None,
        "english_confidence": english,
    }


def load_prowlarr_key():
    config = provider_config("prowlarr") or {}
    if config and not config.get("enabled", True):
        raise RuntimeError("Prowlarr provider is disabled in InkDrop settings")
    settings = config.get("settings") if isinstance(config.get("settings"), dict) else {}
    provider_key = str(settings.get("api_key") or os.environ.get("INKDROP_PROWLARR_API_KEY") or "").strip()
    if provider_key:
        return provider_key
    try:
        config_key = ET.parse(PROWLARR_CONFIG).getroot().findtext("ApiKey")
    except (OSError, ET.ParseError) as exc:
        raise RuntimeError(
            "Prowlarr API key is not set in InkDrop settings and could not be read from Prowlarr config.xml"
        ) from exc
    config_key = str(config_key or "").strip()
    if not config_key:
        raise RuntimeError("Prowlarr API key is not set in InkDrop settings or Prowlarr config.xml")
    return config_key


def provider_config(provider_id):
    if inkdrop_state is None:
        return None
    try:
        return inkdrop_state.provider_config(INKDROP_STATE_DB, provider_id)
    except Exception:
        return None


def string_list(value, fallback):
    if isinstance(value, list):
        out = [str(item).strip() for item in value if str(item).strip()]
    elif isinstance(value, str):
        out = [part.strip() for part in value.split(",") if part.strip()]
    else:
        out = []
    return out or list(fallback)


def prowlarr_search_url(base_url):
    base = str(base_url or DEFAULT_PROWLARR_BASE_URL).strip().rstrip("/")
    if not base:
        raise RuntimeError("Prowlarr URL is not configured; set INKDROP_PROWLARR_URL or the Prowlarr provider base_url setting.")
    if not base.startswith(("http://", "https://")):
        base = "http://" + base
    if base.endswith("/search"):
        return base
    return base + "/search"


def prowlarr_result_sort_key(item):
    item = item if isinstance(item, dict) else {}
    return (
        protocol_rank(item.get("protocol")),
        -(item.get("seeders") or 0),
        item.get("size") or 0,
    )


def load_prowlarr_settings(media_type):
    config = provider_config("prowlarr") or {}
    if config and not config.get("enabled", True):
        raise RuntimeError("Prowlarr provider is disabled in InkDrop settings")
    settings = config.get("settings") if isinstance(config.get("settings"), dict) else {}
    categories_key = "comic_categories" if media_type == "comics" else "ebook_categories"
    fallback_categories = COMIC_CATEGORIES if media_type == "comics" else EBOOK_CATEGORIES
    timeout = settings.get("timeout_seconds", PROWLARR_SEARCH_TIMEOUT_SECONDS)
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        timeout = PROWLARR_SEARCH_TIMEOUT_SECONDS
    return {
        "base_url": config.get("base_url") or os.environ.get("INKDROP_PROWLARR_URL") or DEFAULT_PROWLARR_BASE_URL,
        "search_url": prowlarr_search_url(config.get("base_url") or os.environ.get("INKDROP_PROWLARR_URL") or DEFAULT_PROWLARR_BASE_URL),
        "categories": string_list(settings.get(categories_key), fallback_categories),
        "timeout_seconds": max(5.0, min(timeout, 120.0)),
        "source": config.get("source") or "fallback",
    }


def load_qbit_settings():
    config = provider_config("qbittorrent") or {}
    if config and not config.get("enabled", True):
        raise RuntimeError("qBittorrent provider is disabled in InkDrop settings")
    settings = config.get("settings") if isinstance(config.get("settings"), dict) else {}
    # An API key authenticates on its own (qBittorrent >= 5.2.0), so when one is
    # set we never look for a username/password and never fall back to the
    # qbit_manage config file for them.
    api_key = str(settings.get("api_key") or os.environ.get("INKDROP_QBITTORRENT_API_KEY") or "").strip()
    qbt = {}
    if not api_key and not all(str(settings.get(key) or "").strip() for key in ("username", "password")):
        try:
            import yaml

            cfg = yaml.safe_load(QBIT_CONFIG.read_text()) or {}
            qbt = cfg.get("qbt") or {}
        except (ImportError, OSError, KeyError, TypeError, ValueError) as exc:
            if not (settings.get("username") and settings.get("password")):
                raise RuntimeError(
                    "qBittorrent credentials are not set in InkDrop settings and could not be read from qbit_manage config"
                ) from exc
    host = str(config.get("base_url") or settings.get("host") or os.environ.get("INKDROP_QBITTORRENT_URL") or qbt.get("host") or "").strip().rstrip("/")
    if not host:
        raise RuntimeError("qBittorrent URL is not configured; set INKDROP_QBITTORRENT_URL or the qBittorrent provider host setting.")
    if not host.startswith(("http://", "https://")):
        host = "http://" + host
    user = str(settings.get("username") or settings.get("user") or os.environ.get("INKDROP_QBITTORRENT_USERNAME") or qbt.get("user") or "").strip()
    password = str(settings.get("password") or settings.get("pass") or os.environ.get("INKDROP_QBITTORRENT_PASSWORD") or qbt.get("pass") or "").strip()
    if not api_key and not (user and password):
        raise RuntimeError(
            "qBittorrent needs an API key, or a username and password, in InkDrop settings or qbit_manage config"
        )
    return {
        "host": host,
        "user": user,
        "pass": password,
        "api_key": api_key,
        "comics_category": str(settings.get("comics_category") or "comics").strip() or "comics",
        "ebooks_category": str(settings.get("ebooks_category") or "ebooks").strip() or "ebooks",
        "comics_save_path": str(settings.get("comics_save_path") or "/downloads/comics").strip() or "/downloads/comics",
        "ebooks_save_path": str(settings.get("ebooks_save_path") or "/downloads/ebooks").strip() or "/downloads/ebooks",
        "source": config.get("source") or "fallback",
    }


def load_sab_settings():
    config = provider_config("sabnzbd") or {}
    if config and not config.get("enabled", True):
        raise RuntimeError("SABnzbd provider is disabled in InkDrop settings")
    settings = config.get("settings") if isinstance(config.get("settings"), dict) else {}
    fallback_host = ""
    fallback_api_key = ""
    if not str(settings.get("api_key") or "").strip():
        try:
            import configparser

            cp = configparser.ConfigParser(interpolation=None)
            cp.read(MYLAR_CONFIG)
            fallback_host = cp.get("SABnzbd", "sab_host", fallback="").rstrip("/")
            fallback_api_key = cp.get("SABnzbd", "sab_apikey", fallback="").strip()
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                "SABnzbd API key is not set in InkDrop settings and could not be read from Mylar config"
            ) from exc
    host = str(config.get("base_url") or settings.get("host") or os.environ.get("INKDROP_SABNZBD_URL") or fallback_host or "").strip().rstrip("/")
    if not host:
        raise RuntimeError("SABnzbd URL is not configured; set INKDROP_SABNZBD_URL or the SABnzbd provider host setting.")
    if not host.startswith(("http://", "https://")):
        host = "http://" + host
    api_key = str(settings.get("api_key") or os.environ.get("INKDROP_SABNZBD_API_KEY") or fallback_api_key or "").strip()
    if not api_key:
        raise RuntimeError("SABnzbd API key is not set in InkDrop settings or Mylar config")
    return {
        "host": host,
        "api_key": api_key,
        "comics_category": str(settings.get("comics_category") or "comics").strip() or "comics",
        "failure_categories": string_list(settings.get("failure_categories"), ["comics", "manga", "mylar", "kapowarr"]),
        "delegated_url_allowed_hosts": [
            item.lower().rstrip(".")
            for item in string_list(
                settings.get("delegated_url_allowed_hosts")
                or os.environ.get("INKDROP_SABNZBD_DELEGATED_URL_ALLOWED_HOSTS"),
                [],
            )
        ],
        "source": config.get("source") or "fallback",
    }


def load_transmission_settings():
    config = provider_config("transmission") or {}
    if config and not config.get("enabled", True):
        raise RuntimeError("Transmission provider is disabled in InkDrop settings")
    settings = config.get("settings") if isinstance(config.get("settings"), dict) else {}
    raw = dict(settings)
    if config.get("base_url") and not raw.get("base_url"):
        raw["base_url"] = config.get("base_url")
    raw.setdefault("source", config.get("source") or "fallback")
    return inkdrop_download_clients.validate_transmission_settings(raw)


def load_deluge_settings():
    config = provider_config("deluge") or {}
    if config and not config.get("enabled", True):
        raise RuntimeError("Deluge provider is disabled in InkDrop settings")
    settings = config.get("settings") if isinstance(config.get("settings"), dict) else {}
    raw = dict(settings)
    if config.get("base_url") and not raw.get("base_url"):
        raw["base_url"] = config.get("base_url")
    raw.setdefault("source", config.get("source") or "fallback")
    return inkdrop_download_clients.validate_deluge_settings(raw)


def load_nzbget_settings():
    config = provider_config("nzbget") or {}
    if config and not config.get("enabled", True):
        raise RuntimeError("NZBGet provider is disabled in InkDrop settings")
    settings = config.get("settings") if isinstance(config.get("settings"), dict) else {}
    raw = dict(settings)
    if config.get("base_url") and not raw.get("base_url"):
        raw["base_url"] = config.get("base_url")
    raw.setdefault("source", config.get("source") or "fallback")
    return inkdrop_download_clients.validate_nzbget_settings(raw)


def load_utorrent_settings():
    config = provider_config("utorrent") or {}
    if config and not config.get("enabled", True):
        raise RuntimeError("uTorrent provider is disabled in InkDrop settings")
    raw = dict(config.get("settings") or {})
    raw.setdefault("base_url", config.get("base_url"))
    raw.setdefault("source", config.get("source") or "fallback")
    return inkdrop_download_clients.validate_utorrent_settings(raw)


def load_rtorrent_settings():
    config = provider_config("rtorrent") or {}
    if config and not config.get("enabled", True):
        raise RuntimeError("rTorrent provider is disabled in InkDrop settings")
    raw = dict(config.get("settings") or {})
    raw.setdefault("base_url", config.get("base_url"))
    raw.setdefault("source", config.get("source") or "fallback")
    return inkdrop_download_clients.validate_rtorrent_settings(raw)


def audit(event, payload):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    redacted = dict(payload)
    for key in list(redacted):
        if "key" in key.lower() or "pass" in key.lower() or "url" in key.lower():
            redacted[key] = "<redacted>"
    with AUDIT_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": event, **redacted}, sort_keys=True) + "\n")


def require_requests():
    if requests is None:
        raise RuntimeError("Python requests package is required for live Prowlarr/SAB/qBittorrent operations")
    return requests


def result_download_url_hash(result):
    if not isinstance(result, dict):
        return None
    for key in ("downloadUrlHash", "download_url_hash", "url_hash"):
        if result.get(key):
            return str(result.get(key))
    url = result.get("downloadUrl") or result.get("download_url") or result.get("url")
    if not url:
        return None
    return hashlib.sha256(str(url).encode("utf-8")).hexdigest()


def first_scalar(value):
    if isinstance(value, (list, tuple)):
        for item in value:
            if item not in (None, ""):
                return item
        return None
    return value if value not in (None, "") else None


def pending_artifact_identity(protocol, download_client, client_id, download_url_hash):
    """A durable identity for the one physical transfer this record describes,
    independent of the title/query text used to find it. client_id is the
    download client's own handle for the transfer (a torrent hash or SAB
    nzo_id) -- already unique per physical download -- namespaced by
    protocol/client so two clients can never collide on a numeric id.
    download_url_hash is kept as a fallback for outcomes that don't surface a
    client id. Returns "" when neither is available (nothing durable to key
    on), which callers must treat as "no stable identity", not a match key.
    """
    handle = str(client_id or "").strip()
    if not handle:
        return ""
    basis = "|".join((str(protocol or "").strip().lower(), str(download_client or "").strip().lower(), handle))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def pending_target_identity(target):
    """Stable series/issue/queue/Wanted identity for the row this download was
    sent for, when the caller has one in scope. Tolerant of the different key
    spellings used across callers (queue rows, manual-review items, watch
    entries) -- absent fields are simply omitted, never guessed."""
    target = target if isinstance(target, dict) else {}
    identity = {}
    for out_key, in_keys in (
        ("target_queue_id", ("queue_id", "queueId", "id")),
        ("target_wanted_id", ("wanted_id", "wantedId")),
        ("target_series_id", ("series_id", "seriesId", "native_series_id", "inkdrop_series_id")),
        ("target_issue_id", ("issue_id", "issueId", "native_issue_id")),
    ):
        for in_key in in_keys:
            value = str(target.get(in_key) or "").strip()
            if value:
                identity[out_key] = value
                break
    return identity


def record_pending_import(query, media_type, chosen, outcome, target=None):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    outcome = outcome if isinstance(outcome, dict) else {}
    nzo_id = first_scalar(outcome.get("nzo_id") or outcome.get("nzo_ids"))
    client_hash = first_scalar(outcome.get("client_hash") or outcome.get("hash") or outcome.get("hashes"))
    client_id = first_scalar(outcome.get("client_id") or client_hash or nzo_id)
    download_url_hash = result_download_url_hash(chosen)
    protocol = chosen.get("protocol")
    download_client = outcome.get("download_client")
    record = {
        "event": "pending_import",
        "created_at": __import__("time").time(),
        "query": query,
        "type": media_type,
        "title": chosen.get("title"),
        "indexer": chosen.get("indexer"),
        "indexerId": chosen.get("indexerId"),
        "protocol": protocol,
        "download_client": download_client,
        "client_id": client_id,
        "client_hash": client_hash,
        "nzo_id": nzo_id,
        "download_url_hash": download_url_hash,
        "size": chosen.get("size"),
        "category": outcome.get("category"),
        "save_path": outcome.get("save_path"),
        "status": "sent",
        "pending_artifact_id": pending_artifact_identity(protocol, download_client, client_id, download_url_hash),
    }
    record.update(pending_target_identity(target))
    with PENDING_IMPORTS_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    return record


def prowlarr_search(query, media_type, indexer_ids=None, limit=10, timeout_seconds=None):
    http = require_requests()
    api_key = load_prowlarr_key()
    settings = load_prowlarr_settings(media_type)
    timeout = settings["timeout_seconds"]
    if timeout_seconds is not None:
        try:
            timeout = max(1.0, min(float(timeout_seconds), timeout))
        except (TypeError, ValueError):
            timeout = settings["timeout_seconds"]
    params = {"query": query, "categories": settings["categories"]}
    if indexer_ids:
        params["indexerIds"] = ",".join(str(x) for x in indexer_ids)
    response = http.get(
        settings["search_url"],
        params=params,
        headers={"X-Api-Key": api_key},
        timeout=timeout,
    )
    response.raise_for_status()
    results = response.json()
    results = sorted(
        results,
        key=prowlarr_result_sort_key,
    )
    return results[:limit]


def qbit_handoff_tag(title, download_url=None, unique_tag=None):
    if unique_tag:
        raw = str(unique_tag)
    else:
        seed = f"{title or 'inkdrop'}|{download_url or ''}"
        raw = "inkdrop-" + hashlib.sha1(seed.encode("utf-8", "ignore")).hexdigest()[:16]
    tag = re.sub(r"[^A-Za-z0-9_.:-]+", "-", raw).strip("-")
    return (tag or "inkdrop-handoff")[:64]


def qbit_item_tags(item):
    value = (item or {}).get("tags")
    if isinstance(value, str):
        return {part.strip() for part in value.split(",") if part.strip()}
    if isinstance(value, (list, tuple, set)):
        return {str(part).strip() for part in value if str(part).strip()}
    return set()


def qbit_has_ownership_tag(value):
    item = value if isinstance(value, dict) else {"tags": value}
    tags = {tag.lower() for tag in qbit_item_tags(item)}
    return bool(tags & {QBIT_BROAD_TAG, QBIT_LEGACY_BROAD_TAG})


def qbit_normalize_title(value):
    return re.sub(r"[^a-z0-9]+", " ", ascii_fold(value).lower()).strip()


def qbit_torrent_hash(value):
    """A torrent's own identity, or nothing. Never a display name."""
    text = str(value or "").strip().lower()
    return text if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", text) else ""


def qbit_visible_torrents(session, host, *, tag=None, title=None, category=None, info_hash=None):
    candidates = []
    if tag:
        response = session.get(host + "/api/v2/torrents/info", params={"tag": tag}, timeout=10)
        response.raise_for_status()
        for item in response.json() or []:
            if tag in qbit_item_tags(item):
                candidates.append(item)
    if candidates:
        return candidates
    params = {}
    if category:
        params["category"] = category
    response = session.get(host + "/api/v2/torrents/info", params=params, timeout=10)
    response.raise_for_status()
    items = response.json() or []
    if tag:
        candidates.extend(item for item in items if tag in qbit_item_tags(item))
    # The infohash is what qBittorrent itself keys on, and when we know it we
    # have no business guessing. Deluge, uTorrent and rTorrent already claim
    # this way; qBittorrent and Transmission were the two that never adopted it.
    wanted_hash = qbit_torrent_hash(info_hash)
    if wanted_hash:
        candidates.extend(item for item in items if qbit_torrent_hash(item.get("hash")) == wanted_hash)
        unique = {}
        for item in candidates:
            unique[str(item.get("hash") or item.get("name") or id(item))] = item
        return list(unique.values())
    # No infohash to go on. A display name is the uploader's choice, not an
    # identity: two unrelated releases share one routinely, and claiming on it
    # made InkDrop adopt somebody else's torrent and import its bytes against
    # this issue. Falling back to it is worse than adding a duplicate, so only
    # an exact name match inside our own category counts, and only when we were
    # never told what to look for.
    title_key = qbit_normalize_title(title)
    if title_key:
        for item in items:
            name_key = qbit_normalize_title(item.get("name"))
            if name_key and name_key == title_key:
                candidates.append(item)
    unique = {}
    for item in candidates:
        key = item.get("hash") or item.get("name") or json.dumps(item, sort_keys=True)
        unique[str(key)] = item
    return list(unique.values())


def qbit_wait_for_visible_torrent(session, host, *, tag=None, title=None, category=None, timeout_seconds=20):
    deadline = time.time() + max(1.0, float(timeout_seconds or 20))
    last_items = []
    while True:
        items = qbit_visible_torrents(session, host, tag=tag, title=title, category=category)
        if items:
            return items[0]
        last_items = items
        if time.time() >= deadline:
            return None
        time.sleep(1.0)


def qbit_existing_result(torrent, *, category, save_path, handoff_tag, settings_source):
    torrent = torrent if isinstance(torrent, dict) else {}
    torrent_hash = str(torrent.get("hash") or "").strip()
    out = {
        "ok": bool(torrent_hash),
        "added": False,
        "existing": True,
        "download_client": "qBittorrent",
        "protocol": "torrent",
        "category": category,
        "save_path": save_path,
        "handoff_tag": handoff_tag,
        "settings_source": settings_source,
        "client_name": torrent.get("name"),
        "client_state": torrent.get("state"),
        "progress": torrent.get("progress"),
        "size_bytes": torrent.get("size"),
    }
    if torrent_hash:
        out.update(
            {
                "client_id": torrent_hash,
                "client_external_id": torrent_hash,
                "torrent_hash": torrent_hash,
                "hash": torrent_hash,
            }
        )
    return {key: value for key, value in out.items() if value not in (None, "")}


def prowlarr_download_url_for_client(download_url):
    text = str(download_url or "")
    mapped = text
    if PROWLARR_PUBLIC_BASE_URL:
        for internal in PROWLARR_INTERNAL_BASE_URLS:
            if text.startswith(internal + "/"):
                mapped = PROWLARR_PUBLIC_BASE_URL + text[len(internal):]
                break
    try:
        validated = prowlarr_nzb_fetch_url(mapped)
        parsed = urllib.parse.urlsplit(mapped)
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if validated and not any(str(key).lower() in {"apikey", "api_key", "api-key"} for key, _ in query):
            api_key = load_prowlarr_key()
            query.append(("apikey", api_key))
            return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), ""))
    except RuntimeError:
        # The normal client/fetch path reports configuration failures with its
        # existing bounded error contract; never guess an authority here.
        pass
    return mapped


def qbit_add(
    download_url,
    title,
    media_type,
    dry_run=False,
    unique_tag=None,
    verify=True,
    verify_timeout_seconds=None,
    settings_override=None,
    expected_url_hash=None,
    require_prowlarr_fetch=False,
    expected_torrent_identity=None,
):
    http = require_requests()
    qbit = dict(settings_override) if isinstance(settings_override, dict) else load_qbit_settings()
    category = (qbit.get(f"{media_type}_category") or qbit["comics_category"]) if media_type in {"comics", "manga"} else qbit["ebooks_category"]
    save_path = (qbit.get(f"{media_type}_save_path") or qbit["comics_save_path"]) if media_type in {"comics", "manga"} else qbit["ebooks_save_path"]
    original_download_url = str(download_url or "")
    client_download_url = prowlarr_download_url_for_client(original_download_url)
    handoff_tag = qbit_handoff_tag(title, client_download_url, unique_tag=unique_tag)
    # Keep the legacy broad tag for one rollback window. The stable handoff
    # token remains the dedupe identity, so adding this alias cannot create a
    # second torrent.
    tags = ",".join([QBIT_BROAD_TAG, QBIT_LEGACY_BROAD_TAG, handoff_tag])
    if dry_run:
        return {
            "dry_run": True,
            "download_client": "qBittorrent",
            "protocol": "torrent",
            "category": category,
            "save_path": save_path,
            "handoff_tag": handoff_tag,
            "settings_source": qbit.get("source"),
        }

    if require_prowlarr_fetch and not prowlarr_torrent_fetch_url(
        original_download_url,
        expected_url_hash,
    ):
        raise RuntimeError("Prowlarr torrent URL authority was refused")
    if require_prowlarr_fetch:
        identity = expected_torrent_identity if isinstance(expected_torrent_identity, dict) else {}
        if not any(identity.get(key) for key in ("info_hash", "pack_member", "title", "series_title")):
            raise RuntimeError("Prowlarr torrent payload identity is missing")

    session = http.Session()
    inkdrop_qbittorrent_auth.authenticate_settings(session, qbit, timeout=20)

    existing = qbit_visible_torrents(
        session,
        qbit["host"],
        tag=handoff_tag,
        title=title,
        category=category,
        info_hash=(expected_torrent_identity or {}).get("info_hash")
        if isinstance(expected_torrent_identity, dict)
        else None,
    )
    if existing:
        return qbit_existing_result(
            existing[0],
            category=category,
            save_path=save_path,
            handoff_tag=handoff_tag,
            settings_source=qbit.get("source"),
        )

    torrent_payload = fetch_prowlarr_torrent(
        http,
        original_download_url,
        expected_url_hash,
        expected_torrent_identity=expected_torrent_identity,
    )
    if require_prowlarr_fetch and torrent_payload is None:
        raise RuntimeError("Prowlarr torrent URL authority was refused")
    add_data = {
        "category": category,
        "savepath": save_path,
        "tags": tags,
        "paused": "false",
    }
    add_kwargs = {"data": add_data, "timeout": 30}
    handoff_mode = "torrent_upload" if torrent_payload is not None else "url"
    if torrent_payload is None:
        add_data["urls"] = client_download_url
    else:
        filename = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(title or handoff_tag)).strip("-.") or handoff_tag
        filename = re.sub(r"(?i)\.torrent$", "", filename)[:172].rstrip("-.") + ".torrent"
        add_kwargs["files"] = {
            "torrents": (filename, torrent_payload, "application/x-bittorrent")
        }
    add = session.post(qbit["host"] + "/api/v2/torrents/add", **add_kwargs)
    add.raise_for_status()
    body = add.text.strip()
    if body.lower() not in {"ok.", "ok", ""}:
        try:
            data = add.json()
        except ValueError:
            raise RuntimeError(f"qBittorrent add returned: {body[:120]}")
        if data.get("failure_count", 0) != 0:
            raise RuntimeError(f"qBittorrent add returned failure: {data}")
    verify_timeout = verify_timeout_seconds
    if verify_timeout is None:
        verify_timeout = env_float("INKDROP_QBIT_ADD_VERIFY_SECONDS", 20)
    torrent = None
    if verify:
        torrent = qbit_wait_for_visible_torrent(
            session,
            qbit["host"],
            tag=handoff_tag,
            title=title,
            category=category,
            timeout_seconds=verify_timeout,
        )
        if not torrent:
            audit(
                "qbit_add_not_visible",
                {
                    "title": title,
                    "category": category,
                    "save_path": save_path,
                    "handoff_tag": handoff_tag,
                    "settings_source": qbit.get("source"),
                    "verify_timeout_seconds": verify_timeout,
                    "handoff_mode": handoff_mode,
                    "url_hash": hashlib.sha256(original_download_url.encode("utf-8")).hexdigest(),
                },
            )
            # qBittorrent already returned "Ok." for /torrents/add before this
            # check ever ran -- the add itself is not in doubt, only whether the
            # listing caught up in time. Calling that "failed_download" let the
            # coordinator retire this handoff and hand the candidate to a new
            # instance/search, which re-added a torrent that was already there.
            # "enqueue_response_ambiguous" is the status the SLSKD lane already
            # uses for "accepted but no authoritative confirmation yet" -- it
            # skips the same retire-and-replace path instead of duplicating it.
            return {
                "ok": False,
                "added": False,
                "status": "enqueue_response_ambiguous",
                "reason": "qbittorrent_add_not_visible",
                "download_client": "qBittorrent",
                "protocol": "torrent",
                "category": category,
                "save_path": save_path,
                "handoff_tag": handoff_tag,
                "settings_source": qbit.get("source"),
                "verify_timeout_seconds": verify_timeout,
                "handoff_mode": handoff_mode,
            }
    audit(
        "qbit_add",
        {
            "title": title,
            "category": category,
            "save_path": save_path,
            "handoff_tag": handoff_tag,
            "settings_source": qbit.get("source"),
            "torrent_hash": (torrent or {}).get("hash"),
            "handoff_mode": handoff_mode,
        },
    )
    out = {
        "ok": True,
        "added": True,
        "download_client": "qBittorrent",
        "protocol": "torrent",
        "category": category,
        "save_path": save_path,
        "handoff_tag": handoff_tag,
        "settings_source": qbit.get("source"),
        "handoff_mode": handoff_mode,
    }
    if torrent:
        torrent_hash = torrent.get("hash")
        if torrent_hash:
            out["client_id"] = torrent_hash
            out["client_external_id"] = torrent_hash
            out["torrent_hash"] = torrent_hash
            out["hash"] = torrent_hash
        out["client_name"] = torrent.get("name")
        out["client_state"] = torrent.get("state")
        out["progress"] = torrent.get("progress")
        out["size_bytes"] = torrent.get("size")
    return out


def sab_handoff_key(title, download_url=None, unique_tag=None):
    if unique_tag:
        seed = str(unique_tag)
    else:
        seed = f"{title or 'inkdrop'}|{download_url or ''}"
    return "inkdrop-" + hashlib.sha1(seed.encode("utf-8", "ignore")).hexdigest()[:24]


def sab_result_slots(payload, section):
    payload = payload if isinstance(payload, dict) else {}
    section_payload = payload.get(section)
    if not isinstance(section_payload, dict):
        return []
    return [row for row in section_payload.get("slots") or [] if isinstance(row, dict)]


def sab_find_existing_job(http, sab, handoff_key, *, history_limit=200):
    common = {"output": "json", "apikey": sab["api_key"]}
    requests_to_make = (
        ("queue", {}),
        ("history", {"limit": max(1, min(int(history_limit or 200), 500))}),
    )
    for section, extra in requests_to_make:
        response = http.get(
            sab["host"] + "/api",
            params={**common, "mode": section, **extra},
            timeout=20,
        )
        response.raise_for_status()
        for row in sab_result_slots(response.json(), section):
            duplicate_key = str(row.get("duplicate_key") or row.get("dupekey") or "").strip()
            if duplicate_key != handoff_key:
                continue
            status = str(row.get("status") or "").strip().lower()
            if status in {"failed", "deleted"}:
                continue
            return {**row, "_inkdrop_section": section}
    return None


def sab_existing_result(job, *, category, handoff_key, settings_source):
    job = job if isinstance(job, dict) else {}
    nzo_id = str(job.get("nzo_id") or "").strip()
    out = {
        "ok": bool(nzo_id),
        "status": True if nzo_id else False,
        "added": False,
        "existing": True,
        "download_client": "SABnzbd",
        "protocol": "usenet",
        "category": category,
        "handoff_key": handoff_key,
        "settings_source": settings_source,
        "nzo_id": nzo_id,
        "client_id": nzo_id,
        "client_external_id": nzo_id,
        "client_name": job.get("name") or job.get("nzb_name"),
        "client_state": job.get("status"),
        "client_scope": job.get("_inkdrop_section"),
    }
    return {key: value for key, value in out.items() if value not in (None, "")}


def _url_origin(value):
    parsed = urllib.parse.urlsplit(str(value or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return ""
    default_port = 443 if parsed.scheme == "https" else 80
    try:
        port = parsed.port or default_port
    except ValueError:
        return ""
    return f"{parsed.scheme}://{parsed.hostname.lower()}:{port}"


def _legacy_prowlarr_download_endpoint(path):
    return bool(re.fullmatch(r"/\d+/download/?", str(path or ""), re.I))


def _prowlarr_download_endpoint(path):
    path = str(path or "")
    if (
        not path
        or "%" in path
        or "\\" in path
        or any(ord(char) < 32 or ord(char) == 127 for char in path)
        or any(segment in {".", ".."} for segment in path.split("/"))
    ):
        return False
    return bool(
        re.match(r"^/api/v1/indexer/[^/]+/download(?:/|$)", path, re.I)
        or re.match(r"^/download(?:/|$)", path, re.I)
        or _legacy_prowlarr_download_endpoint(path)
    )


def _url_base_path(value):
    parsed = urllib.parse.urlsplit(str(value or "").strip())
    path = "/" + str(parsed.path or "").strip("/") if str(parsed.path or "").strip("/") else ""
    return path


def _prowlarr_service_base(value):
    """Normalize a configured Prowlarr search URL to its service/API base."""

    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    parsed = urllib.parse.urlsplit(text)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return ""
    try:
        parsed.port
    except ValueError:
        return ""
    path = str(parsed.path or "").rstrip("/")
    if path.lower().endswith("/search"):
        path = path[:-len("/search")]
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _loopback_hostname(value):
    hostname = str(value or "").strip().lower().rstrip(".")
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(hostname)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return address.is_loopback
    except ValueError:
        return False


def _canonical_ip_address(value):
    try:
        address = ipaddress.ip_address(str(value or "").split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped
    return address


def _globally_routable_address(value):
    address = _canonical_ip_address(value)
    return bool(
        address
        and address.is_global
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_reserved
        and not address.is_multicast
        and not address.is_unspecified
    )


def _sab_url_delegation_allowed(download_url, sab_host, allowed_hosts=None):
    unmodified = str(download_url or "")
    if any(ord(char) < 32 or ord(char) == 127 for char in unmodified):
        return False
    parsed = urllib.parse.urlsplit(unmodified.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        return False
    try:
        parsed.port
    except ValueError:
        return False
    hostname = str(parsed.hostname or "").strip().lower().rstrip(".")
    if not hostname or "%" in hostname:
        return False
    allowlist = {
        str(value or "").strip().lower().rstrip(".")
        for value in (allowed_hosts or [])
        if str(value or "").strip()
    }
    sab = urllib.parse.urlsplit(str(sab_host or "").strip())
    if (
        sab.scheme in {"http", "https"}
        and sab.hostname
        and not sab.username
        and not sab.password
        and _loopback_hostname(sab.hostname)
    ):
        return True
    literal = _canonical_ip_address(hostname)
    if literal:
        return _globally_routable_address(literal) or hostname in allowlist
    return hostname in allowlist


def _endpoint_relative_to_base(path, base_path):
    path = str(path or "")
    if not path.startswith("/") or path.startswith("//"):
        return ""
    base_path = str(base_path or "").rstrip("/")
    if base_path:
        if path == base_path:
            suffix = "/"
        elif path.startswith(base_path + "/"):
            suffix = path[len(base_path):]
        else:
            return ""
    else:
        suffix = path
    candidates = [suffix]
    if base_path.lower().endswith("/api/v1") and suffix.lower().startswith("/indexer/"):
        candidates.insert(0, "/api/v1" + suffix)
    return next((candidate for candidate in candidates if _prowlarr_download_endpoint(candidate)), "")


def prowlarr_nzb_fetch_url(download_url):
    """Return a locally reachable URL only for a recognized Prowlarr download endpoint."""

    unmodified_download_url = str(download_url or "")
    if any(ord(char) < 32 or ord(char) == 127 for char in unmodified_download_url):
        return ""
    raw_download_url = unmodified_download_url.strip()
    original = urllib.parse.urlsplit(raw_download_url)
    if original.scheme not in {"http", "https"} or not original.hostname or original.username or original.password:
        return ""
    path = original.path or "/"
    loopback = _loopback_hostname(original.hostname)
    try:
        settings = load_prowlarr_settings("comics")
    except RuntimeError:
        if loopback:
            raise RuntimeError("Prowlarr context is unavailable for NZB fetch")
        return ""
    configured_base = _prowlarr_service_base(settings.get("base_url") or settings.get("search_url"))
    if not configured_base:
        if loopback:
            raise RuntimeError("Prowlarr context is unavailable for NZB fetch")
        return ""
    known_bases = [
        value
        for value in (
            configured_base,
            _prowlarr_service_base(PROWLARR_PUBLIC_BASE_URL),
            *(_prowlarr_service_base(value) for value in PROWLARR_INTERNAL_BASE_URLS),
        )
        if value
    ]
    original_origin = _url_origin(download_url)
    configured = urllib.parse.urlsplit(configured_base)
    try:
        expected_port = configured.port or (443 if configured.scheme == "https" else 80)
        original_port = original.port or (443 if original.scheme == "https" else 80)
    except ValueError:
        return ""
    endpoint = ""
    if loopback and original_port in {9696, expected_port}:
        base_paths = sorted({_url_base_path(value) for value in known_bases if value}, key=len, reverse=True)
        for base_path in [*base_paths, ""]:
            endpoint = _endpoint_relative_to_base(path, base_path)
            if endpoint:
                break
    else:
        for base in known_bases:
            if base and original_origin == _url_origin(base):
                endpoint = _endpoint_relative_to_base(path, _url_base_path(base))
                if endpoint:
                    break
    if not endpoint:
        return ""
    configured_path = _url_base_path(configured_base)
    if _legacy_prowlarr_download_endpoint(endpoint):
        target_path = endpoint
    elif configured_path and (endpoint == configured_path or endpoint.startswith(configured_path + "/")):
        target_path = endpoint
    elif configured_path.lower().endswith("/api/v1") and endpoint.lower().startswith("/api/v1/"):
        target_path = configured_path[:-len("/api/v1")] + endpoint
    else:
        target_path = (configured_path + endpoint) or "/"
    return urllib.parse.urlunsplit((configured.scheme, configured.netloc, target_path, original.query, ""))


def _nzb_deadline_check(deadline, clock):
    if deadline is not None and clock() >= deadline:
        raise RuntimeError("Prowlarr NZB fetch exceeded the total deadline")


def _bounded_nzb_payload(response, max_bytes=SAB_NZB_MAX_BYTES, *, deadline=None, clock=time.monotonic):
    _nzb_deadline_check(deadline, clock)
    try:
        declared = int((getattr(response, "headers", {}) or {}).get("Content-Length") or 0)
    except (TypeError, ValueError):
        declared = 0
    if declared > max_bytes:
        raise RuntimeError("Prowlarr NZB payload exceeded the configured size limit")
    body = bytearray()
    if callable(getattr(response, "iter_content", None)):
        chunks = response.iter_content(chunk_size=64 * 1024)
    else:
        chunks = [getattr(response, "content", b"")]
    for chunk in chunks:
        _nzb_deadline_check(deadline, clock)
        if not chunk:
            continue
        body.extend(chunk)
        if len(body) > max_bytes:
            raise RuntimeError("Prowlarr NZB payload exceeded the configured size limit")
    payload = bytes(body)
    _nzb_deadline_check(deadline, clock)
    try:
        root = ET.fromstring(payload)
    except (ET.ParseError, ValueError) as exc:
        raise RuntimeError("Prowlarr returned a malformed NZB payload") from exc
    _nzb_deadline_check(deadline, clock)
    files = [node for node in root if node.tag.rsplit("}", 1)[-1].lower() == "file"]
    if root.tag.rsplit("}", 1)[-1].lower() != "nzb" or not files:
        raise RuntimeError("Prowlarr returned a malformed NZB payload")
    for file_node in files:
        segment_groups = [
            node for node in file_node
            if node.tag.rsplit("}", 1)[-1].lower() == "segments"
        ]
        segments = [
            node for group in segment_groups for node in group
            if node.tag.rsplit("}", 1)[-1].lower() == "segment"
        ]
        usable = bool(segments)
        for segment in segments:
            try:
                raw_message_id = str(segment.text or "")
                message_id = raw_message_id.strip()
                segment_bytes = int(segment.attrib.get("bytes") or 0)
                usable = bool(
                    usable
                    and message_id
                    and raw_message_id == message_id
                    and len(message_id) <= SAB_NZB_MAX_MESSAGE_ID_CHARS
                    and re.fullmatch(r"[^\s<>@]+@[^\s<>@]+", message_id)
                    and int(segment.attrib.get("number") or 0) > 0
                    and 0 < segment_bytes <= SAB_NZB_MAX_SEGMENT_BYTES
                )
            except (TypeError, ValueError):
                usable = False
        if not usable:
            raise RuntimeError("Prowlarr returned a semantically unusable NZB payload")
    _nzb_deadline_check(deadline, clock)
    return payload


def _bounded_response_bytes(response, max_bytes, *, deadline=None, clock=time.monotonic, label="download"):
    def deadline_check():
        if deadline is not None and clock() >= deadline:
            raise RuntimeError(f"Prowlarr {label} fetch exceeded the total deadline")

    deadline_check()
    try:
        declared = int((getattr(response, "headers", {}) or {}).get("Content-Length") or 0)
    except (TypeError, ValueError):
        declared = 0
    if declared > max_bytes:
        raise RuntimeError(f"Prowlarr {label} payload exceeded the configured size limit")
    body = bytearray()
    chunks = (
        response.iter_content(chunk_size=64 * 1024)
        if callable(getattr(response, "iter_content", None))
        else [getattr(response, "content", b"")]
    )
    for chunk in chunks:
        deadline_check()
        if not chunk:
            continue
        body.extend(chunk)
        if len(body) > max_bytes:
            raise RuntimeError(f"Prowlarr {label} payload exceeded the configured size limit")
    deadline_check()
    return bytes(body)


def _decode_torrent_payload(payload):
    payload = bytes(payload or b"")
    nodes = 0
    info_span = None

    def parse(index, depth=0):
        nonlocal nodes, info_span
        nodes += 1
        if depth > 32 or nodes > 100_000 or index >= len(payload):
            raise ValueError("invalid bencode")
        token = payload[index:index + 1]
        if token == b"i":
            end = payload.find(b"e", index + 1)
            raw = payload[index + 1:end] if end >= 0 else b""
            if end < 0 or not re.fullmatch(rb"(?:0|-?[1-9][0-9]*)", raw):
                raise ValueError("invalid bencode integer")
            return int(raw), end + 1
        if token in {b"l", b"d"}:
            collection = [] if token == b"l" else {}
            index += 1
            while index < len(payload) and payload[index:index + 1] != b"e":
                if token == b"d":
                    key, index = parse(index, depth + 1)
                    if not isinstance(key, bytes):
                        raise ValueError("invalid bencode key")
                    value_start = index
                    value, index = parse(index, depth + 1)
                    if depth == 0 and key == b"info":
                        info_span = (value_start, index)
                    collection[key] = value
                else:
                    value, index = parse(index, depth + 1)
                    collection.append(value)
            if index >= len(payload):
                raise ValueError("unterminated bencode collection")
            return collection, index + 1
        colon = payload.find(b":", index, min(len(payload), index + 24))
        raw_length = payload[index:colon] if colon >= 0 else b""
        if colon < 0 or not re.fullmatch(rb"(?:0|[1-9][0-9]*)", raw_length):
            raise ValueError("invalid bencode string")
        length = int(raw_length)
        start = colon + 1
        end = start + length
        if end > len(payload):
            raise ValueError("truncated bencode string")
        return payload[start:end], end

    root, end = parse(0)
    if end != len(payload) or not isinstance(root, dict):
        raise ValueError("invalid torrent root")
    info = root.get(b"info")
    if not isinstance(info, dict):
        raise ValueError("torrent info dictionary missing")
    name = info.get(b"name.utf-8") or info.get(b"name")
    piece_length = info.get(b"piece length")
    v1_pieces = info.get(b"pieces")
    v1_content = (
        isinstance(info.get(b"length"), int)
        and info.get(b"length") > 0
    ) or isinstance(info.get(b"files"), list)
    v1 = (
        isinstance(v1_pieces, bytes)
        and len(v1_pieces) > 0
        and len(v1_pieces) % 20 == 0
        and v1_content
    )
    v2 = info.get(b"meta version") == 2 and bool(info.get(b"file tree"))
    if (
        not isinstance(name, bytes)
        or not name
        or len(name) > 4096
        or not isinstance(piece_length, int)
        or piece_length <= 0
        or not (v1 or v2)
    ):
        raise ValueError("torrent info dictionary unusable")
    if not info_span:
        raise ValueError("torrent info span missing")
    _torrent_file_entries(root, strict=True)
    return root, payload[info_span[0]:info_span[1]]


def _torrent_text(value):
    if not isinstance(value, bytes):
        return ""
    return value.decode("utf-8", errors="replace").strip()


def _torrent_path_component(value):
    if not isinstance(value, bytes):
        raise ValueError("torrent path component invalid")
    try:
        raw = value.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("torrent path component invalid") from exc
    text = unicodedata.normalize("NFKC", raw)
    if (
        not text
        or text in {".", ".."}
        or "/" in text
        or "\\" in text
        or text.startswith(("/", "\\"))
        or re.match(r"^[A-Za-z]:", text)
        or text.rstrip(" .") != text
        or any(unicodedata.category(char).startswith("C") for char in text)
    ):
        raise ValueError("torrent path component invalid")
    return text, text.casefold()


def _torrent_file_entries(root, *, strict=False):
    info = root.get(b"info") if isinstance(root, dict) else {}
    root_value = (info or {}).get(b"name.utf-8") or (info or {}).get(b"name")
    try:
        root_name, root_key = _torrent_path_component(root_value)
    except ValueError:
        if strict:
            raise
        root_name, root_key = "", ""
    files = (info or {}).get(b"files")
    v1_entries = []
    v2_entries = []
    v1_seen_paths = set()
    v2_seen_paths = set()

    def append_entry(raw_parts, length, *, target, seen_paths, padding=False):
        try:
            normalized = [_torrent_path_component(part) for part in raw_parts]
        except ValueError:
            if strict:
                raise
            return
        if not normalized:
            if strict:
                raise ValueError("torrent path invalid")
            return
        display_parts = [root_name, *(part[0] for part in normalized)] if root_name else [part[0] for part in normalized]
        key_parts = [root_key, *(part[1] for part in normalized)] if root_key else [part[1] for part in normalized]
        path_key = "/".join(key_parts)
        if path_key in seen_paths:
            if strict:
                raise ValueError("torrent normalized path collision")
            return
        seen_paths.add(path_key)
        target.append({
            "path": "/".join(display_parts),
            "path_key": path_key,
            "length": length,
            "kind": "padding" if padding else "file",
        })

    if isinstance(files, list):
        if strict and not files:
            raise ValueError("torrent file list empty")
        for row in files:
            if not isinstance(row, dict):
                if strict:
                    raise ValueError("torrent file row invalid")
                continue
            parts = row.get(b"path.utf-8") or row.get(b"path")
            length = row.get(b"length")
            if (
                not isinstance(parts, list)
                or not parts
                or not isinstance(length, int)
                or length < 0
            ):
                if strict:
                    raise ValueError("torrent file metadata invalid")
                continue
            attr = row.get(b"attr")
            append_entry(
                parts,
                length,
                target=v1_entries,
                seen_paths=v1_seen_paths,
                padding=isinstance(attr, bytes) and b"p" in attr,
            )
    elif isinstance((info or {}).get(b"length"), int):
        length = (info or {}).get(b"length")
        if strict and length <= 0:
            raise ValueError("torrent single-file length invalid")
        if root_name and length > 0:
            path_key = root_key
            if path_key in v1_seen_paths:
                raise ValueError("torrent normalized path collision")
            v1_seen_paths.add(path_key)
            v1_entries.append({"path": root_name, "path_key": path_key, "length": length, "kind": "file"})
    file_tree = (info or {}).get(b"file tree")
    if isinstance(file_tree, dict):
        def visit_tree(node, parts=(), depth=0):
            if depth > 32 or not isinstance(node, dict) or not node:
                if strict:
                    raise ValueError("torrent v2 file tree invalid")
                return
            if b"" in node:
                leaf = node.get(b"")
                length = leaf.get(b"length") if isinstance(leaf, dict) else None
                pieces_root = leaf.get(b"pieces root") if isinstance(leaf, dict) else None
                attr = leaf.get(b"attr") if isinstance(leaf, dict) else None
                if (
                    not parts
                    or len(node) != 1
                    or not isinstance(leaf, dict)
                    or not isinstance(length, int)
                    or length < 0
                    or (length == 0 and pieces_root is not None)
                    or (length > 0 and (not isinstance(pieces_root, bytes) or len(pieces_root) != 32))
                ):
                    if strict:
                        raise ValueError("torrent v2 leaf metadata invalid")
                    return
                append_entry(
                    parts,
                    length,
                    target=v2_entries,
                    seen_paths=v2_seen_paths,
                    padding=isinstance(attr, bytes) and b"p" in attr,
                )
                return
            for key, child in node.items():
                try:
                    _torrent_path_component(key)
                except ValueError:
                    if strict:
                        raise
                    continue
                visit_tree(child, parts + (key,), depth + 1)

        visit_tree(file_tree)
    if v1_entries and v2_entries:
        v1_manifest = [
            (entry["path_key"], entry["length"])
            for entry in v1_entries
            if entry["kind"] == "file"
        ]
        v2_manifest = [
            (entry["path_key"], entry["length"])
            for entry in v2_entries
            if entry["kind"] == "file"
        ]
        v1_single_file = not isinstance(files, list) and isinstance((info or {}).get(b"length"), int)
        if v1_single_file and len(v1_manifest) == 1 and len(v2_manifest) == 1:
            v1_path, v1_length = v1_manifest[0]
            v2_path, v2_length = v2_manifest[0]
            if v2_path == f"{v1_path}/{v1_path}" and v2_length == v1_length:
                v2_manifest = [(v1_path, v2_length)]
        if v1_manifest != v2_manifest:
            raise ValueError("torrent hybrid manifests diverge")
        out = v1_entries
    else:
        out = v1_entries or v2_entries
    if strict and not out:
        raise ValueError("torrent file tree empty")
    return [{key: value for key, value in entry.items() if key != "path_key"} for entry in out]


def _torrent_filename_key(value):
    text = unicodedata.normalize("NFKC", str(value or "")).replace("\\", "/").strip()
    return re.sub(r"\s+", " ", text.rsplit("/", 1)[-1]).casefold()


def _validate_torrent_identity(payload, expected_identity):
    expected = expected_identity if isinstance(expected_identity, dict) else {}
    root, raw_info = _decode_torrent_payload(payload)
    expected_hash = str(expected.get("info_hash") or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{40}", expected_hash):
        if hashlib.sha1(raw_info).hexdigest() != expected_hash:
            raise RuntimeError("Prowlarr torrent payload identity mismatch")
        return
    if re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        if hashlib.sha256(raw_info).hexdigest() != expected_hash:
            raise RuntimeError("Prowlarr torrent payload identity mismatch")
        return
    entries = _torrent_file_entries(root, strict=True)
    expected_member = str(expected.get("pack_member") or "").strip()
    if expected_member:
        member_key = _torrent_filename_key(expected_member)
        if member_key and any(
            entry.get("kind") == "file"
            and int(entry.get("length") or 0) > 0
            and _torrent_filename_key(entry.get("path")) == member_key
            for entry in entries
        ):
            return
        raise RuntimeError("Prowlarr torrent payload identity mismatch")
    expected_title = qbit_normalize_title(expected.get("title"))
    if expected_title and any(
        int(entry.get("length") or 0) > 0
        and qbit_normalize_title(entry.get("path")) == expected_title
        for entry in entries
    ):
        return
    edition_bound = any(
        expected.get(key)
        for key in ("edition_id", "edition_marker", "publication_title", "publication_year", "publisher")
    )
    series_title = str(expected.get("series_title") or "").strip()
    unit_number = str(expected.get("unit_number") or "").strip()
    if series_title and unit_number and not edition_bound:
        import inkdrop_source_providers

        candidate = {
            "series_title": series_title,
            "unit_type": expected.get("unit_type"),
            "issue_number": expected.get("issue_number") or unit_number,
            "chapter_number": expected.get("chapter_number"),
            "volume_number": expected.get("volume_number"),
        }
        if any(
            inkdrop_source_providers.indexer_manifest_entry_matches_candidate(candidate, entry)
            or inkdrop_source_providers.indexer_manifest_entry_matches_volume_candidate(candidate, entry)
            for entry in (
                row.get("path")
                for row in entries
                if row.get("kind") == "file" and int(row.get("length") or 0) > 0
            )
        ):
            return
    raise RuntimeError("Prowlarr torrent payload identity mismatch")


def _bounded_torrent_payload(
    response,
    max_bytes=QBIT_TORRENT_MAX_BYTES,
    *,
    deadline=None,
    clock=time.monotonic,
    expected_identity=None,
):
    payload = _bounded_response_bytes(
        response,
        max_bytes,
        deadline=deadline,
        clock=clock,
        label="torrent",
    )
    try:
        if expected_identity is None:
            _decode_torrent_payload(payload)
        else:
            _validate_torrent_identity(payload, expected_identity)
    except RuntimeError:
        raise
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Prowlarr returned a malformed torrent payload") from exc
    return payload


def _public_https_redirect(url, resolver=None):
    text = str(url or "").strip()
    parsed = urllib.parse.urlsplit(text)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or "#" in text
    ):
        raise RuntimeError("Prowlarr NZB redirect was refused")
    hostname = str(parsed.hostname).strip().lower().rstrip(".")
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise RuntimeError("Prowlarr NZB redirect was refused")
    try:
        resolver = resolver or socket.getaddrinfo
        port = parsed.port or 443
        rows = resolver(hostname, port, type=socket.SOCK_STREAM)
        addresses = {str(row[4][0]).split("%", 1)[0] for row in rows if row and len(row) > 4 and row[4]}
        parsed_addresses = {ipaddress.ip_address(value) for value in addresses}
    except (OSError, TypeError, ValueError):
        raise RuntimeError("Prowlarr NZB redirect was refused")
    if not parsed_addresses or any(
        not address.is_global
        or address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
        for address in parsed_addresses
    ):
        raise RuntimeError("Prowlarr NZB redirect was refused")
    return text, {str(address) for address in parsed_addresses}


def _response_peer_address(response):
    raw = getattr(response, "raw", None)
    candidates = (
        getattr(getattr(raw, "_connection", None), "sock", None),
        getattr(getattr(raw, "connection", None), "sock", None),
        getattr(
            getattr(getattr(getattr(raw, "_fp", None), "fp", None), "raw", None),
            "_sock",
            None,
        ),
        getattr(
            getattr(getattr(getattr(raw, "_original_response", None), "fp", None), "raw", None),
            "_sock",
            None,
        ),
    )
    for sock in candidates:
        if not sock:
            continue
        try:
            peer = sock.getpeername()
            if peer and peer[0]:
                return str(peer[0]).split("%", 1)[0]
        except (AttributeError, OSError, TypeError, ValueError):
            continue
    return ""


def _prowlarr_nzb_response(http, fetch_url, api_key, deadline, *, clock=time.monotonic):
    def remaining_timeout():
        remaining = deadline - clock()
        if remaining <= 0:
            raise RuntimeError("Prowlarr NZB fetch exceeded the total deadline")
        return max(0.1, min(5.0, remaining))

    request_timeout = remaining_timeout()
    try:
        response = http.get(
            fetch_url,
            headers={"X-Api-Key": api_key, "Accept": "application/x-nzb, application/xml"},
            timeout=request_timeout,
            stream=True,
            allow_redirects=False,
        )
    except Exception as exc:
        raise RuntimeError("Prowlarr NZB fetch failed") from exc
    status = int(getattr(response, "status_code", 200) or 200)
    if 300 <= status < 400:
        if status not in {301, 302, 303, 307, 308}:
            raise RuntimeError("Prowlarr NZB redirect was refused")
        location = str((getattr(response, "headers", {}) or {}).get("Location") or "")
        redirect_url, resolved_addresses = _public_https_redirect(location)
        close = getattr(response, "close", None)
        if callable(close):
            close()
        redirect_http = http
        if requests is not None and isinstance(http, requests.Session):
            redirect_http = requests.Session()
            redirect_http.trust_env = False
            redirect_http.auth = ()
        request_timeout = remaining_timeout()
        try:
            response = redirect_http.get(
                redirect_url,
                headers={"Accept": "application/x-nzb, application/xml"},
                timeout=request_timeout,
                stream=True,
                allow_redirects=False,
            )
        except Exception as exc:
            raise RuntimeError("Prowlarr NZB fetch failed") from exc
        status = int(getattr(response, "status_code", 200) or 200)
        if 300 <= status < 400:
            raise RuntimeError("Prowlarr NZB redirect was refused")
        peer = _response_peer_address(response)
        if not peer:
            raise RuntimeError("Prowlarr NZB redirect was refused")
        try:
            peer_address = ipaddress.ip_address(peer)
        except ValueError:
            raise RuntimeError("Prowlarr NZB redirect was refused")
        if str(peer_address) not in resolved_addresses or not peer_address.is_global:
            raise RuntimeError("Prowlarr NZB redirect was refused")
    try:
        response.raise_for_status()
    except Exception as exc:
        raise RuntimeError("Prowlarr NZB fetch failed") from exc
    return response


def _prowlarr_torrent_response(http, fetch_url, api_key, deadline, *, clock=time.monotonic):
    def remaining_timeout():
        remaining = deadline - clock()
        if remaining <= 0:
            raise RuntimeError("Prowlarr torrent fetch exceeded the total deadline")
        return max(0.1, min(5.0, remaining))

    try:
        response = http.get(
            fetch_url,
            headers={"X-Api-Key": api_key, "Accept": "application/x-bittorrent, application/octet-stream"},
            timeout=remaining_timeout(),
            stream=True,
            allow_redirects=False,
        )
    except Exception as exc:
        if "timed out" in str(exc).lower() or "timeout" in str(exc).lower():
            raise RuntimeError("Prowlarr torrent fetch timed out") from exc
        raise RuntimeError("Prowlarr torrent fetch failed") from exc
    status = int(getattr(response, "status_code", 200) or 200)
    if 300 <= status < 400:
        if status not in {301, 302, 303, 307, 308}:
            raise RuntimeError("Prowlarr torrent redirect was refused")
        location = str((getattr(response, "headers", {}) or {}).get("Location") or "")
        try:
            redirect_url, resolved_addresses = _public_https_redirect(location)
        except RuntimeError as exc:
            raise RuntimeError("Prowlarr torrent redirect was refused") from exc
        close = getattr(response, "close", None)
        if callable(close):
            close()
        redirect_http = http
        if requests is not None and isinstance(http, requests.Session):
            redirect_http = requests.Session()
            redirect_http.trust_env = False
            redirect_http.auth = ()
        try:
            response = redirect_http.get(
                redirect_url,
                headers={"Accept": "application/x-bittorrent, application/octet-stream"},
                timeout=remaining_timeout(),
                stream=True,
                allow_redirects=False,
            )
        except Exception as exc:
            if "timed out" in str(exc).lower() or "timeout" in str(exc).lower():
                raise RuntimeError("Prowlarr torrent fetch timed out") from exc
            raise RuntimeError("Prowlarr torrent fetch failed") from exc
        status = int(getattr(response, "status_code", 200) or 200)
        if 300 <= status < 400:
            raise RuntimeError("Prowlarr torrent redirect was refused")
        peer = _response_peer_address(response)
        if not peer:
            raise RuntimeError("Prowlarr torrent redirect was refused")
        try:
            peer_address = ipaddress.ip_address(peer)
        except ValueError as exc:
            raise RuntimeError("Prowlarr torrent redirect was refused") from exc
        if str(peer_address) not in resolved_addresses or not peer_address.is_global:
            raise RuntimeError("Prowlarr torrent redirect was refused")
    try:
        response.raise_for_status()
    except Exception as exc:
        raise RuntimeError("Prowlarr torrent fetch failed") from exc
    return response


def _prowlarr_torrent_fetch_subprocess():
    """Private isolated torrent fetch entry point with bounded JSON pipes."""
    try:
        request = json.loads(sys.stdin.buffer.read(64 * 1024).decode("utf-8"))
        fetch_url = str(request.get("url") or "")
        api_key = str(request.get("api_key") or "")
        timeout_seconds = max(0.1, float(request.get("timeout_seconds") or 0.1))
        max_bytes = max(1, int(request.get("max_bytes") or QBIT_TORRENT_MAX_BYTES))
        expected_identity = request.get("expected_identity")
        deadline = time.monotonic() + timeout_seconds
        session = requests.Session()
        session.trust_env = False
        response = _prowlarr_torrent_response(session, fetch_url, api_key, deadline)
        payload = _bounded_torrent_payload(
            response,
            max_bytes=max_bytes,
            deadline=deadline,
            expected_identity=expected_identity,
        )
        result = {"ok": True, "payload": base64.b64encode(payload).decode("ascii")}
    except RuntimeError as exc:
        result = {"ok": False, "reason": str(exc)}
    except Exception:
        result = {"ok": False, "reason": "Prowlarr torrent fetch failed"}
    sys.stdout.write(json.dumps(result, separators=(",", ":")))


def _prowlarr_nzb_fetch_subprocess():
    """Private isolated fetch entry point. Input/output are bounded JSON over pipes."""

    try:
        request = json.loads(sys.stdin.buffer.read(64 * 1024).decode("utf-8"))
        fetch_url = str(request.get("url") or "")
        api_key = str(request.get("api_key") or "")
        timeout_seconds = max(0.1, float(request.get("timeout_seconds") or 0.1))
        max_bytes = max(1, int(request.get("max_bytes") or SAB_NZB_MAX_BYTES))
        deadline = time.monotonic() + timeout_seconds
        session = requests.Session()
        session.trust_env = False
        response = _prowlarr_nzb_response(session, fetch_url, api_key, deadline)
        payload = _bounded_nzb_payload(response, max_bytes=max_bytes, deadline=deadline)
        result = {"ok": True, "payload": base64.b64encode(payload).decode("ascii")}
    except RuntimeError as exc:
        result = {"ok": False, "reason": str(exc)}
    except Exception:
        result = {"ok": False, "reason": "Prowlarr NZB fetch failed"}
    sys.stdout.write(json.dumps(result, separators=(",", ":")))


def _isolated_prowlarr_nzb_fetch(fetch_url, api_key, timeout_seconds, max_bytes=SAB_NZB_MAX_BYTES):
    request = json.dumps(
        {
            "url": fetch_url,
            "api_key": api_key,
            "timeout_seconds": timeout_seconds,
            "max_bytes": max_bytes,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    popen_kwargs = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.DEVNULL,
        "cwd": str(Path(__file__).resolve().parent),
        "close_fds": True,
    }
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import inkdrop_acquire as m; m._prowlarr_nzb_fetch_subprocess()",
        ],
        **popen_kwargs,
    )
    try:
        stdout, _stderr = proc.communicate(input=request, timeout=max(0.01, float(timeout_seconds)))
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        try:
            proc.communicate(timeout=1.0)
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait(timeout=1.0)
        raise RuntimeError("Prowlarr NZB fetch exceeded the total deadline") from exc
    if proc.returncode != 0 or len(stdout or b"") > ((max_bytes * 4 // 3) + 4096):
        raise RuntimeError("Prowlarr NZB fetch failed")
    try:
        result = json.loads((stdout or b"").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Prowlarr NZB fetch failed") from exc
    if not isinstance(result, dict) or not result.get("ok"):
        reason = str((result or {}).get("reason") or "Prowlarr NZB fetch failed")
        if not reason.startswith("Prowlarr NZB") and reason != "Prowlarr returned a malformed NZB payload" and reason != "Prowlarr returned a semantically unusable NZB payload":
            reason = "Prowlarr NZB fetch failed"
        raise RuntimeError(reason)
    try:
        payload = base64.b64decode(result.get("payload") or "", validate=True)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Prowlarr NZB fetch failed") from exc
    if len(payload) > max_bytes:
        raise RuntimeError("Prowlarr NZB payload exceeded the configured size limit")
    return payload


def _isolated_prowlarr_torrent_fetch(
    fetch_url,
    api_key,
    timeout_seconds,
    max_bytes=QBIT_TORRENT_MAX_BYTES,
    expected_identity=None,
):
    request = json.dumps(
        {
            "url": fetch_url,
            "api_key": api_key,
            "timeout_seconds": timeout_seconds,
            "max_bytes": max_bytes,
            "expected_identity": expected_identity,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    popen_kwargs = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.DEVNULL,
        "cwd": str(Path(__file__).resolve().parent),
        "close_fds": True,
    }
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    proc = subprocess.Popen(
        [sys.executable, "-c", "import inkdrop_acquire as m; m._prowlarr_torrent_fetch_subprocess()"],
        **popen_kwargs,
    )
    try:
        stdout, _stderr = proc.communicate(input=request, timeout=max(0.01, float(timeout_seconds)))
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        try:
            proc.communicate(timeout=1.0)
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait(timeout=1.0)
        raise RuntimeError("Prowlarr torrent fetch exceeded the total deadline") from exc
    if proc.returncode != 0 or len(stdout or b"") > ((max_bytes * 4 // 3) + 4096):
        raise RuntimeError("Prowlarr torrent fetch failed")
    try:
        result = json.loads((stdout or b"").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Prowlarr torrent fetch failed") from exc
    if not isinstance(result, dict) or not result.get("ok"):
        reason = str((result or {}).get("reason") or "Prowlarr torrent fetch failed")
        allowed = {
            "Prowlarr torrent fetch failed",
            "Prowlarr torrent fetch timed out",
            "Prowlarr torrent fetch exceeded the total deadline",
            "Prowlarr torrent payload exceeded the configured size limit",
            "Prowlarr returned a malformed torrent payload",
            "Prowlarr torrent payload identity mismatch",
            "Prowlarr torrent redirect was refused",
        }
        raise RuntimeError(reason if reason in allowed else "Prowlarr torrent fetch failed")
    try:
        payload = base64.b64decode(result.get("payload") or "", validate=True)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Prowlarr torrent fetch failed") from exc
    if len(payload) > max_bytes:
        raise RuntimeError("Prowlarr torrent payload exceeded the configured size limit")
    try:
        if expected_identity is None:
            _decode_torrent_payload(payload)
        else:
            _validate_torrent_identity(payload, expected_identity)
    except RuntimeError:
        raise
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Prowlarr returned a malformed torrent payload") from exc
    return payload


def fetch_prowlarr_torrent(http, download_url, expected_url_hash, *, expected_torrent_identity=None):
    fetch_url = prowlarr_torrent_fetch_url(download_url, expected_url_hash)
    if not fetch_url:
        return None
    deadline = time.monotonic() + QBIT_TORRENT_FETCH_TIMEOUT_SECONDS
    try:
        api_key = load_prowlarr_key()
    except Exception as exc:
        raise RuntimeError("Prowlarr torrent fetch failed") from exc
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError("Prowlarr torrent fetch exceeded the total deadline")
    if http is requests:
        return _isolated_prowlarr_torrent_fetch(
            fetch_url,
            api_key,
            remaining,
            expected_identity=expected_torrent_identity,
        )
    response = _prowlarr_torrent_response(http, fetch_url, api_key, deadline)
    return _bounded_torrent_payload(
        response,
        deadline=deadline,
        expected_identity=expected_torrent_identity,
    )


def prowlarr_torrent_fetch_url(download_url, expected_url_hash):
    """Validate immutable URL authority without performing network I/O."""
    raw_url = str(download_url or "")
    fetch_url = prowlarr_nzb_fetch_url(raw_url)
    if not fetch_url:
        return None
    expected_url_hash = str(expected_url_hash or "").strip().lower()
    actual_url_hash = hashlib.sha256(raw_url.encode("utf-8")).hexdigest()
    if not expected_url_hash or not re.fullmatch(r"[0-9a-f]{64}", expected_url_hash):
        raise RuntimeError("Prowlarr torrent URL authority is missing")
    if actual_url_hash != expected_url_hash:
        raise RuntimeError("Prowlarr torrent URL authority mismatch")
    return fetch_url


def fetch_prowlarr_nzb(http, download_url):
    clock = time.monotonic
    deadline = clock() + SAB_NZB_FETCH_TIMEOUT_SECONDS
    fetch_url = prowlarr_nzb_fetch_url(download_url)
    if not fetch_url:
        return None
    try:
        api_key = load_prowlarr_key()
    except Exception as exc:
        raise RuntimeError("Prowlarr NZB fetch failed") from exc
    remaining = deadline - clock()
    if remaining <= 0:
        raise RuntimeError("Prowlarr NZB fetch exceeded the total deadline")
    if http is requests:
        return _isolated_prowlarr_nzb_fetch(fetch_url, api_key, remaining)
    response = _prowlarr_nzb_response(http, fetch_url, api_key, deadline, clock=clock)
    return _bounded_nzb_payload(response, deadline=deadline, clock=clock)


def sab_add(download_url, title, dry_run=False, unique_tag=None, settings_override=None):
    http = require_requests()
    sab = dict(settings_override) if isinstance(settings_override, dict) else load_sab_settings()
    category = sab.get("category") or sab["comics_category"]
    client_download_url = prowlarr_download_url_for_client(download_url)
    handoff_key = sab_handoff_key(title, client_download_url, unique_tag=unique_tag)
    if dry_run:
        return {
            "dry_run": True,
            "download_client": "SABnzbd",
            "protocol": "usenet",
            "category": category,
            "handoff_key": handoff_key,
            "settings_source": sab.get("source"),
        }
    existing = sab_find_existing_job(http, sab, handoff_key)
    if existing:
        return sab_existing_result(
            existing,
            category=category,
            handoff_key=handoff_key,
            settings_source=sab.get("source"),
        )
    nzb_payload = fetch_prowlarr_nzb(http, download_url)
    if nzb_payload is None and not _sab_url_delegation_allowed(
        client_download_url,
        sab.get("host"),
        allowed_hosts=sab.get("delegated_url_allowed_hosts"),
    ):
        raise RuntimeError("SABnzbd URL handoff was refused")
    params = {
        "mode": "addfile" if nzb_payload is not None else "addurl",
        "output": "json",
        "apikey": sab["api_key"],
        "cat": category,
        "nzbname": title,
        "dupekey": handoff_key,
        "dupemode": "score",
        "dupescore": 0,
    }
    if nzb_payload is None:
        params["name"] = client_download_url
        response = http.get(sab["host"] + "/api", params=params, timeout=30)
    else:
        filename = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(title or handoff_key)).strip("-.") or handoff_key
        filename = re.sub(r"(?i)\.nzb$", "", filename)[:176].rstrip("-.") + ".nzb"
        response = http.post(
            sab["host"] + "/api",
            data=params,
            files={"name": (filename, nzb_payload, "application/x-nzb")},
            timeout=30,
        )
    response.raise_for_status()
    data = response.json()
    audit("sab_add", {
        "title": title,
        "category": category,
        "settings_source": sab.get("source"),
        "handoff_mode": params["mode"],
    })
    if isinstance(data, dict):
        data["download_client"] = "SABnzbd"
        data["protocol"] = "usenet"
        data["category"] = category
        data["handoff_key"] = handoff_key
        data["settings_source"] = sab.get("source")
        data["handoff_mode"] = params["mode"]
    return data


def transmission_add(download_url, title, dry_run=False, unique_tag=None):
    settings = load_transmission_settings()
    download_url = prowlarr_download_url_for_client(download_url)
    outcome = inkdrop_download_clients.transmission_add(
        download_url,
        title,
        settings,
        dry_run=dry_run,
        unique_tag=unique_tag,
    )
    audit(
        "transmission_add",
        {
            "title": title,
            "category": outcome.get("category"),
            "save_path": outcome.get("save_path"),
            "handoff_tag": outcome.get("handoff_tag"),
            "settings_source": outcome.get("settings_source"),
            "torrent_hash": outcome.get("torrent_hash"),
        },
    )
    return outcome


def deluge_add(download_url, title, dry_run=False, unique_tag=None):
    settings = load_deluge_settings()
    download_url = prowlarr_download_url_for_client(download_url)
    outcome = inkdrop_download_clients.deluge_add(
        download_url,
        title,
        settings,
        dry_run=dry_run,
        unique_tag=unique_tag,
    )
    audit(
        "deluge_add",
        {
            "title": title,
            "category": outcome.get("category"),
            "save_path": outcome.get("save_path"),
            "handoff_tag": outcome.get("handoff_tag"),
            "settings_source": outcome.get("settings_source"),
            "torrent_hash": outcome.get("torrent_hash"),
        },
    )
    return outcome


def nzbget_add(download_url, title, dry_run=False, unique_tag=None):
    settings = load_nzbget_settings()
    download_url = prowlarr_download_url_for_client(download_url)
    outcome = inkdrop_download_clients.nzbget_add(
        download_url,
        title,
        settings,
        dry_run=dry_run,
        unique_tag=unique_tag,
    )
    audit(
        "nzbget_add",
        {
            "title": title,
            "category": outcome.get("category"),
            "save_path": outcome.get("save_path"),
            "handoff_key": outcome.get("handoff_key"),
            "settings_source": outcome.get("settings_source"),
            "nzb_id": outcome.get("nzb_id"),
        },
    )
    return outcome


def utorrent_add(download_url, title, dry_run=False, unique_tag=None):
    outcome = inkdrop_download_clients.utorrent_add(
        prowlarr_download_url_for_client(download_url), title, load_utorrent_settings(),
        dry_run=dry_run, unique_tag=unique_tag,
    )
    audit("utorrent_add", {"title": title, "torrent_hash": outcome.get("torrent_hash"), "handoff_tag": outcome.get("handoff_tag")})
    return outcome


def rtorrent_add(download_url, title, dry_run=False, unique_tag=None):
    outcome = inkdrop_download_clients.rtorrent_add(
        prowlarr_download_url_for_client(download_url), title, load_rtorrent_settings(),
        dry_run=dry_run, unique_tag=unique_tag,
    )
    audit("rtorrent_add", {"title": title, "torrent_hash": outcome.get("torrent_hash"), "handoff_tag": outcome.get("handoff_tag")})
    return outcome


def choose_result(results, prefer):
    if not results:
        return None
    if prefer != "any":
        for result in results:
            if result.get("protocol") == prefer:
                return result
    return results[0]


def cmd_search(args):
    results = prowlarr_search(args.query, args.type, args.indexer_id, args.limit)
    safe = []
    for index, item in enumerate(results, 1):
        safe.append(
            {
                "n": index,
                "title": item.get("title"),
                "indexer": item.get("indexer"),
                "indexerId": item.get("indexerId"),
                "protocol": item.get("protocol"),
                "size": item.get("size"),
                "seeders": item.get("seeders"),
                "leechers": item.get("leechers"),
                "publishDate": item.get("publishDate"),
                "categories": category_ids(item),
            }
        )
    print(json.dumps(safe, indent=2))


def cmd_probe(args):
    summary = {
        "query": args.query,
        "type": args.type,
        "series": args.series,
        "issue": args.issue,
        "indexerIds": args.indexer_id or [],
        "results": [],
    }
    results = prowlarr_search(args.query, args.type, args.indexer_id, args.limit)
    counts = {}
    for index, item in enumerate(results, 1):
        classification = classify_probe_result(item, args.series, args.issue)
        counts[classification["classification"]] = counts.get(classification["classification"], 0) + 1
        summary["results"].append(
            {
                "n": index,
                "title": item.get("title"),
                "indexer": item.get("indexer"),
                "indexerId": item.get("indexerId"),
                "protocol": item.get("protocol"),
                "size": item.get("size"),
                "seeders": item.get("seeders"),
                "leechers": item.get("leechers"),
                "publishDate": item.get("publishDate"),
                "categories": category_ids(item),
                **classification,
            }
        )
    summary["counts"] = counts
    print(json.dumps(summary, indent=2))


def cmd_grab(args):
    results = prowlarr_search(args.query, args.type, args.indexer_id, args.limit)
    chosen = choose_result(results, args.prefer)
    if not chosen:
        print(json.dumps({"status": "no_results", "query": args.query}))
        return 2
    protocol = chosen.get("protocol")
    title = chosen.get("title") or args.query
    download_url = chosen.get("downloadUrl")
    if not download_url:
        raise RuntimeError("Chosen result has no downloadUrl")
    if protocol == "torrent":
        outcome = qbit_add(download_url, title, args.type, args.dry_run)
    elif protocol == "usenet":
        outcome = sab_add(download_url, title, args.dry_run)
    else:
        raise RuntimeError(f"Unsupported protocol: {protocol}")
    pending = None if args.dry_run else record_pending_import(args.query, args.type, chosen, outcome if isinstance(outcome, dict) else {})
    print(json.dumps(
        {
            "status": "selected",
            "title": title,
            "indexer": chosen.get("indexer"),
            "protocol": protocol,
            "size": chosen.get("size"),
            "seeders": chosen.get("seeders"),
            "outcome": outcome,
            "pending_import_recorded": bool(pending),
        },
        indent=2,
    ))
    return 0


def main():
    parser = argparse.ArgumentParser(description="Controlled Prowlarr-to-Kavita acquisition helper")
    sub = parser.add_subparsers(dest="cmd", required=True)

    search = sub.add_parser("search")
    search.add_argument("query")
    search.add_argument("--type", choices=["comics", "ebooks"], default="comics")
    search.add_argument("--indexer-id", action="append", type=int)
    search.add_argument("--limit", type=int, default=10)
    search.set_defaults(func=cmd_search)

    probe = sub.add_parser("probe")
    probe.add_argument("query")
    probe.add_argument("--type", choices=["comics", "ebooks"], default="comics")
    probe.add_argument("--indexer-id", action="append", type=int)
    probe.add_argument("--limit", type=int, default=10)
    probe.add_argument("--series")
    probe.add_argument("--issue")
    probe.set_defaults(func=cmd_probe)

    grab = sub.add_parser("grab")
    grab.add_argument("query")
    grab.add_argument("--type", choices=["comics", "ebooks"], default="comics")
    grab.add_argument("--indexer-id", action="append", type=int)
    grab.add_argument("--prefer", choices=["torrent", "usenet", "any"], default="torrent")
    grab.add_argument("--limit", type=int, default=10)
    grab.add_argument("--dry-run", action="store_true")
    grab.set_defaults(func=cmd_grab)

    args = parser.parse_args()
    raise SystemExit(args.func(args) or 0)


if __name__ == "__main__":
    main()
