#!/usr/bin/env python3
import argparse
import hashlib
import html
import importlib.util
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath

import inkdrop_db

try:
    import inkdrop_state
except Exception:
    inkdrop_state = None

import inkdrop_runtime_config
import inkdrop_qbittorrent_auth
import inkdrop_internal_jobs
import inkdrop_artifact_acceptance
import inkdrop_manual_search

try:
    import inkdrop_language
except Exception:
    inkdrop_language = None

try:
    import inkdrop_nfo_parser
except Exception:
    inkdrop_nfo_parser = None

CONFIG_DIR = inkdrop_runtime_config.config_dir()
STATE_DIR = inkdrop_runtime_config.state_dir()
LOG_DIR = inkdrop_runtime_config.log_dir()
ACQUIRE_PATH = Path(os.environ.get("INKDROP_ACQUIRE_MODULE") or Path(__file__).resolve().with_name("inkdrop_acquire.py"))
INKDROP_STATE_DB = STATE_DIR / (inkdrop_state.STATE_DB_NAME if inkdrop_state else "inkdrop-state.sqlite3")
COMPLETION_DB = STATE_DIR / "imported-files.sqlite3"
PENDING_IMPORTS_LOG = STATE_DIR / "pending-imports.jsonl"
AUDIT_LOG = LOG_DIR / "missing-acquire.log"
CACHE_FILE = STATE_DIR / "missing-acquire-cache.json"
RECONCILE_STATUS_FILE = STATE_DIR / "import-reconcile-status.json"
REVIEW_FILE = STATE_DIR / "manual-review.jsonl"
MANUAL_REVIEW_ACTIONS_FILE = STATE_DIR / "manual-review-actions.json"
PACK_REVIEW_STATE_FILE = STATE_DIR / "pack-review-state.json"
PENDING_PACKS_LOG = STATE_DIR / "pending-pack-imports.jsonl"
QBIT_BROAD_TAGS = {"inkdrop", "kavita-acquire"}
PACK_BAD_ARCHIVE_HISTORY_FILE = STATE_DIR / "pack-bad-archive-history.json"
BAD_RESULT_TTL = 30 * 86400
SOURCE_FAILURE_STATES = {"failed_download", "bad_archive", "false_positive", "stale_no_local_file", "wrong_series_or_subseries"}
QUEUE_MODE_REVIEW_REASONS = {
    "unsafe_or_missing_target_folder",
}
MANUAL_REVIEW_PERSIST_ENV = "INKDROP_PERSIST_SOFT_REVIEWS"
PACK_REVIEWABLE_BLOCK_REASONS = {
    "unknown_pack_contents",
    "english_not_confirmed",
    "pack_requires_review",
    "special_or_issue_zero_requires_review",
}
PACK_IN_FLIGHT_STALE_SECONDS = 12 * 3600
PACK_BAD_ARCHIVE_AUTO_BLOCK_MIN = 3
PACK_BAD_ARCHIVE_HISTORY_CACHE = {"mtime": None, "counts": {}}
QUALITY_LANGUAGE_RULES_CACHE = None
SQLITE_LOCK_RETRY_ATTEMPTS = int(os.environ.get("INKDROP_SQLITE_LOCK_RETRY_ATTEMPTS") or "4")
SQLITE_LOCK_RETRY_INITIAL_DELAY_SECONDS = float(os.environ.get("INKDROP_SQLITE_LOCK_RETRY_INITIAL_DELAY_SECONDS") or "0.75")
SQLITE_BUSY_TIMEOUT_MS = int(os.environ.get("INKDROP_SQLITE_BUSY_TIMEOUT_MS") or "60000")


def inkdrop_web_api_url(path):
    base_url = inkdrop_runtime_config.worker_web_base_url()
    return urllib.parse.urljoin(base_url + "/", str(path or "").lstrip("/"))


DEFAULT_QUALITY_LANGUAGE_RULES = {
    "preferred_language": "english",
    "pdf_allowed": True,
    "packs_allowed": True,
    "pack_auto_approve_min_missing": 1,
    "complete_pack_min_missing": 1,
    "allowed_extensions": {".cbz", ".cbr", ".pdf", ".epub", ".zip", ".rar", ".7z"},
    "blocked_release_terms": [
        "spanish",
        "espanol",
        "spa",
        "castellano",
        "latino",
        "latam",
        "es-la",
        "es-es",
        "pt-br",
        "portuguese",
        "brasileiro",
        "francais",
        "french",
        "italian",
        "deutsch",
        "german",
    ],
    "source": "fallback",
}
DEFAULT_PROTOCOL_ORDER = ["usenet", "torrent", "direct"]
PACK_DETAIL_CACHE_TTL_SECONDS = 7 * 86400
PACK_DETAIL_MAX_BYTES = 8 * 1024 * 1024
PACK_DETAIL_SIDECAR_MAX_BYTES = 1024 * 1024
PACK_DETAIL_MAX_FETCHES_PER_RUN = 8
PACK_DETAIL_MAX_ENTRIES = 1000
PACK_DETAIL_SIDECAR_HEADER_KEYS = (
    "x-dnzb-nfo",
    "x-nzb-nfo",
    "x-dnzb-details",
    "x-nzb-details",
)
TRUSTED_PROWLARR_HOSTS = {
    host.strip().lower()
    for host in str(os.environ.get("INKDROP_TRUSTED_PROWLARR_HOSTS") or "").split(",")
    if host.strip()
}
DEFAULT_COLLECTED_EDITION_RANGE_HINTS = {
    "gotham central": {
        "book": {
            "1": [1, 10],
            "2": [11, 22],
            "3": [23, 31],
            "4": [32, 40],
        },
    },
    "the league of extraordinary gentlemen": {
        "volume": {
            "1": [1, 6],
        },
        "vol": {
            "1": [1, 6],
        },
        "v": {
            "1": [1, 6],
        },
    },
    "league of extraordinary gentlemen": {
        "volume": {
            "1": [1, 6],
        },
        "vol": {
            "1": [1, 6],
        },
        "v": {
            "1": [1, 6],
        },
    },
}

MANGA_SERIES = {"berserk", "fire punch", "one piece"}
MANGA_PUBLISHERS = {
    "hakusensha",
    "kodansha",
    "shogakukan",
    "shueisha",
    "viz",
    "viz media",
    "yen press",
    "seven seas",
    "dark horse manga",
    "square enix",
    "mangadex",
}
COMICS_CONTAINER_ROOT = Path("/comics")
COMICS_HOST_ROOT = Path(os.environ.get("INKDROP_COMIC_ROOT") or "/library/comics")
MANGA_CONTAINER_ROOT = Path("/manga")
MANGA_HOST_ROOT = Path(os.environ.get("INKDROP_MANGA_ROOT") or "/library/manga")


def load_acquire():
    spec = importlib.util.spec_from_file_location("inkdrop_acquire", ACQUIRE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def audit(event, payload):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with AUDIT_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"ts": time.time(), "event": event, **payload}, sort_keys=True) + "\n")


def sqlite_locked(exc):
    checker = getattr(inkdrop_state, "is_database_locked_error", None) if inkdrop_state else None
    if checker:
        try:
            return bool(checker(exc))
        except Exception:
            pass
    return isinstance(exc, sqlite3.OperationalError) and "database is locked" in str(exc).lower()


def audit_warning(event, payload):
    try:
        audit(event, payload)
    except Exception as exc:
        print(json.dumps({"ts": time.time(), "event": event, **payload, "audit_error": str(exc)}, sort_keys=True), flush=True)


def with_sqlite_lock_retry(fn, label):
    attempts = max(1, int(SQLITE_LOCK_RETRY_ATTEMPTS or 1))
    delay = max(0.1, float(SQLITE_LOCK_RETRY_INITIAL_DELAY_SECONDS or 0.75))
    for attempt in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if not sqlite_locked(exc) or attempt >= attempts - 1:
                raise
            audit_warning(
                "sqlite_lock_retry",
                {"label": label, "attempt": attempt + 1, "attempts": attempts, "error": str(exc)},
            )
            time.sleep(delay)
            delay = min(delay * 2, 10.0)


def review(reason, payload):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    record = {"ts": time.time(), "reason": reason, **payload}
    if not should_persist_review(reason):
        return record
    with REVIEW_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    return record


def truthy_env(name):
    value = str(os.environ.get(name) or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def env_is_set(name):
    return name in os.environ and str(os.environ.get(name) or "").strip() != ""


def env_float(name, default):
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return float(default)


def env_int(name, default, minimum=None, maximum=None):
    try:
        value = int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        value = int(default)
    if minimum is not None:
        value = max(int(minimum), value)
    if maximum is not None:
        value = min(int(maximum), value)
    return value


def inkdrop_app_setting(key):
    if inkdrop_state is None:
        return None
    try:
        return inkdrop_state.app_setting(INKDROP_STATE_DB, key) or {}
    except Exception as exc:
        audit("inkdrop_app_setting_load_failed", {"key": key, "error": f"{type(exc).__name__}: {exc}"})
        return None


def inkdrop_provider_config(provider_id):
    if inkdrop_state is None:
        return None
    try:
        return inkdrop_state.provider_config(INKDROP_STATE_DB, provider_id) or {}
    except Exception as exc:
        audit("inkdrop_provider_config_load_failed", {"provider_id": provider_id, "error": f"{type(exc).__name__}: {exc}"})
        return None


def inkdrop_app_setting_value(key, default=None):
    setting = inkdrop_app_setting(key) or {}
    return setting.get("value", default)


def inkdrop_user_app_setting_value(key, default=None):
    setting = inkdrop_app_setting(key) or {}
    if setting.get("source") != "user":
        return default
    return setting.get("value", default)


def inkdrop_quality_app_settings():
    return {
        "preferred_language": inkdrop_user_app_setting_value("quality.preferred_language", None),
        "allow_non_english": inkdrop_user_app_setting_value("quality.allow_non_english", None),
        "allow_pdfs": inkdrop_user_app_setting_value("quality.allow_pdfs", None),
        "allow_packs": inkdrop_user_app_setting_value("quality.allow_packs", None),
        "preferred_extensions": inkdrop_user_app_setting_value("quality.preferred_extensions", []) or [],
        "blocked_release_terms": inkdrop_user_app_setting_value("quality.blocked_release_terms", []) or [],
    }


def boolish_value(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def queue_mode_enabled():
    if env_is_set("INKDROP_QUEUE_MODE"):
        return truthy_env("INKDROP_QUEUE_MODE")
    return boolish_value(inkdrop_app_setting_value("automation.queue_mode", True), True)




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
    raw = []
    if isinstance(value, str):
        raw = [part.strip() for part in value.split(",")]
    elif isinstance(value, (list, tuple)):
        raw = list(value)
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
    if env_is_set("INKDROP_PROTOCOL_ORDER"):
        return normalize_protocol_order(os.environ.get("INKDROP_PROTOCOL_ORDER"))
    return normalize_protocol_order(inkdrop_app_setting_value("automation.protocol_order", DEFAULT_PROTOCOL_ORDER))


def protocol_rank(protocol, order=None):
    order = normalize_protocol_order(order if order is not None else configured_protocol_order())
    protocol = normalize_protocol_name(protocol)
    try:
        return order.index(protocol)
    except ValueError:
        return len(order)


def bool_setting(settings, key, default):
    if not isinstance(settings, dict):
        return bool(default)
    value = settings.get(key)
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def int_setting(settings, key, default, minimum=None, maximum=None):
    if not isinstance(settings, dict):
        value = int(default)
    else:
        try:
            value = int(settings.get(key, default))
        except (TypeError, ValueError):
            value = int(default)
    if minimum is not None:
        value = max(int(minimum), value)
    if maximum is not None:
        value = min(int(maximum), value)
    return value


def float_setting(settings, key, default, minimum=None, maximum=None):
    if not isinstance(settings, dict):
        value = float(default)
    else:
        try:
            value = float(settings.get(key, default))
        except (TypeError, ValueError):
            value = float(default)
    if minimum is not None:
        value = max(float(minimum), value)
    if maximum is not None:
        value = min(float(maximum), value)
    return value


def load_prowlarr_missing_provider_settings():
    config = inkdrop_provider_config("prowlarr") or {}
    settings = config.get("settings") if isinstance(config.get("settings"), dict) else {}
    return {
        "enabled": boolish_value(config.get("enabled"), True),
        "source": config.get("source") or ("runtime" if config else "fallback"),
        "limit": int_setting(settings, "limit", 20, 1, 100),
        "max_queries_per_issue": int_setting(settings, "max_queries_per_issue", 6, 1, 20),
        "timeout_seconds": float_setting(settings, "timeout_seconds", env_float("INKDROP_PROWLARR_SEARCH_TIMEOUT_SECONDS", 12.0), 1.0, 120.0),
        "search_budget_seconds": float_setting(settings, "search_budget_seconds", 0.0, 0.0, 300.0),
        "no_result_cooldown_hours": float_setting(settings, "no_result_cooldown_hours", 24.0, 0.0, 24.0 * 30.0),
    }


def apply_prowlarr_missing_provider_defaults(args):
    settings = load_prowlarr_missing_provider_settings()
    args.prowlarr_provider_defaults = dict(settings)
    args.prowlarr_provider_enabled = bool(settings["enabled"])
    args.prowlarr_provider_settings_source = settings["source"]
    if args.limit is None:
        args.limit = settings["limit"]
    if args.max_queries_per_issue is None:
        args.max_queries_per_issue = settings["max_queries_per_issue"]
    if args.prowlarr_timeout_seconds is None:
        args.prowlarr_timeout_seconds = settings["timeout_seconds"]
    if args.search_budget_seconds is None:
        args.search_budget_seconds = settings["search_budget_seconds"]
    if args.no_result_cooldown_hours is None:
        args.no_result_cooldown_hours = settings["no_result_cooldown_hours"]
    return settings


def prowlarr_provider_runtime_summary(args, settings=None):
    settings = settings if settings is not None else getattr(args, "prowlarr_provider_defaults", None)
    return {
        "enabled": bool(getattr(args, "prowlarr_provider_enabled", True)),
        "settings_source": getattr(args, "prowlarr_provider_settings_source", None) or (settings or {}).get("source") or "fallback",
        "defaults": dict(settings or {}),
        "applied": {
            "limit": getattr(args, "limit", None),
            "max_queries_per_issue": getattr(args, "max_queries_per_issue", None),
            "timeout_seconds": getattr(args, "prowlarr_timeout_seconds", None),
            "search_budget_seconds": getattr(args, "search_budget_seconds", None),
            "no_result_cooldown_hours": getattr(args, "no_result_cooldown_hours", None),
        },
    }


def normalize_extension(value):
    text = str(value or "").strip().lower()
    if not text:
        return ""
    return text if text.startswith(".") else f".{text}"


def normalized_extensions(values):
    out = set()
    for value in values or []:
        ext = normalize_extension(value)
        if ext:
            out.add(ext)
    return out


def quality_language_rules(refresh=False):
    global QUALITY_LANGUAGE_RULES_CACHE
    if QUALITY_LANGUAGE_RULES_CACHE is not None and not refresh:
        return QUALITY_LANGUAGE_RULES_CACHE
    rules = {
        "preferred_language": DEFAULT_QUALITY_LANGUAGE_RULES["preferred_language"],
        "pdf_allowed": DEFAULT_QUALITY_LANGUAGE_RULES["pdf_allowed"],
        "packs_allowed": DEFAULT_QUALITY_LANGUAGE_RULES["packs_allowed"],
        "pack_auto_approve_min_missing": DEFAULT_QUALITY_LANGUAGE_RULES["pack_auto_approve_min_missing"],
        "complete_pack_min_missing": DEFAULT_QUALITY_LANGUAGE_RULES["complete_pack_min_missing"],
        "allowed_extensions": set(DEFAULT_QUALITY_LANGUAGE_RULES["allowed_extensions"]),
        "blocked_release_terms": list(DEFAULT_QUALITY_LANGUAGE_RULES["blocked_release_terms"]),
        "source": DEFAULT_QUALITY_LANGUAGE_RULES["source"],
    }
    if inkdrop_state is not None:
        try:
            config = inkdrop_state.provider_config(INKDROP_STATE_DB, "quality_language_rules") or {}
        except Exception as exc:
            audit("quality_language_rules_load_failed", {"error": f"{type(exc).__name__}: {exc}"})
            config = {}
        if config:
            settings = config.get("settings") if isinstance(config.get("settings"), dict) else {}
            rules["source"] = config.get("source") or "inkdrop_state"
            preferred = str(settings.get("preferred_language") or rules["preferred_language"]).strip().lower()
            rules["preferred_language"] = preferred or "english"
            if bool_setting(settings, "allow_non_english", False):
                rules["preferred_language"] = "any"
            rules["pdf_allowed"] = bool_setting(settings, "pdf_allowed", rules["pdf_allowed"])
            rules["packs_allowed"] = bool_setting(settings, "packs_allowed", rules["packs_allowed"])
            rules["pack_auto_approve_min_missing"] = int_setting(
                settings,
                "pack_auto_approve_min_missing",
                rules["pack_auto_approve_min_missing"],
                1,
                999,
            )
            rules["complete_pack_min_missing"] = int_setting(
                settings,
                "complete_pack_min_missing",
                rules["complete_pack_min_missing"],
                1,
                999,
            )
            allowed = normalized_extensions(settings.get("allowed_manual_extensions") or [])
            if allowed:
                rules["allowed_extensions"] = allowed | {".cbz", ".cbr"}
            if rules["pdf_allowed"]:
                rules["allowed_extensions"].add(".pdf")
            else:
                rules["allowed_extensions"].discard(".pdf")
            if rules["packs_allowed"]:
                rules["allowed_extensions"].update(normalized_extensions(settings.get("pack_archives") or [".zip", ".rar", ".7z"]))
            blocked = settings.get("blocked_release_terms") or []
            if blocked:
                rules["blocked_release_terms"] = [str(term).strip().lower() for term in blocked if str(term or "").strip()]
    app_settings = inkdrop_quality_app_settings()
    preferred = app_settings.get("preferred_language")
    if preferred not in (None, ""):
        rules["preferred_language"] = str(preferred).strip().lower() or rules["preferred_language"]
        rules["source"] = "inkdrop_app_settings"
    if app_settings.get("allow_non_english") is not None:
        if boolish_value(app_settings.get("allow_non_english"), False):
            rules["preferred_language"] = "any"
        elif str(rules.get("preferred_language") or "").strip().lower() == "any":
            rules["preferred_language"] = str(preferred or DEFAULT_QUALITY_LANGUAGE_RULES["preferred_language"]).strip().lower() or "english"
        rules["source"] = "inkdrop_app_settings"
    if app_settings.get("allow_pdfs") is not None:
        rules["pdf_allowed"] = boolish_value(app_settings.get("allow_pdfs"), rules["pdf_allowed"])
        rules["source"] = "inkdrop_app_settings"
    if app_settings.get("allow_packs") is not None:
        rules["packs_allowed"] = boolish_value(app_settings.get("allow_packs"), rules["packs_allowed"])
        rules["source"] = "inkdrop_app_settings"
    preferred_extensions = normalized_extensions(app_settings.get("preferred_extensions") or [])
    if preferred_extensions:
        rules["allowed_extensions"] = preferred_extensions | {".cbz", ".cbr"}
        rules["source"] = "inkdrop_app_settings"
    if rules["pdf_allowed"]:
        rules["allowed_extensions"].add(".pdf")
    else:
        rules["allowed_extensions"].discard(".pdf")
    if rules["packs_allowed"]:
        rules["allowed_extensions"].update({".zip", ".rar", ".7z"} & set(DEFAULT_QUALITY_LANGUAGE_RULES["allowed_extensions"]))
    else:
        rules["allowed_extensions"].difference_update({".zip", ".rar", ".7z"})
    blocked_terms = app_settings.get("blocked_release_terms") or []
    if blocked_terms:
        rules["blocked_release_terms"] = [str(term).strip().lower() for term in blocked_terms if str(term or "").strip()]
    QUALITY_LANGUAGE_RULES_CACHE = rules
    return rules


def language_rule_requires_english(rules=None):
    rules = rules or quality_language_rules()
    preferred = str(rules.get("preferred_language") or "english").strip().lower()
    return preferred in {"english", "eng", "en"}


def english_status_allowed(english, rules=None):
    if not language_rule_requires_english(rules):
        return True
    return str((english or {}).get("status") or "") in {"confirmed_english", "likely_english"}


def result_file_extension(result):
    if isinstance(result, dict):
        values = [
            result.get("title"),
            result.get("filename"),
            result.get("source_path"),
            result.get("path"),
        ]
    else:
        values = [result]
    for value in values:
        text = str(value or "")
        match = re.search(r"(?i)(\.(?:cbz|cbr|pdf|epub|zip|rar|7z))(?:$|[\s\]\)\}])", text)
        if match:
            return match.group(1).lower()
        if re.search(r"(?i)(^|[\W_])pdf($|[\W_])", text):
            return ".pdf"
    return ""


def cover_only_release(result):
    if isinstance(result, dict):
        values = [
            result.get("title"),
            result.get("filename"),
            result.get("source_path"),
            result.get("path"),
        ]
    else:
        values = [result]
    text = " ".join(str(value or "") for value in values)
    return bool(re.search(r"(?i)covers?[\W_]*only", text))


def quality_rule_block_reason(result, rules=None, english=None):
    rules = rules or quality_language_rules()
    if english is None:
        english = english_confidence(result)
    if cover_only_release(result):
        return "cover_only_artifact"
    if language_rule_requires_english(rules):
        if inkdrop_language is not None:
            language = inkdrop_language.classify_release_language(
                title=(result or {}).get("title") if isinstance(result, dict) else result,
                path=(result or {}).get("path") or (result or {}).get("source_path") if isinstance(result, dict) else None,
                metadata=result if isinstance(result, dict) else None,
                preferred_languages=("en",),
                unknown_policy="allow_if_exact",
            )
            if language.get("blocked"):
                return f"wrong_language_source: {language.get('detail') or language.get('reason')}"
        acquire = load_acquire()
        if hasattr(acquire, "blocked_release_term_reason"):
            blocked_term = acquire.blocked_release_term_reason(result, rules.get("blocked_release_terms") or [])
        else:
            blocked_term = ""
        if blocked_term:
            return blocked_term.replace("blocked release term", "blocked_release_term", 1)
    if not english_status_allowed(english, rules):
        return "english_not_confirmed"
    ext = result_file_extension(result)
    if ext == ".pdf" and not rules.get("pdf_allowed", True):
        return "pdf_disabled_by_quality_rules"
    allowed_extensions = rules.get("allowed_extensions") or set()
    if ext and allowed_extensions and ext not in allowed_extensions:
        return f"extension_{ext.lstrip('.')}_disabled_by_quality_rules"
    return None


def quality_rule_summary(rules=None):
    rules = rules or quality_language_rules()
    return {
        "preferred_language": rules.get("preferred_language"),
        "pdf_allowed": bool(rules.get("pdf_allowed", True)),
        "packs_allowed": bool(rules.get("packs_allowed", True)),
        "pack_auto_approve_min_missing": int(rules.get("pack_auto_approve_min_missing") or 1),
        "complete_pack_min_missing": int(rules.get("complete_pack_min_missing") or 1),
        "allowed_extensions": sorted(rules.get("allowed_extensions") or []),
        "blocked_release_terms": sorted(rules.get("blocked_release_terms") or []),
        "source": rules.get("source"),
    }


def should_persist_review(reason):
    if str(reason or "") in QUEUE_MODE_REVIEW_REASONS:
        return True
    return truthy_env(MANUAL_REVIEW_PERSIST_ENV)


def read_json_file(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json_file(path, payload):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def review_id_for(item):
    raw = "|".join(
        str(value or "").lower()
        for value in (
            item.get("reason"),
            item.get("series"),
            item.get("issue"),
            item.get("query"),
            (item.get("candidate") or {}).get("title"),
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def load_manual_review_actions():
    data = read_json_file(MANUAL_REVIEW_ACTIONS_FILE, {}) or {}
    data.setdefault("ignored", [])
    data.setdefault("approved", [])
    data.setdefault("pack_approved", [])
    data.setdefault("pack_finished", [])
    data.setdefault("pack_handled_keys", {})
    data.setdefault("bad", [])
    data.setdefault("aliases", {})
    return data


def pack_handled_key_for_item(item, candidate=None):
    candidate = candidate if isinstance(candidate, dict) else (item.get("candidate") if isinstance(item.get("candidate"), dict) else {})
    pack_info = item.get("pack_info") if isinstance(item.get("pack_info"), dict) else {}
    pack_match = item.get("pack_match") if isinstance(item.get("pack_match"), dict) else {}
    title = normalize(candidate.get("title") or item.get("title") or item.get("query") or "")
    range_text = normalize(
        pack_info.get("summary")
        or pack_info.get("range")
        or pack_match.get("summary")
        or pack_match.get("range")
        or pack_match.get("pack_range")
        or ""
    )
    protocol = normalize(candidate.get("protocol") or item.get("protocol") or "")
    indexer = normalize(candidate.get("indexer") or candidate.get("indexerId") or item.get("indexer") or "")
    broad_manifest_pack = False
    try:
        broad_manifest_pack = bool(
            WEEKLY_COMICS_PACK_RE.search(candidate.get("title") or item.get("title") or item.get("query") or "")
            or (
                pack_match.get("coverage_source") == "pack_contents_filename"
                and pack_match.get("multi_series")
            )
        )
    except NameError:
        broad_manifest_pack = bool(
            pack_match.get("coverage_source") == "pack_contents_filename"
            and pack_match.get("multi_series")
        )
    series = "" if broad_manifest_pack else normalize(item.get("series") or pack_match.get("series") or "")
    if not series and not title:
        return None
    return hashlib.sha256("|".join([series, title, range_text, protocol, indexer]).encode("utf-8")).hexdigest()[:20]


def pack_handled_map(actions):
    handled = actions.get("pack_handled_keys", {})
    if isinstance(handled, dict):
        return handled
    if isinstance(handled, list):
        return {str(key): {"legacy": True} for key in handled}
    return {}


def load_pack_state():
    data = read_json_file(PACK_REVIEW_STATE_FILE, {}) or {}
    data.setdefault("active", None)
    data.setdefault("history", [])
    return data


def active_pack_blocks_new(review_id):
    state = load_pack_state()
    active = state.get("active")
    if not active:
        return False
    if active.get("review_id") == review_id:
        return False
    active_status = str(active.get("status") or "").lower()
    if active_status in {
        "auto_approved",
        "approved",
        "sent",
        "approved_sent",
        "downloading",
        "active_download",
        "completed_in_client",
        "completed_ready",
        "ready_to_import",
    }:
        return False
    try:
        import requests

        if not inkdrop_runtime_config.worker_http_callback_requested():
            public_state = (inkdrop_internal_jobs.run_pack_review_state().get("pack_state") or {})
            lifecycle = str(public_state.get("lifecycle_state") or "").lower()
            if lifecycle in {
                "downloading", "completed_in_client", "completed_ready",
                "ready_to_import", "active_download",
            }:
                return False
            if str(public_state.get("reason") or "").lower() == "uploading":
                return False
            return bool(public_state.get("blocks_new", True))
        response = requests.get(
            inkdrop_web_api_url("/api/pack-review/state"),
            headers=inkdrop_runtime_config.worker_auth_headers(required=True),
            timeout=10,
        )
        response.raise_for_status()
        public_state = (response.json() or {}).get("pack_state") or {}
        lifecycle = str(public_state.get("lifecycle_state") or "").lower()
        if lifecycle in {
            "downloading",
            "completed_in_client",
            "completed_ready",
            "ready_to_import",
            "active_download",
        }:
            return False
        if str(public_state.get("reason") or "").lower() == "uploading":
            return False
        return bool(public_state.get("blocks_new", True))
    except Exception as exc:
        if inkdrop_runtime_config.worker_http_callback_requested():
            print(
                f"InkDrop pack-state worker callback unavailable ({type(exc).__name__}); blocking new pack work.",
                file=sys.stderr,
            )
            return True
        return False


def save_pack_state(data):
    write_json_file(PACK_REVIEW_STATE_FILE, data)


def append_pending_pack(record):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with PENDING_PACKS_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def pending_pack_records(limit=1000):
    if not PENDING_PACKS_LOG.exists():
        return []
    records = []
    try:
        with PENDING_PACKS_LOG.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return records[-limit:]


def numeric_ts(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def recent_ts(ts, ttl=PACK_IN_FLIGHT_STALE_SECONDS):
    ts = numeric_ts(ts)
    return bool(ts and time.time() - ts < ttl)


def active_pack_matches(review_id):
    if not review_id:
        return False
    active = load_pack_state().get("active")
    return isinstance(active, dict) and str(active.get("review_id") or "") == str(review_id)


def pending_pack_record_is_current(record):
    if not isinstance(record, dict):
        return False
    status = str(record.get("status") or "").lower()
    if status in {"finished", "imported", "already_satisfied", "failed", "bad_archive"}:
        return False
    return recent_ts(record.get("created_at") or record.get("approved_at") or record.get("ts"))


def handled_pack_entry_is_current(entry):
    if entry is True:
        return True
    if not isinstance(entry, dict):
        return False
    status = str(entry.get("status") or "").lower()
    if status in {"finished", "imported", "already_satisfied"}:
        return True
    if status in {"auto_approved", "approved", "sent", "approved_sent", "downloading", "active_download"}:
        return recent_ts(entry.get("updated_at") or entry.get("approved_at") or entry.get("ts"))
    return recent_ts(entry.get("updated_at") or entry.get("approved_at") or entry.get("ts"))


def pack_is_handled(actions, review_id, item, candidate=None):
    if review_id in set(actions.get("pack_finished", [])):
        return True
    key = pack_handled_key_for_item(item, candidate)
    if active_pack_matches(review_id):
        return True
    handled = pack_handled_map(actions)
    if key:
        for record in reversed(pending_pack_records()):
            if pack_handled_key_for_item(record) == key and pending_pack_record_is_current(record):
                return True
        if key in handled:
            return handled_pack_entry_is_current(handled.get(key))
    if review_id in set(actions.get("pack_approved", [])):
        return True
    return False


def mark_pack_identity_in_flight(actions, review_id, item, candidate=None, status="auto_approved"):
    key = pack_handled_key_for_item(item or {}, candidate)
    if not key:
        return None
    handled = actions.setdefault("pack_handled_keys", {})
    if not isinstance(handled, dict):
        handled = pack_handled_map(actions)
        actions["pack_handled_keys"] = handled
    candidate = candidate or ((item or {}).get("candidate") or {})
    handled[key] = {
        "review_id": review_id,
        "series": (item or {}).get("series"),
        "issue": (item or {}).get("issue"),
        "title": candidate.get("title") or (item or {}).get("title") or (item or {}).get("query"),
        "status": status,
        "reason": "pack_sent_to_downloader",
        "updated_at": time.time(),
    }
    return key


def normalize(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def normalize_pack_archive_title(text):
    text = str(text or "")
    text = re.sub(r"\[[^\]]*(?:fixed|fix|repack|proper|v\d+)[^\]]*\]", " ", text, flags=re.I)
    text = re.sub(r"\b(?:fixed|fix|repack|proper)\b", " ", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()
    return normalize(strip_release_prefixes(text))


def pack_source_folder_title(source):
    text = str(source or "").strip()
    if not text:
        return ""
    path = PurePosixPath(text.replace("\\", "/"))
    parent = path.parent.name
    if parent and normalize(parent) not in {"", "comics", "downloads", "download"}:
        return parent
    return path.stem


def load_pack_bad_archive_counts():
    try:
        mtime = PACK_BAD_ARCHIVE_HISTORY_FILE.stat().st_mtime
    except OSError:
        PACK_BAD_ARCHIVE_HISTORY_CACHE["mtime"] = None
        PACK_BAD_ARCHIVE_HISTORY_CACHE["counts"] = {}
        return {}
    if PACK_BAD_ARCHIVE_HISTORY_CACHE.get("mtime") == mtime:
        return PACK_BAD_ARCHIVE_HISTORY_CACHE.get("counts") or {}
    data = read_json_file(PACK_BAD_ARCHIVE_HISTORY_FILE, {}) or {}
    rows = data.get("bad_archives") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        rows = []
    counts = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        source = row.get("source")
        pack_title = pack_source_folder_title(source)
        pack_key = normalize_pack_archive_title(pack_title)
        if not pack_key:
            continue
        entry = counts.setdefault(
            pack_key,
            {
                "bad_archive_count": 0,
                "pack_title": pack_title,
                "series": {},
                "sources": [],
                "last_seen_at": 0,
            },
        )
        entry["bad_archive_count"] += 1
        series_key = normalize(row.get("matched_series"))
        if series_key:
            entry["series"][series_key] = entry["series"].get(series_key, 0) + 1
        if source and len(entry["sources"]) < 3:
            entry["sources"].append(source)
        entry["last_seen_at"] = max(numeric_ts(entry.get("last_seen_at")), numeric_ts(row.get("ts")))
    PACK_BAD_ARCHIVE_HISTORY_CACHE["mtime"] = mtime
    PACK_BAD_ARCHIVE_HISTORY_CACHE["counts"] = counts
    return counts


def known_bad_pack_archive_history(item):
    candidate = item.get("candidate") if isinstance(item, dict) else {}
    candidate_title = (candidate or {}).get("title") or (item or {}).get("title") or (item or {}).get("query")
    candidate_key = normalize_pack_archive_title(candidate_title)
    series_key = normalize((item or {}).get("series"))
    if not candidate_key:
        return None
    if inkdrop_state is not None:
        try:
            db_bad = inkdrop_state.find_bad_source_candidate(
                INKDROP_STATE_DB,
                title=candidate_title,
                series=(item or {}).get("series"),
                provider=result_source(candidate),
                download_url_hash=result_download_url_hash(candidate),
            )
        except Exception:
            db_bad = None
        if db_bad:
            return {
                "reason": "known_bad_source_candidate",
                "candidate_title": candidate_title,
                "pack_title": db_bad.get("title"),
                "bad_source_candidate_id": db_bad.get("id"),
                "bad_archive_count": int(db_bad.get("failure_count") or 1),
                "sample_sources": [db_bad.get("source_path")] if db_bad.get("source_path") else [],
                "last_seen_at": db_bad.get("last_seen_at"),
            }
    best = None
    for pack_key, entry in load_pack_bad_archive_counts().items():
        if candidate_key != pack_key and not candidate_key.startswith(pack_key) and not pack_key.startswith(candidate_key):
            continue
        if series_key:
            series_counts = entry.get("series") or {}
            if series_counts and series_key not in series_counts:
                continue
        count = int(entry.get("bad_archive_count") or 0)
        if count < PACK_BAD_ARCHIVE_AUTO_BLOCK_MIN:
            continue
        if not best or count > int(best.get("bad_archive_count") or 0):
            best = {
                "reason": "known_bad_pack_archive_history",
                "candidate_title": candidate_title,
                "pack_title": entry.get("pack_title"),
                "bad_archive_count": count,
                "sample_sources": entry.get("sources") or [],
                "last_seen_at": entry.get("last_seen_at"),
            }
    return best


def redact_error(value):
    text = str(value or "")
    text = re.sub(r"apikey=[^&\s]+", "apikey=<redacted>", text, flags=re.I)
    return text


def unique(values):
    seen = set()
    out = []
    for value in values:
        value = re.sub(r"\s+", " ", str(value or "").replace(":", " ").replace("-", " ")).strip()
        key = normalize(value)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def stylized_x_title_variants(title):
    text = re.sub(r"\s+", " ", str(title or "").strip())
    if not text:
        return []
    variants = [text]
    if re.search(r"(?i)\b[a-z0-9]+\s+x\s+[a-z0-9]+\b", text):
        variants.append(re.sub(r"(?i)\s+x\s+", " x ", text))
        variants.append(re.sub(r"(?i)\s+x\s+", " X ", text))
        variants.append(re.sub(r"(?i)\s+x\s+", "×", text))
    if "×" in text:
        variants.append(re.sub(r"\s*×\s*", "×", text))
        variants.append(re.sub(r"\s*×\s*", " x ", text))
        variants.append(re.sub(r"\s*×\s*", " X ", text))
    return unique(variants)


def creator_possessive_title_variants(title):
    text = re.sub(r"\s+", " ", str(title or "").strip())
    if not text:
        return []
    match = re.match(
        r"^(?:[A-Z][\w.\-]+(?:\s+[A-Z][\w.\-]+){0,3})['’]s\s+(.+)$",
        text,
    )
    if not match:
        return []
    remainder = re.sub(r"\s+", " ", match.group(1).strip())
    if not remainder:
        return []
    words = re.findall(r"[a-z0-9]+", remainder.lower())
    if len(words) < 2 and not re.match(r"^\d", remainder):
        return []
    return [remainder]


def leading_article_title_variants(title):
    text = re.sub(r"\s+", " ", str(title or "").strip())
    if not text:
        return []
    variants = [text]
    stripped = re.sub(r"(?i)^(?:the|a|an)\s+", "", text).strip()
    if stripped and stripped != text:
        variants.append(stripped)
    return unique(variants)


def expanded_search_titles(title, alt_titles=()):
    values = []
    for name in [title, *(alt_titles or [])]:
        for article_variant in leading_article_title_variants(name):
            variants = stylized_x_title_variants(article_variant)
            values.extend(variants)
            for variant in variants:
                values.extend(creator_possessive_title_variants(variant))
    return unique(values)


def edition_like(title):
    return bool(re.search(r"\b(library|deluxe|omnibus|trade|paperback|hardcover|hc|tpb)\b", title or "", re.I))


def stripped_edition_title(title):
    text = re.sub(r"\b(?:library|deluxe|expanded|anniversary|collector'?s?)\s+edition\b", " ", title or "", flags=re.I)
    text = re.sub(r"\b(?:hardcover|paperback|trade paperback|tpb|hc)\b", " ", text, flags=re.I)
    return re.sub(r"\s+", " ", text.replace(":", " ").replace("-", " ")).strip()


def title_words_pattern(title):
    words = re.findall(r"[a-z0-9]+", (title or "").lower())
    return r"[\W_]+".join(re.escape(word) for word in words)


def title_words_patterns(title):
    return unique(
        pattern
        for value in leading_article_title_variants(title)
        for pattern in [title_words_pattern(value)]
        if pattern
    )


def strip_release_prefixes(raw_title):
    text = str(raw_title or "").strip()
    # Common release naming may start with one or more group tags before the
    # actual title. Keep the title match strict after removing only those tags.
    previous = None
    while previous != text:
        previous = text
        text = re.sub(r"^\s*\[[^\]]+\]\s*", "", text)
        text = re.sub(r"^\s*\([^)]+\)\s*", "", text)
    return text.strip()


def series_title_starts_release(title, raw_title):
    patterns = title_words_patterns(title)
    if not patterns:
        return False
    title_text = strip_release_prefixes(raw_title)
    return any(re.search(rf"^\s*{pattern}(?:[\W_]|$)", title_text, re.I) for pattern in patterns)


def related_subseries_title_blocker(title, raw_title, issue_title=None, issue_number=None, publisher=None):
    patterns = title_words_patterns(title)
    if not patterns:
        return ""
    title_text = strip_release_prefixes(raw_title)
    title_suffix = Path(str(title_text or "")).suffix.lower()
    title_stem = title_text[:-len(title_suffix)] if title_suffix in {".cbz", ".cbr", ".pdf"} else title_text
    title_text = title_stem
    terminal_image_imprint_re = re.compile(
        r"\s*\(\s*image\s*,\s*(?:19|20)\d{2}[-_.](?:0?[1-9]|1[0-2])\s*\)\s*$",
        re.I,
    )
    any_image_imprint_re = re.compile(
        r"\(\s*image\s*,\s*(?:19|20)\d{2}[-_.](?:0?[1-9]|1[0-2])\s*\)",
        re.I,
    )
    terminal_image_imprint = bool(terminal_image_imprint_re.search(title_stem))
    if any_image_imprint_re.search(title_stem) and not terminal_image_imprint:
        suffix = any_image_imprint_re.split(title_stem, maxsplit=1)[-1].strip()
        return "related subseries title tail after publisher imprint: " + (suffix or "unexpected suffix")
    if terminal_image_imprint:
        title_stem = terminal_image_imprint_re.sub("", title_stem).rstrip()
        exact_numbered_title = any(re.match(
            rf"^\s*{pattern}[\W_]+(?:#\s*|(?:issue|iss|no|number)\.?\s*)?0*\d+(?:\.\d+)?\s*$",
            title_stem,
            re.I,
        ) for pattern in patterns)
        if not exact_numbered_title:
            return "publisher imprint is not attached to an exact numbered series title"
        title_text = title_stem
    match = None
    for pattern in patterns:
        match = re.match(rf"^\s*{pattern}(?P<tail>.*)$", title_text, re.I)
        if match:
            break
    if not match:
        return ""
    if inkdrop_artifact_acceptance.trusted_issue_subtitle_matches_release(
        title,
        title_text,
        issue_title,
        issue_number,
    ):
        return ""
    tail_text = match.group("tail") or ""
    words = re.findall(r"[a-z0-9]+", tail_text.lower())
    if not words:
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
    pack_markers = {
        "complete",
        "pack",
        "set",
    }
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
    title_words = set(re.findall(r"[a-z0-9]+", str(title or "").lower()))
    if inkdrop_artifact_acceptance.benign_exact_title_publication_tail(
        tail_text,
        issue_number,
        stop_words=stop_words,
        edition_words=edition_words,
        publisher=publisher,
    ):
        return ""
    # A pack candidate's raw title plays the same role a pack folder name
    # plays for a local file path -- recognize the same organizational
    # descriptors (issue range, year range, publisher imprint) the source-side
    # blocker already does, so a search-time candidate for a real pack isn't
    # rejected before it ever gets the chance to download.
    if inkdrop_artifact_acceptance.benign_exact_title_organizational_folder_tail(tail_text):
        return ""
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


def issue_int(issue_number):
    try:
        return int(float(str(issue_number)))
    except ValueError:
        return None


def normalize_manga_number(value):
    match = re.search(r"\d+(?:\.\d+)?", str(value or ""))
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


def completed_numbers_by_volume(table, truth_model):
    if not COMPLETION_DB.exists():
        return {}
    con = sqlite3.connect(COMPLETION_DB, timeout=30)
    con.execute("pragma busy_timeout=30000")
    try:
        row = con.execute("select name from sqlite_master where type='table' and name=?", (table,)).fetchone()
        if not row:
            return {}
        unit_filter = "and unit_type in ('volume','pack')" if table == "manga_coverage" else ""
        rows = con.execute(
            f"""
            select kapowarr_volume_id, normalized_series, normalized_number
            from {table}
            where truth_model = ?
              and verification_status = 'kavita_verified'
              {unit_filter}
            """,
            (truth_model,),
        ).fetchall()
    finally:
        con.close()
    out = {}
    for volume_id, series, number in rows:
        if volume_id is not None:
            out.setdefault(("volume", int(volume_id)), set()).add(number)
        if series:
            out.setdefault(("series", series), set()).add(number)
    return out


def manga_coverage_range_completed_numbers():
    """Real per-series chapter numbers proven covered by an owned/verified volume's
    actual metadata range (manga_coverage.covered_chapter_numbers_json), keyed by
    ("series", normalized_series). Unlike completed_numbers_by_volume, this covers
    chapters whose own number never equals the volume's own number -- e.g. volume 5
    containing chapters 41-50. Only rows with a real range_source (never a filename
    guess) contribute, so an unmapped/unknown volume never suppresses anything.
    """
    if not COMPLETION_DB.exists():
        return {}
    con = sqlite3.connect(COMPLETION_DB, timeout=30)
    con.execute("pragma busy_timeout=30000")
    try:
        row = con.execute(
            "select name from sqlite_master where type='table' and name='manga_coverage'"
        ).fetchone()
        if not row:
            return {}
        rows = con.execute(
            """
            select normalized_series, covered_chapter_numbers_json
            from manga_coverage
            where truth_model = 'kavita_manga'
              and verification_status = 'kavita_verified'
              and unit_type in ('volume','pack')
              and range_source is not null
              and covered_chapter_numbers_json is not null
            """
        ).fetchall()
    finally:
        con.close()
    out = {}
    for series, covered_json in rows:
        if not series:
            continue
        try:
            numbers = json.loads(covered_json)
        except (TypeError, ValueError):
            continue
        if isinstance(numbers, list) and numbers:
            out.setdefault(("series", series), set()).update(str(number) for number in numbers)
    return out


PACK_COMPLETED_NUMBER_CACHE = {}


def completed_numbers_for_volume_id(volume_id):
    if volume_id is None:
        return set()
    try:
        volume_key = int(volume_id)
    except (TypeError, ValueError):
        return set()
    if volume_key in PACK_COMPLETED_NUMBER_CACHE:
        return PACK_COMPLETED_NUMBER_CACHE[volume_key]
    completed = set()
    for table, truth_model in (
        ("collection_completion", "kavita_collection"),
        ("manga_completion", "kavita_manga"),
        ("manga_coverage", "kavita_manga"),
    ):
        completed.update(completed_numbers_by_volume(table, truth_model).get(("volume", volume_key), set()))
    PACK_COMPLETED_NUMBER_CACHE[volume_key] = completed
    return completed


def suppress_completed_reading(rows):
    if not rows:
        return [], []
    manga_completed = completed_numbers_by_volume("manga_completion", "kavita_manga")
    manga_coverage = completed_numbers_by_volume("manga_coverage", "kavita_manga")
    manga_coverage_ranges = manga_coverage_range_completed_numbers()
    collection_completed = completed_numbers_by_volume("collection_completion", "kavita_collection")
    kept = []
    suppressed = []
    for row in rows:
        normalized = normalize_manga_number(row.get("calculated_issue_number") or row.get("issue_number"))
        if normalized:
            completed = collection_completed
            reason = "kavita_collection_verified"
            if is_manga_title(row.get("title"), row.get("publisher")):
                completed = {
                    key: set(values)
                    for key, values in collection_completed.items()
                }
                for key, values in manga_completed.items():
                    completed.setdefault(key, set()).update(values)
                for key, values in manga_coverage.items():
                    completed.setdefault(key, set()).update(values)
                for key, values in manga_coverage_ranges.items():
                    completed.setdefault(key, set()).update(values)
                reason = "kavita_manga_or_collection_verified"
            volume_done = set()
            if row.get("volume_id") not in (None, ""):
                try:
                    volume_done = completed.get(("volume", int(row["volume_id"])), set())
                except (TypeError, ValueError):
                    volume_done = set()
            series_done = completed.get(("series", normalize(row.get("title"))), set())
            if normalized in volume_done or normalized in series_done:
                suppressed.append({**row, "normalized_number": normalized, "suppression": reason})
                continue
        kept.append(row)
    return kept, suppressed


def manga_unit_model_for(row):
    if not COMPLETION_DB.exists():
        return None
    volume_id = row.get("volume_id")
    normalized_series = normalize(row.get("title"))
    con = sqlite3.connect(COMPLETION_DB, timeout=30)
    con.execute("pragma busy_timeout=30000")
    try:
        table = con.execute(
            "select name from sqlite_master where type='table' and name='manga_series_unit_model'"
        ).fetchone()
        if not table:
            return None
        match = None
        if volume_id is not None:
            match = con.execute(
                """
                select manga_unit_model
                from manga_series_unit_model
                where kapowarr_volume_id = ?
                order by updated_at desc
                limit 1
                """,
                (int(volume_id),),
            ).fetchone()
        if not match and normalized_series:
            match = con.execute(
                """
                select manga_unit_model
                from manga_series_unit_model
                where normalized_series = ?
                order by updated_at desc
                limit 1
                """,
                (normalized_series,),
            ).fetchone()
        if match:
            return str(match[0] or "").strip().lower() or None
    finally:
        con.close()
    return None


def pending_entries():
    latest = {}
    if not PENDING_IMPORTS_LOG.exists():
        return set(), []
    with PENDING_IMPORTS_LOG.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            status = row.get("status")
            for key in ("query", "title"):
                value = row.get(key)
                norm = normalize(value)
                if norm:
                    latest[norm] = {"status": status, "value": str(value)}
    out = {key for key, row in latest.items() if row.get("status") == "sent"}
    raw = [row["value"] for row in latest.values() if row.get("status") == "sent"]
    return out, raw


def issue_has_pending(title, issue_number, raw_pending):
    variants = {normalize(q) for q in query_variants(title, issue_number)}
    for value in raw_pending:
        if normalize(value) in variants or acceptable_result(title, issue_number, {"title": value}):
            return True
    return False


def load_cache():
    if not CACHE_FILE.exists():
        return {"no_result": {}, "bad_results": {}}
    # Say so out loud. Starting from an empty cache is the right recovery, but
    # it silently throws away every no_result and bad_results memory the
    # acquisition lanes have built up, and the only visible symptom is a
    # sudden burst of re-tried dead ends. ValueError rather than
    # JSONDecodeError because a file that is not valid UTF-8 raises
    # UnicodeDecodeError, which is a ValueError but not a JSONDecodeError.
    try:
        cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"InkDrop could not read {CACHE_FILE}, starting from an empty cache: {exc}", flush=True)
        return {"no_result": {}, "bad_results": {}}
    if not isinstance(cache, dict):
        print(
            f"InkDrop ignored {CACHE_FILE}, starting from an empty cache: "
            f"expected an object, found {type(cache).__name__}",
            flush=True,
        )
        return {"no_result": {}, "bad_results": {}}
    cache.setdefault("no_result", {})
    cache.setdefault("bad_results", {})
    return cache


def save_cache(cache):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(CACHE_FILE)


def qbit_incomplete_series(acquire, series_names, timeout_seconds=None):
    if truthy_env("INKDROP_SKIP_QBIT_ACTIVE_CHECK"):
        audit("qbit_active_check_skipped", {"reason": "INKDROP_SKIP_QBIT_ACTIVE_CHECK"})
        return set()
    timeout_seconds = max(1.0, min(float(timeout_seconds or env_float("INKDROP_QBIT_STATUS_TIMEOUT_SECONDS", 2.0)), 30.0))
    try:
        qbit = acquire.load_qbit_settings()
        import requests

        target_categories = {normalize(qbit.get("comics_category")), normalize("comics"), normalize("kapowarr")}
        target_save_paths = {
            str(path or "").strip().lower().rstrip("/")
            for path in (qbit.get("comics_save_path"), "/downloads/comics")
            if str(path or "").strip()
        }
        session = requests.Session()
        inkdrop_qbittorrent_auth.authenticate_settings(session, qbit, timeout=(2.0, timeout_seconds))
        resp = session.get(qbit["host"] + "/api/v2/torrents/info", timeout=(2.0, timeout_seconds))
        resp.raise_for_status()
        torrents = resp.json()
    except Exception as exc:
        audit(
            "qbit_active_check_failed",
            {
                "error": f"{type(exc).__name__}: {redact_error(exc)}",
                "timeout_seconds": timeout_seconds,
                "series_count": len(series_names or ()),
            },
        )
        return set()
    active = set()
    for tor in torrents:
        category = normalize(tor.get("category"))
        tags = {
            part.strip().lower()
            for part in str(tor.get("tags") or "").split(",")
            if part.strip()
        }
        save_path = str(tor.get("save_path") or tor.get("content_path") or "").lower().rstrip("/")
        save_path_match = bool(save_path and any(save_path.startswith(path) for path in target_save_paths if path))
        if (
            category not in target_categories
            and not (tags & QBIT_BROAD_TAGS)
            and not save_path_match
        ):
            continue
        name = normalize(tor.get("name"))
        progress = float(tor.get("progress") or 0)
        if progress >= 0.999:
            continue
        for series in series_names:
            if normalize(series) in name:
                active.add(series)
    return active


def sync_inkdrop_state_for_missing():
    if inkdrop_state is None or not INKDROP_STATE_DB.exists():
        return {"ok": False, "reason": "inkdrop_state_unavailable"}
    if queue_mode_enabled():
        return {
            "ok": True,
            "auto_synced": False,
            "auto_sync_reason": "queue_mode_worker_uses_current_state",
            "db_path": str(INKDROP_STATE_DB),
        }
    try:
        return inkdrop_state.sync_state_if_stale(STATE_DIR, INKDROP_STATE_DB)
    except Exception as exc:
        audit("inkdrop_state_missing_sync_failed", {"error": f"{type(exc).__name__}: {exc}"})
        return {"ok": False, "error": str(exc)}


def inkdrop_monitored_series_names():
    sync_inkdrop_state_for_missing()
    if not INKDROP_STATE_DB.exists():
        return tuple()
    con = inkdrop_db.open_connection(
        INKDROP_STATE_DB,
        readonly=True,
        operation="missing_acquire_series_names",
    )
    try:
        rows = con.execute(
            """
            select distinct s.title
            from queue_items q
            join series s on s.id = q.series_id
            where q.active = 1
              and q.state in ('queued','searching')
              and s.title is not null
            order by lower(s.title)
            """
        ).fetchall()
        return tuple(row[0] for row in rows)
    except Exception as exc:
        audit("inkdrop_series_names_failed", {"error": f"{type(exc).__name__}: {exc}"})
        return tuple()
    finally:
        con.close()


def monitored_series_names():
    if queue_mode_enabled():
        return inkdrop_monitored_series_names()
    return tuple()


def library_path_roots():
    mappings = [
        (COMICS_CONTAINER_ROOT, COMICS_HOST_ROOT),
        (Path("/data/comics"), COMICS_HOST_ROOT),
        (COMICS_HOST_ROOT, COMICS_HOST_ROOT),
        (MANGA_CONTAINER_ROOT, MANGA_HOST_ROOT),
        (Path("/data/manga"), MANGA_HOST_ROOT),
        (MANGA_HOST_ROOT, MANGA_HOST_ROOT),
    ]
    if inkdrop_state is not None:
        try:
            for kavita_root, host_root in inkdrop_state.kavita_path_mappings(INKDROP_STATE_DB):
                mappings.append((Path(str(kavita_root)), Path(str(host_root))))
                mappings.append((Path(str(host_root)), Path(str(host_root))))
        except Exception as exc:
            audit("inkdrop_path_mappings_failed", {"error": f"{type(exc).__name__}: {exc}"})
    out = []
    seen = set()
    for container_root, host_root in mappings:
        key = (str(container_root).replace("\\", "/").rstrip("/"), str(host_root).replace("\\", "/").rstrip("/"))
        if key in seen:
            continue
        seen.add(key)
        out.append((Path(key[0]), Path(key[1])))
    return out


def folder_is_safe(folder):
    if not folder:
        return False
    folder = str(folder).replace("\\", "/")
    normalized = folder.rstrip("/")
    for container_root, host_root in library_path_roots():
        container = str(container_root).replace("\\", "/").rstrip("/")
        host = str(host_root).replace("\\", "/").rstrip("/")
        if normalized == container or normalized.startswith(container + "/"):
            rel = normalized[len(container):].lstrip("/")
            return (host_root / rel).exists()
        if normalized == host or normalized.startswith(host + "/"):
            return Path(normalized).exists()
    return False


def inkdrop_queue_missing_issues(series_names=(), fresh_days=None):
    sync_inkdrop_state_for_missing()
    if not INKDROP_STATE_DB.exists():
        return []
    filters = [
        "q.active = 1",
        "q.state in ('queued','searching')",
    ]
    params = []
    if series_names:
        placeholders = ",".join("?" for _ in series_names)
        filters.append(f"s.title in ({placeholders})")
        params.extend(series_names)
    if fresh_days:
        cutoff = time.time() - fresh_days * 86400
        filters.append("coalesce(q.updated_at, q.created_at, 0) >= ?")
        params.append(cutoff)
    con = inkdrop_db.open_connection(
        INKDROP_STATE_DB,
        readonly=True,
        operation="missing_acquire_queue_rows",
    )
    try:
        rows = con.execute(
            f"""
            select q.id as queue_id, q.state as queue_state, q.current_source,
                   q.query, q.last_event, q.display_phase, q.retry_after, q.retry_after_iso,
                   q.raw_json as queue_raw_json,
                   s.id as series_id, s.title, s.publisher, s.year, s.media_type,
                   s.source as series_source, s.metadata_provider, s.metadata_id,
                   s.kapowarr_id,
                   i.id as state_issue_id, i.issue_number, i.normalized_number,
                   i.title as issue_title, i.release_date,
                   i.metadata_provider as issue_metadata_provider,
                   i.metadata_id as issue_metadata_id, i.kapowarr_issue_id
            from queue_items q
            join series s on s.id = q.series_id
            left join issues i on i.id = q.issue_id
            where {" and ".join(filters)}
            order by lower(s.title), i.normalized_number, q.updated_at desc
            """,
            params,
        ).fetchall()
    except Exception as exc:
        audit("inkdrop_queue_missing_issues_failed", {"error": f"{type(exc).__name__}: {exc}"})
        return []
    finally:
        con.close()
    out = []
    for row in rows:
        item = inkdrop_missing_row_from_queue_record(row)
        if item:
            out.append(item)
    return out


def inkdrop_missing_row_from_queue_record(row):
    row = row if isinstance(row, dict) else dict(row or {})
    raw = json.loads(row.get("queue_raw_json") or "{}") if row.get("queue_raw_json") else {}
    kapowarr_id = row.get("kapowarr_id") if row.get("kapowarr_id") not in (None, "") else raw.get("kapowarr_id")
    folder = raw.get("folder")
    issue_number = row.get("issue_number") or raw.get("issue") or raw.get("chapter")
    if not row.get("title") or not issue_number:
        return None
    # Joined series/issue columns are the durable identity boundary.  Queue
    # raw_json is caller-controlled legacy context and must not relabel them.
    metadata_provider = str(row.get("metadata_provider") or "").strip().lower()
    issue_metadata_provider = str(row.get("issue_metadata_provider") or "").strip().lower()
    explicit_unit_type = raw.get("unitType") or raw.get("unit_type")
    durable_identity = bool(
        row.get("state_issue_id")
        and (issue_metadata_provider or metadata_provider)
    )
    trusted_identity = inkdrop_manual_search.trusted_target_unit_identity(
        {
            # Explicit raw queue fields are only a fallback for records that do
            # not have a durable metadata-owned issue identity.
            "unit_type": None if durable_identity else explicit_unit_type,
            "media_type": row.get("media_type") or raw.get("media_type"),
            "unit_number": issue_number,
            "series_metadata_provider": metadata_provider,
            "issue_metadata_provider": issue_metadata_provider,
            "target_unit_metadata_trusted": durable_identity,
        }
    )
    unit_type = trusted_identity.get("unit_type") or (None if durable_identity else explicit_unit_type)
    return {
        "volume_id": kapowarr_id,
        "title": row.get("title"),
        "alt_title": raw.get("alt_title"),
        "publisher": row.get("publisher") or raw.get("watch_publisher"),
        "year": row.get("year") or raw.get("watch_year"),
        "special_version": raw.get("special_version"),
        "folder": folder,
        "issue_id": row.get("kapowarr_issue_id") or row.get("issue_metadata_id") or row.get("state_issue_id"),
        "issue_number": issue_number,
        "calculated_issue_number": raw.get("calculated_issue_number") or issue_number,
        "issue_title": row.get("issue_title") or raw.get("issue_title"),
        "release_date": row.get("release_date") or raw.get("release_date") or raw.get("date") or raw.get("publishAt"),
        "comicvine_id": row.get("metadata_id") if str(row.get("metadata_provider") or "").lower() == "comicvine" else raw.get("comicvine_id"),
        "mangadex_id": row.get("metadata_id") if metadata_provider == "mangadex" else raw.get("mangadex_id") or raw.get("mangadexId"),
        "mangadex_chapter_id": raw.get("mangadex_chapter_id") or raw.get("chapterId") or raw.get("chapter_id") or (row.get("issue_metadata_id") if issue_metadata_provider == "mangadex" else None),
        "search_query": raw.get("searchQuery") or row.get("query"),
        "unit_type": unit_type,
        "volume_number": (trusted_identity.get("volume_number") or raw.get("volume_number") or raw.get("volume")) if unit_type == "volume" else None,
        "chapter": raw.get("chapter") if unit_type == "chapter" else None,
        "chapter_volume": raw.get("volume") if unit_type == "chapter" else None,
        "media_type": row.get("media_type") or raw.get("media_type"),
        "issue_metadata_provider": issue_metadata_provider,
        "translated_language": raw.get("translatedLanguage") or raw.get("translated_language"),
        "queue_id": row.get("queue_id"),
        "queue_state": row.get("queue_state"),
        "current_source": row.get("current_source"),
        "query": row.get("query"),
        "last_event": row.get("last_event") or raw.get("last_event"),
        "display_phase": row.get("display_phase") or raw.get("display_phase"),
        "retry_after": row.get("retry_after"),
        "retry_after_iso": row.get("retry_after_iso"),
        "series_source": row.get("series_source"),
        "metadata_provider": row.get("metadata_provider") or raw.get("metadata_provider"),
        "metadata_id": row.get("metadata_id"),
        "inkdrop_queue_row": True,
    }


def missing_issues(series_names, fresh_days=None):
    if queue_mode_enabled():
        return inkdrop_queue_missing_issues(series_names, fresh_days=fresh_days)
    return []


def missing_source_summary(rows):
    inkdrop_rows = sum(1 for row in rows if row.get("inkdrop_queue_row"))
    source = "inkdrop_queue" if inkdrop_rows else "inkdrop_queue_empty"
    return {
        "missing_source": source,
        "inkdrop_queue_rows": inkdrop_rows,
        "queue_mode": queue_mode_enabled(),
    }


def row_output_context(row):
    return {
        "volume_id": row.get("volume_id"),
        "kapowarr_id": row.get("volume_id"),
        "issue_id": row.get("issue_id"),
        "queue_id": row.get("queue_id"),
        "comicvine_id": row.get("comicvine_id"),
        "mangadex_id": row.get("mangadex_id"),
        "mangadex_chapter_id": row.get("mangadex_chapter_id"),
        "metadata_provider": row.get("metadata_provider"),
        "metadata_id": row.get("metadata_id"),
        "series_source": row.get("series_source"),
        "inkdrop_queue_row": bool(row.get("inkdrop_queue_row")),
        "year": row.get("year"),
        "publisher": row.get("publisher"),
    }


def native_inkdrop_provider(row):
    provider = str((row or {}).get("metadata_provider") or "").strip().lower()
    source = str((row or {}).get("series_source") or "").strip().lower()
    metadata_id = (row or {}).get("metadata_id")
    if provider in {"kapowarr", "watch", "manual", ""}:
        return ""
    if not (row or {}).get("inkdrop_queue_row"):
        return ""
    if provider in {"comicvine", "mangadex"}:
        return provider
    return provider if metadata_id not in (None, "") and source != "kapowarr" else ""


def row_can_search_without_folder(row):
    if not row or row.get("folder"):
        return False
    provider = native_inkdrop_provider(row)
    if provider == "comicvine":
        return bool(row.get("comicvine_id") or row.get("metadata_id"))
    if provider == "mangadex":
        return bool(row.get("mangadex_id") or row.get("metadata_id"))
    return bool(provider)


def is_manga_title(title, publisher=None):
    title_key = normalize(title)
    publisher_key = normalize(publisher)
    if title_key in MANGA_SERIES:
        return True
    return any(pub in publisher_key for pub in MANGA_PUBLISHERS)


def row_is_manga(row):
    row = row if isinstance(row, dict) else {}
    media_type = str(row.get("media_type") or row.get("mediaType") or "").strip().lower()
    return media_type in {"manga", "manhwa", "manhua", "webtoon"} or is_manga_title(
        row.get("title"), row.get("publisher")
    )


def add_year_variants(variants, year):
    values = list(variants or [])
    year_text = str(year or "").strip()
    if not re.fullmatch(r"(?:19|20)\d{2}", year_text):
        return unique(values)
    return unique([f"{value} {year_text}" for value in values] + values)


def year_from_value(value):
    match = re.search(r"\b((?:19|20)\d{2})\b", str(value or ""))
    return match.group(1) if match else ""


def row_issue_year(row):
    row = row if isinstance(row, dict) else {}
    for key in ("release_date", "issue_date", "date", "publishAt"):
        year = year_from_value(row.get(key))
        if year:
            return year
    return ""


def row_query_year(row, is_manga=False, unit_model=None):
    issue_year = row_issue_year(row)
    if issue_year:
        return issue_year
    model = str(unit_model or row.get("unit_type") or row.get("unitType") or "").strip().lower()
    if is_manga or model in {"volume", "pack", "mixed_volume_preferred", "mixed_chapter_preferred"}:
        return None
    return row.get("year")


def issue_search_token(issue_number):
    text = str(issue_number or "").strip()
    if not text:
        return ""
    match = re.search(r"\d+(?:\.\d+)?", text)
    if match:
        number = match.group(0)
        return number.rstrip("0").rstrip(".") if "." in number else str(int(number))
    return text


def padded_issue_tokens(token):
    try:
        value = float(str(token))
    except (TypeError, ValueError):
        return []
    if not value.is_integer() or value < 0:
        return []
    n = int(value)
    return [f"{n:03d}", f"{n:02d}", str(n)]


def stale_series_year_preferred_query(value, row, is_manga=False, unit_model=None):
    value = str(value or "").strip()
    if not value:
        return False
    row = row if isinstance(row, dict) else {}
    model = str(unit_model or row.get("unit_type") or row.get("unitType") or "").strip().lower()
    if not (is_manga or model in {"volume", "pack", "mixed_volume_preferred", "mixed_chapter_preferred"}):
        return False
    if row_issue_year(row):
        return False
    series_year = str(row.get("year") or "").strip()
    if not re.fullmatch(r"(?:19|20)\d{2}", series_year):
        return False
    if not re.search(rf"\b{re.escape(series_year)}\s*$", value):
        return False
    token = issue_search_token(row.get("issue_number") or row.get("issue") or row.get("chapter"))
    if not token:
        return False
    issue_tokens = [token, *padded_issue_tokens(token)]
    token_pattern = "|".join(re.escape(t) for t in unique(issue_tokens) if t)
    if not token_pattern:
        return False
    return bool(re.search(rf"\b(?:{token_pattern})\b\s+{re.escape(series_year)}\s*$", value, flags=re.I))


def query_variants(title, issue_number, is_manga=False, alt_titles=(), unit_model=None, year=None):
    token = issue_search_token(issue_number)
    if not token:
        return []
    padded = padded_issue_tokens(token)
    n = issue_int(token) if padded else None
    titles = expanded_search_titles(title, alt_titles)
    if is_manga or is_manga_title(title):
        variants = []
        for name in titles:
            if (unit_model or "").lower() in {"chapter", "mixed_chapter_preferred"}:
                variants.extend([
                    f"{name} Chapter {token}",
                    f"{name} Ch. {token}",
                    f"{name} {token}",
                ])
                for padded_token in padded[:1]:
                    variants.extend([f"{name} chapter {padded_token}", f"{name} ch {padded_token}"])
            elif (unit_model or "").lower() in {"volume", "pack", "mixed_volume_preferred"}:
                variants.extend([
                    f"{name} Vol. {token}",
                    f"{name} Volume {token}",
                    f"{name} {token}",
                ])
                for padded_token in padded:
                    variants.extend([f"{name} v{padded_token}", f"{name} {padded_token}"])
            else:
                variants.extend([
                    f"{name} Vol. {token}",
                    f"{name} Volume {token}",
                    f"{name} Chapter {token}",
                    f"{name} Ch. {token}",
                    f"{name} {token}",
                ])
                for padded_token in padded:
                    variants.extend([f"{name} v{padded_token}", f"{name} {padded_token}"])
        return add_year_variants(unique(variants), year)
    variants = []
    for name in titles:
        variants.append(f"{name} {token}")
        variants.extend(f"{name} {padded_token}" for padded_token in padded[:1])
        if n == 1 and edition_like(name):
            base = stripped_edition_title(name)
            variants.extend([name, base])
    return add_year_variants(unique(variants), year)


def query_variants_for_row(row, is_manga=False, unit_model=None):
    row = row if isinstance(row, dict) else {}
    title = row.get("title")
    issue_number = row.get("issue_number")
    preferred = []
    stale_preferred = []
    for value in (row.get("search_query"), row.get("query")):
        value = str(value or "").strip()
        if not value:
            continue
        if stale_series_year_preferred_query(value, row, is_manga=is_manga, unit_model=unit_model):
            stale_preferred.append(value)
        else:
            preferred.append(value)
    generated = query_variants(
        title,
        issue_number,
        is_manga=is_manga,
        alt_titles=[row.get("alt_title")],
        unit_model=unit_model,
        year=row_query_year(row, is_manga=is_manga, unit_model=unit_model),
    )
    return unique([*preferred, *generated, *stale_preferred])


def row_unit_model(row, cache=None):
    row = row if isinstance(row, dict) else {}
    unit_type = str(row.get("unit_type") or row.get("unitType") or "").strip().lower()
    if unit_type in {"volume", "pack"}:
        return "volume"
    if not row_is_manga(row):
        return None
    key = str(row.get("volume_id") or normalize(row.get("title")) or id(row))
    if cache is not None and key in cache:
        return cache[key]
    model = row.get("manga_unit_model") or manga_unit_model_for(row)
    # Preserve the mixed-chapter policy only across a durable chapter wanted
    # boundary. It enables exact chapter acquisition; volume coverage remains
    # a separate manifest-proven future concern. A volume row stays volume-only.
    if unit_type in {"chapter", "oneshot"}:
        model = "mixed_chapter_preferred" if model == "mixed_chapter_preferred" else "chapter"
    if cache is not None:
        cache[key] = model
    return model


def limited_queries(queries, args):
    values = list(queries or [])
    limit = int(getattr(args, "max_queries_per_issue", 0) or 0)
    if limit > 0:
        return values[:limit]
    return values


COLLECTED_EDITION_RE = re.compile(
    r"\b(?:book|books|tpb|trade\s+paperback|hardcover|hc|v|vol(?:ume)?)\.?\s*0*\d{1,3}\b",
    re.I,
)
WEEKLY_COMICS_PACK_RE = re.compile(
    r"\b(?:weekly[\W_]+comics?[\W_]+pack|comics?[\W_]+weekly[\W_]+releases|(?:dc|image|indie)[\W_]+week\+?)\b"
    r"|\b\d{4}[\W_]+\d{2}[\W_]+\d{2}[\W_]+(?:dc|image|indie)?[\W_]*(?:week|weekly)\b",
    re.I,
)
PACK_CONTENT_TEXT_KEYS = (
    "title",
    "fileName",
    "filename",
    "description",
    "summary",
    "details",
    "nfo",
    "info",
    "files",
)
COMIC_FILE_EXTENSION_RE = re.compile(r"\.(?:cbz|cbr|pdf|epub|zip|rar|7z)\b", re.I)


def is_pack_result(raw_title):
    text = str(raw_title or "")
    return bool(
        re.search(
            r"\b(?:v|vol(?:ume)?\.?|ch(?:apter)?\.?|issue\s*)?0*\d{1,4}\s*[-–]\s*(?:v|vol(?:ume)?\.?|ch(?:apter)?\.?|issue\s*)?0*\d{1,4}\b|\(\s*\d{1,4}\s*[-–]\s*\d{1,4}\s*\+?\s*\)|\b(?:complete|omnibus|compendium|collection)\b",
            text,
            re.I,
        )
        or COLLECTED_EDITION_RE.search(text)
        or WEEKLY_COMICS_PACK_RE.search(text)
    )


def candidate_payload(result):
    keep = {
        "title",
        "indexer",
        "indexerId",
        "source",
        "protocol",
        "seeders",
        "leechers",
        "size",
        "publishDate",
        "downloadUrl",
        "download_url",
        "downloadUrlHash",
        "download_url_hash",
        "url_hash",
        "magnetUrl",
        "guid",
        "infoUrl",
        "infoHash",
        "fileName",
        "filename",
        "source_unit",
    }
    payload = {key: result.get(key) for key in keep if result.get(key) is not None}
    if not payload.get("downloadUrl"):
        if str(payload.get("protocol") or "").lower() == "torrent":
            payload["downloadUrl"] = payload.get("magnetUrl") or payload.get("guid")
        else:
            payload["downloadUrl"] = payload.get("download_url")
    return {key: value for key, value in payload.items() if value is not None}


def normalized_number(value):
    text = str(value or "").strip()
    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return normalize(text)
    number = match.group(0)
    if "." in number:
        return number.rstrip("0").rstrip(".")
    return str(int(number)).zfill(3)


def release_title(result_or_title):
    if isinstance(result_or_title, dict):
        return str(result_or_title.get("title") or result_or_title.get("query") or "")
    return str(result_or_title or "")


def result_source(result_or_title):
    if isinstance(result_or_title, dict):
        return str(result_or_title.get("indexer") or result_or_title.get("source") or result_or_title.get("client") or "")
    return ""


def result_protocol(result_or_title):
    if isinstance(result_or_title, dict):
        return str(result_or_title.get("protocol") or "")
    return ""


def result_download_url_hash(result_or_title):
    if not isinstance(result_or_title, dict):
        return ""
    for key in ("downloadUrlHash", "download_url_hash", "url_hash"):
        if result_or_title.get(key):
            return str(result_or_title.get(key))
    url = result_or_title.get("downloadUrl") or result_or_title.get("download_url") or result_or_title.get("url")
    if url:
        return hashlib.sha256(str(url).encode("utf-8")).hexdigest()
    return ""


def result_source_memory_source(result_or_title):
    if isinstance(result_or_title, dict):
        if any(result_or_title.get(key) for key in ("indexer", "indexerId", "downloadUrl", "download_url", "magnetUrl", "guid", "infoUrl")):
            return "prowlarr"
        return result_source(result_or_title) or "download_client"
    return "prowlarr"


def result_source_memory_path(result_or_title):
    if not isinstance(result_or_title, dict):
        return ""
    for key in ("infoUrl", "guid", "downloadUrl", "download_url", "magnetUrl", "url"):
        value = str(result_or_title.get(key) or "").strip()
        if value:
            return value
    return ""


def record_inkdrop_queue_attempt(
    row,
    status,
    reason,
    *,
    query=None,
    candidate=None,
    outcome=None,
    dry_run=False,
    source="prowlarr",
    extra=None,
):
    if dry_run or inkdrop_state is None or not row.get("queue_id"):
        return None
    candidate = candidate if isinstance(candidate, dict) else {}
    outcome = outcome if isinstance(outcome, dict) else {}
    protocol = candidate.get("protocol") or outcome.get("protocol")
    nzo_ids = outcome.get("nzo_ids")
    nzo_id = outcome.get("nzo_id") or (nzo_ids[0] if isinstance(nzo_ids, list) and nzo_ids else None)
    client_hashes = outcome.get("hashes")
    client_hash = outcome.get("client_hash") or outcome.get("hash") or (client_hashes[0] if isinstance(client_hashes, list) and client_hashes else None)
    client_id = outcome.get("client_id") or client_hash or nzo_id
    attempt = {
        "source": source,
        "provider": candidate.get("indexer") or candidate.get("indexerId") or source,
        "indexer": candidate.get("indexer") or candidate.get("indexerId"),
        "protocol": protocol,
        "download_client": outcome.get("download_client"),
        "client_id": client_id,
        "client_hash": client_hash,
        "nzo_id": nzo_id,
        "category": outcome.get("category"),
        "save_path": outcome.get("save_path"),
        "download_url_hash": result_download_url_hash(candidate),
        "status": status,
        "reason": reason,
        "query": query,
        "title": release_title(candidate),
        "score": candidate.get("seeders") or candidate.get("score"),
        "series": row.get("title"),
        "issue": row.get("issue_number"),
        "kind": "missing_acquire",
        "ts": time.time(),
    }
    if extra:
        attempt.update({key: value for key, value in dict(extra).items() if value is not None})
    try:
        return inkdrop_state.record_queue_source_attempt(INKDROP_STATE_DB, row.get("queue_id"), attempt)
    except Exception as exc:
        audit(
            "inkdrop_queue_attempt_record_failed",
            {
                "queue_id": row.get("queue_id"),
                "series": row.get("title"),
                "issue": row.get("issue_number"),
                "status": status,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
        return None


def sync_bad_source_candidates_from_history(dry_run=False):
    if dry_run:
        return {"ok": True, "skipped": True, "reason": "dry_run"}
    if inkdrop_state is None:
        return {"ok": False, "reason": "inkdrop_state_module_missing"}
    try:
        return inkdrop_state.sync_bad_source_candidates_from_pack_history(STATE_DIR, INKDROP_STATE_DB)
    except Exception as exc:
        audit(
            "bad_source_candidate_sync_failed",
            {"error": f"{type(exc).__name__}: {exc}"},
        )
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def bad_result_key(series, issue_number, result_or_title):
    return "|".join(
        [
            normalize(series),
            normalized_number(issue_number),
            normalize(release_title(result_or_title)),
        ]
    )


def bad_result_strong_key(series, issue_number, result_or_title):
    return "|".join(
        [
            normalize(series),
            normalized_number(issue_number),
            normalize(release_title(result_or_title)),
            normalize(result_source(result_or_title)),
            normalize(result_protocol(result_or_title)),
            result_download_url_hash(result_or_title),
        ]
    )


DURABLE_BAD_SOURCE_REASONS = {
    "bad_archive",
    "failed_download_duplicate_nzb",
    "false_positive",
    "known_bad_pack_archive_history",
    "known_bad_source_candidate",
    "sab_not_complete",
    "sab_url_fetch_failed",
    "wrong_series_or_subseries",
}


def durable_bad_source_reason(reason):
    text = str(reason or "").strip().lower()
    return text in DURABLE_BAD_SOURCE_REASONS


def record_durable_bad_source_result(series, issue_number, result_or_title, reason):
    if inkdrop_state is None or not durable_bad_source_reason(reason):
        return None
    title = release_title(result_or_title)
    download_url_hash = result_download_url_hash(result_or_title)
    source_path = result_source_memory_path(result_or_title)
    if not title and not download_url_hash and not source_path:
        return None
    payload = {
        "source": result_source_memory_source(result_or_title),
        "provider": result_source(result_or_title),
        "protocol": result_protocol(result_or_title),
        "series": series,
        "title": title,
        "download_url_hash": download_url_hash,
        "source_path": source_path,
        "reason": str(reason or "bad_source_candidate"),
        "raw": {
            "kind": "prowlarr_result_failure",
            "series": series,
            "issue": str(issue_number or ""),
            "candidate": candidate_payload(result_or_title if isinstance(result_or_title, dict) else {"title": title}),
        },
        "seen_at": time.time(),
    }
    try:
        return inkdrop_state.record_bad_source_candidate(INKDROP_STATE_DB, **payload)
    except Exception as exc:
        audit(
            "bad_source_candidate_record_failed",
            {
                "series": series,
                "issue": issue_number,
                "title": title,
                "reason": reason,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
        return None


def remember_bad_result(cache, series, issue_number, result_or_title, reason, *, record_durable=True):
    release = release_title(result_or_title)
    if not release:
        return
    entry = {
        "ts": time.time(),
        "series": series,
        "issue": str(issue_number or ""),
        "release": release,
        "source": result_source(result_or_title),
        "protocol": result_protocol(result_or_title),
        "download_url_hash": result_download_url_hash(result_or_title),
        "reason": reason,
    }
    bad = cache.setdefault("bad_results", {})
    bad[bad_result_key(series, issue_number, result_or_title)] = entry
    strong_key = bad_result_strong_key(series, issue_number, result_or_title)
    if strong_key != bad_result_key(series, issue_number, result_or_title):
        bad[strong_key] = entry
    if record_durable:
        record_durable_bad_source_result(series, issue_number, result_or_title, reason)


def source_failure_key(series, issue_number, release, source=None, protocol=None, download_url_hash=None):
    raw = "|".join(
        [
            normalize(series),
            normalized_number(issue_number),
            normalize(release),
            normalize(source),
            normalize(protocol),
            str(download_url_hash or ""),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def ensure_source_failures_table():
    if not COMPLETION_DB.exists():
        return

    def _ensure():
        con = sqlite3.connect(COMPLETION_DB, timeout=max(1, SQLITE_BUSY_TIMEOUT_MS / 1000))
        con.execute(f"pragma busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        try:
            con.execute(
                """
                create table if not exists source_failures (
                  failure_key text primary key,
                  normalized_series text not null,
                  series_title text,
                  normalized_number text not null,
                  issue_number text,
                  release_title text,
                  source text,
                  protocol text,
                  download_url_hash text,
                  reason text,
                  query text,
                  pending_key text,
                  retry_status text,
                  retry_count integer not null default 0,
                  alternate_attempted_at real,
                  first_seen real not null,
                  last_seen real not null
                )
                """
            )
            columns = {row[1] for row in con.execute("pragma table_info(source_failures)").fetchall()}
            if "download_url_hash" not in columns:
                con.execute("alter table source_failures add column download_url_hash text")
            con.execute("create index if not exists idx_source_failures_item on source_failures (normalized_series, normalized_number)")
            con.execute("create index if not exists idx_source_failures_retry on source_failures (retry_status, alternate_attempted_at)")
            con.commit()
        finally:
            con.close()

    return with_sqlite_lock_retry(_ensure, "ensure_source_failures_table")


def record_source_failure(
    series,
    issue_number,
    release,
    reason,
    *,
    source=None,
    protocol=None,
    download_url_hash=None,
    query=None,
    pending_key=None,
):
    if not series or not issue_number or not release or not COMPLETION_DB.exists():
        return

    def _record():
        ensure_source_failures_table()
        now = time.time()
        key = source_failure_key(series, issue_number, release, source=source, protocol=protocol, download_url_hash=download_url_hash)
        con = sqlite3.connect(COMPLETION_DB, timeout=max(1, SQLITE_BUSY_TIMEOUT_MS / 1000))
        con.execute(f"pragma busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        try:
            con.execute(
                """
                insert into source_failures (
                  failure_key, normalized_series, series_title, normalized_number, issue_number,
                  release_title, source, protocol, download_url_hash, reason, query, pending_key,
                  retry_status, retry_count, first_seen, last_seen
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, coalesce(?, 'failed_seen'), 0, ?, ?)
                on conflict(failure_key) do update set
                  source=coalesce(excluded.source, source_failures.source),
                  protocol=coalesce(excluded.protocol, source_failures.protocol),
                  download_url_hash=coalesce(excluded.download_url_hash, source_failures.download_url_hash),
                  reason=excluded.reason,
                  query=coalesce(excluded.query, source_failures.query),
                  pending_key=coalesce(excluded.pending_key, source_failures.pending_key),
                  last_seen=excluded.last_seen
                """,
                (
                    key,
                    normalize(series),
                    series,
                    normalized_number(issue_number),
                    str(issue_number),
                    release,
                    source,
                    protocol,
                    download_url_hash,
                    reason,
                    query,
                    pending_key,
                    "failed_seen",
                    now,
                    now,
                ),
            )
            con.commit()
        finally:
            con.close()

    try:
        return with_sqlite_lock_retry(_record, "record_source_failure")
    except sqlite3.OperationalError as exc:
        if not sqlite_locked(exc):
            raise
        audit_warning(
            "source_failure_record_skipped_locked",
            {
                "series": series,
                "issue_number": issue_number,
                "release": release,
                "reason": reason,
                "source": source,
                "protocol": protocol,
                "error": str(exc),
            },
        )


def mark_alternate_attempt(series, issue_number, status, note=None):
    if not series or not issue_number or not COMPLETION_DB.exists():
        return

    def _mark():
        ensure_source_failures_table()
        now = time.time()
        series_key = normalize(series)
        number_key = normalized_number(issue_number)
        con = sqlite3.connect(COMPLETION_DB, timeout=max(1, SQLITE_BUSY_TIMEOUT_MS / 1000))
        con.execute(f"pragma busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        try:
            updated = con.execute(
                """
                update source_failures
                   set retry_status=?,
                       retry_count=retry_count + 1,
                       alternate_attempted_at=?,
                       reason=coalesce(?, reason),
                       last_seen=?
                 where normalized_series=?
                   and normalized_number=?
                """,
                (status, now, note, now, series_key, number_key),
            ).rowcount
            if not updated:
                marker_release = f"alternate-attempt:{series_key}:{number_key}"
                con.execute(
                    """
                    insert into source_failures (
                      failure_key, normalized_series, series_title, normalized_number, issue_number,
                      release_title, reason, retry_status, retry_count, alternate_attempted_at,
                      first_seen, last_seen
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                    """,
                    (
                        source_failure_key(series, issue_number, marker_release),
                        series_key,
                        series,
                        number_key,
                        str(issue_number),
                        marker_release,
                        note,
                        status,
                        now,
                        now,
                        now,
                    ),
                )
            con.commit()
        finally:
            con.close()

    try:
        return with_sqlite_lock_retry(_mark, "mark_alternate_attempt")
    except sqlite3.OperationalError as exc:
        if not sqlite_locked(exc):
            raise
        audit_warning(
            "alternate_attempt_mark_skipped_locked",
            {"series": series, "issue_number": issue_number, "status": status, "note": note, "error": str(exc)},
        )


def alternate_attempt_count(series, issue_number):
    if not series or not issue_number or not COMPLETION_DB.exists():
        return 0
    ensure_source_failures_table()
    con = sqlite3.connect(COMPLETION_DB, timeout=30)
    con.execute("pragma busy_timeout=30000")
    try:
        row = con.execute(
            """
            select coalesce(max(retry_count), 0)
            from source_failures
            where normalized_series=?
              and normalized_number=?
              and alternate_attempted_at is not null
            """,
            (normalize(series), normalized_number(issue_number)),
        ).fetchone()
        return int(row[0] or 0) if row else 0
    finally:
        con.close()


def alternate_attempted(series, issue_number, max_attempts=1):
    try:
        limit = int(max_attempts)
    except (TypeError, ValueError):
        limit = 1
    if limit <= 0:
        return False
    return alternate_attempt_count(series, issue_number) >= limit


def prune_bad_results(cache):
    bad = cache.setdefault("bad_results", {})
    cutoff = time.time() - BAD_RESULT_TTL
    for key in list(bad.keys()):
        if float(bad.get(key, {}).get("ts") or 0) < cutoff:
            bad.pop(key, None)


def durable_bad_source_result_match(series, issue_number, result):
    if inkdrop_state is None:
        return None
    title = release_title(result)
    download_url_hash = result_download_url_hash(result)
    source_path = result_source_memory_path(result)
    if not title and not download_url_hash and not source_path:
        return None
    try:
        return inkdrop_state.find_bad_source_candidate(
            INKDROP_STATE_DB,
            title=title,
            series=series,
            source=result_source_memory_source(result),
            provider=result_source(result),
            protocol=result_protocol(result),
            download_url_hash=download_url_hash,
            source_path=source_path,
        )
    except Exception as exc:
        audit(
            "bad_source_candidate_lookup_failed",
            {
                "series": series,
                "issue": issue_number,
                "title": title,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
        return None


def known_bad_result_match(cache, series, issue_number, result):
    prune_bad_results(cache)
    bad = cache.setdefault("bad_results", {})
    exact = bad_result_key(series, issue_number, result)
    if exact in bad:
        return {"source": "runtime_cache", **dict(bad.get(exact) or {})}
    strong = bad_result_strong_key(series, issue_number, result)
    if strong in bad:
        return {"source": "runtime_cache", **dict(bad.get(strong) or {})}
    result_key = normalize(release_title(result))
    if not result_key:
        return None
    result_hash = result_download_url_hash(result)
    series_key = normalize(series)
    issue_key = normalized_number(issue_number)
    for row in bad.values():
        row_series = normalize(row.get("series") or row.get("release"))
        row_issue = normalized_number(row.get("issue"))
        row_release = normalize(row.get("release"))
        if series_key and series_key not in row_series:
            continue
        if issue_key and issue_key != row_issue:
            continue
        if result_hash and row.get("download_url_hash") == result_hash:
            return {"source": "runtime_cache", **dict(row or {})}
        if row_release == result_key:
            return {"source": "runtime_cache", **dict(row or {})}
        if row_release and result_key and (row_release.startswith(result_key) or result_key.startswith(row_release)):
            return {"source": "runtime_cache", **dict(row or {})}
    db_bad = durable_bad_source_result_match(series, issue_number, result)
    if db_bad:
        return {
            "source": "source_memory",
            "reason": db_bad.get("reason") or "known_bad_source_candidate",
            "release": db_bad.get("title") or release_title(result),
            "download_url_hash": db_bad.get("download_url_hash"),
            "bad_source_candidate_id": db_bad.get("id"),
            "provider": db_bad.get("provider"),
            "protocol": db_bad.get("protocol"),
            "source_path": db_bad.get("source_path"),
            "last_seen_at": db_bad.get("last_seen_at"),
        }
    return None


def is_known_bad_result(cache, series, issue_number, result):
    return bool(known_bad_result_match(cache, series, issue_number, result))


def known_bad_result_label(match):
    if not isinstance(match, dict):
        return "known bad source candidate"
    reason = str(match.get("reason") or "known_bad_source_candidate").replace("_", " ")
    if match.get("source") == "source_memory":
        return f"source memory: {reason}"
    return reason


def filter_known_bad_results(cache, row, series, issue_number, results, *, query=None, dry_run=False, source="prowlarr"):
    kept = []
    blocked = []
    recorded = 0
    for result in results or []:
        match = known_bad_result_match(cache, series, issue_number, result)
        if not match:
            kept.append(result)
            continue
        sample = {
            "title": release_title(result),
            "indexer": result_source(result),
            "protocol": result_protocol(result),
            "reason": match.get("reason") or "known_bad_source_candidate",
            "match_source": match.get("source"),
            "bad_source_candidate_id": match.get("bad_source_candidate_id"),
            "download_url_hash": match.get("download_url_hash") or result_download_url_hash(result),
        }
        blocked.append(sample)
        if recorded < 3:
            record_inkdrop_queue_attempt(
                row if isinstance(row, dict) else {},
                "known_bad_source_candidate",
                known_bad_result_label(match),
                query=query,
                candidate=result,
                dry_run=dry_run,
                source=source or "prowlarr",
                extra={
                    "known_bad_result": sample,
                    "bad_source_candidate_id": match.get("bad_source_candidate_id"),
                    "source_memory": match.get("source") == "source_memory",
                    "candidate_source": result_source_memory_source(result),
                },
            )
            recorded += 1
    return kept, blocked


def reconciliation_failure_records(limit=500):
    rows = []
    if COMPLETION_DB.exists():
        try:
            con = sqlite3.connect(COMPLETION_DB)
            con.row_factory = sqlite3.Row
            try:
                table = con.execute(
                    "select name from sqlite_master where type='table' and name='download_reconciliation'"
                ).fetchone()
                if table:
                    available = {row["name"] for row in con.execute("pragma table_info(download_reconciliation)").fetchall()}
                    base_columns = [
                        "pending_key",
                        "title",
                        "query",
                        "protocol",
                        "client",
                        "reason",
                        "lifecycle_state",
                        "updated_at",
                    ]
                    optional_columns = ["client_id", "client_hash", "nzo_id", "download_url_hash"]
                    columns = [column for column in base_columns + optional_columns if column in available]
                    if not columns:
                        columns = ["pending_key", "title", "query", "reason", "lifecycle_state", "updated_at"]
                    rows.extend(
                        dict(row)
                        for row in con.execute(
                            f"""
                            select {", ".join(columns)}
                            from download_reconciliation
                            where lifecycle_state in ('failed_download', 'bad_archive', 'false_positive', 'stale_no_local_file', 'wrong_series_or_subseries')
                            order by updated_at desc
                            limit ?
                            """,
                            (int(limit),),
                        ).fetchall()
                    )
            finally:
                con.close()
        except sqlite3.Error:
            pass
    if not RECONCILE_STATUS_FILE.exists():
        return rows
    try:
        data = json.loads(RECONCILE_STATUS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return rows
    samples = data.get("samples") or {}
    for state in SOURCE_FAILURE_STATES:
        for item in samples.get(state, []) or []:
            item = dict(item)
            item.setdefault("lifecycle_state", state)
            rows.append(item)
    rows.extend(pack_bad_archive_failure_records())
    rows.sort(key=lambda row: numeric_ts((row or {}).get("updated_at") or (row or {}).get("history_updated_at")), reverse=True)
    return rows[: int(limit or 500)]


def bad_archive_issue_number(row):
    for key in (
        "canonical_issue_number",
        "normalized_number",
        "issue_number",
        "matched_issue_number",
        "kapowarr_issue_number",
    ):
        value = row.get(key) if isinstance(row, dict) else None
        number = normalized_number(value)
        if number:
            return number
    for key in ("canonical_filename", "dest", "source"):
        series, issue = parse_series_issue((row or {}).get(key))
        if issue:
            return normalized_number(issue)
    return ""


def bad_archive_release_title(row):
    if not isinstance(row, dict):
        return ""
    for key in ("source", "canonical_filename", "dest"):
        value = str(row.get(key) or "").strip()
        if not value:
            continue
        if "/" in value or "\\" in value:
            return PurePosixPath(value.replace("\\", "/")).name
        return value
    return ""


def pack_bad_archive_failure_records():
    records = []
    sources = []
    try:
        data = read_json_file(PACK_BAD_ARCHIVE_HISTORY_FILE, {}) or {}
        rows = data.get("bad_archives") if isinstance(data, dict) else data
        if isinstance(rows, list):
            sources.extend(rows)
    except Exception:
        pass
    try:
        data = read_json_file(STATE_DIR / "import-status.json", {}) or {}
        rows = data.get("bad_archives") or data.get("skipped_bad_archives")
        if isinstance(rows, list):
            sources.extend(rows)
    except Exception:
        pass

    seen = set()
    for row in sources:
        if not isinstance(row, dict):
            continue
        series = str(row.get("matched_series") or row.get("series") or "").strip()
        issue = bad_archive_issue_number(row)
        release = bad_archive_release_title(row)
        if not series or not issue or not release:
            continue
        archive_check = row.get("archive_check") if isinstance(row.get("archive_check"), dict) else {}
        reason = archive_check.get("reason") or row.get("reason") or "bad_archive"
        source = str(row.get("history_source") or "pack_import")
        source_path = str(row.get("source") or "")
        updated_at = numeric_ts(row.get("history_updated_at") or row.get("updated_at") or row.get("ts") or time.time())
        fingerprint = (
            normalize(series),
            normalized_number(issue),
            normalize(release),
            row.get("sha256") or source_path,
        )
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        records.append(
            {
                "pending_key": f"pack-bad-archive:{row.get('sha256') or hashlib.sha1(source_path.encode('utf-8')).hexdigest()}",
                "title": release,
                "query": f"{series} {issue}".strip(),
                "protocol": "local",
                "client": source,
                "source": source,
                "reason": reason,
                "lifecycle_state": "bad_archive",
                "matched_local_path": source_path,
                "matched_series": series,
                "matched_kapowarr_volume_id": row.get("matched_kapowarr_id") or row.get("kapowarr_id"),
                "matched_kapowarr_issue_id": row.get("matched_kapowarr_issue_id") or row.get("kapowarr_issue_id"),
                "unit_model": row.get("manga_unit_model") or row.get("source_unit"),
                "truth_model": row.get("truth_model"),
                "download_url_hash": row.get("sha256") or hashlib.sha1(source_path.encode("utf-8")).hexdigest(),
                "updated_at": updated_at,
            }
        )
    return records


def parse_series_issue(text):
    clean = re.sub(r"[\._-]+", " ", str(text or ""))
    clean = re.sub(r"\[[^\]]+\]|\([^)]+\)", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    patterns = [
        r"(.+?)\s+(?:#|issue\s*|v|vol\.?\s*|volume\s*|ch\.?\s*|chapter\s*)0*(\d{1,4}(?:\.\d+)?)\b",
        r"(.+?)\s+0*(\d{1,4}(?:\.\d+)?)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, clean, re.I)
        if match:
            series = stripped_edition_title(match.group(1)).strip()
            issue = match.group(2)
            if series and issue:
                return series, issue
    return None, None


def missing_row_lookup(rows):
    query_map = {}
    issue_map = {}
    unit_model_cache = {}
    for row in rows or []:
        row = row if isinstance(row, dict) else {}
        is_manga = row_is_manga(row)
        unit_model = row_unit_model(row, unit_model_cache) if is_manga else None
        for value in query_variants_for_row(row, is_manga=is_manga, unit_model=unit_model):
            key = normalize(value)
            if key:
                query_map.setdefault(key, row)
        series_key = normalize(row.get("title"))
        issue_key = normalized_number(row.get("issue_number"))
        if series_key and issue_key:
            issue_map.setdefault((series_key, issue_key), row)
    return query_map, issue_map


def match_failure_to_missing(failure, rows=None, lookup=None):
    query = failure.get("query") or ""
    title = failure.get("title") or ""
    query_norm = normalize(query)
    if lookup is None:
        lookup = missing_row_lookup(rows or [])
    query_map, issue_map = lookup
    if query_norm and query_norm in query_map:
        return query_map[query_norm]
    series, issue = parse_series_issue(query or title)
    if not series or not issue:
        return None
    series_key = normalize(series)
    issue_key = normalized_number(issue)
    return issue_map.get((series_key, issue_key))


def ingest_reconciliation_bad_results(cache, rows=None, record_failures=True, search_deadline=None):
    if not rows:
        return 0
    failures = reconciliation_failure_records()
    lookup = missing_row_lookup(rows or []) if rows else ({}, {})
    recorded = 0
    for item in failures:
        if search_deadline is not None and time.monotonic() >= search_deadline:
            audit("reconciliation_bad_result_ingest_budget_exhausted", {
                "rows_seen": recorded,
                "failure_records": len(failures),
            })
            break
        release = item.get("title")
        if not release:
            continue
        match = match_failure_to_missing(item, rows or [], lookup=lookup)
        if match:
            series, issue = match.get("title"), match.get("issue_number")
        else:
            series, issue = parse_series_issue(item.get("query") or item.get("title"))
        if not series or not issue:
            continue
        reason = item.get("reason") or item.get("lifecycle_state") or "failed_download"
        failure_result = {
            "title": release,
            "indexer": item.get("client") or item.get("source"),
            "source": item.get("client") or item.get("source"),
            "protocol": item.get("protocol"),
            "downloadUrlHash": item.get("download_url_hash"),
        }
        remember_bad_result(cache, series, issue, failure_result, reason, record_durable=record_failures)
        if record_failures:
            record_source_failure(
                series,
                issue,
                release,
                reason,
                source=item.get("client"),
                protocol=item.get("protocol"),
                download_url_hash=result_download_url_hash(failure_result),
                query=item.get("query"),
                pending_key=item.get("pending_key"),
            )
            recorded += 1
    return recorded


def english_confidence(result):
    acquire = load_acquire()
    if hasattr(acquire, "classify_english_result"):
        return acquire.classify_english_result(result)
    return {"status": "unknown", "score": 0, "reason": "English classifier unavailable"}


def pack_match_estimate(volume_id, pack_info):
    return {
        "useful_missing_count": 0,
        "already_present_count": 0,
        "unknown_unmatched_count": 1,
        "useful_missing_sample": [],
        "already_present_sample": [],
        "coverage_source": "missing_volume_context" if volume_id in (None, "") else "kapowarr_fallback_disabled",
    }


def inkdrop_pack_match_estimate(series, pack_info):
    if not series or not INKDROP_STATE_DB.exists():
        return pack_match_estimate(None, pack_info)
    ranges = (pack_info or {}).get("ranges") or []
    keywords = {str(item).lower() for item in ((pack_info or {}).get("keywords") or [])}
    complete_series_pack = "complete" in keywords and not any(
        item.get("start") is not None and item.get("end") is not None
        for item in ranges
    )
    numbers = set()
    for item in ranges:
        start = item.get("start")
        end = item.get("end")
        if start is None or end is None:
            continue
        numbers.update(range(int(start), int(end) + 1))
    con = inkdrop_db.open_connection(
        INKDROP_STATE_DB,
        readonly=True,
        operation="missing_acquire_pack_estimate",
    )
    try:
        rows = con.execute(
            """
            select i.issue_number, i.normalized_number, w.status, q.state
            from wanted_items w
            join series s on s.id = w.series_id
            left join issues i on i.id = w.issue_id
            left join queue_items q on q.wanted_id = w.id
            where lower(s.title) = lower(?)
              and coalesce(q.active, 1) = 1
              and coalesce(q.state, w.status) not in ('verified','satisfied','inactive','stale_source_absent')
            order by i.normalized_number, i.issue_number
            """,
            (series,),
        ).fetchall()
    except Exception as exc:
        audit("inkdrop_pack_match_estimate_failed", {"series": series, "error": f"{type(exc).__name__}: {exc}"})
        return pack_match_estimate(None, pack_info)
    finally:
        con.close()
    useful = []
    unknown = 0
    for row in rows:
        issue_value = row["issue_number"] or row["normalized_number"]
        try:
            number = int(float(issue_value))
        except (TypeError, ValueError):
            unknown += 1
            continue
        if numbers and number not in numbers:
            continue
        useful.append({"issue": row["issue_number"], "calculated": row["normalized_number"], "presence": "inkdrop_wanted"})
    if not numbers and not complete_series_pack:
        unknown = max(unknown, len(rows))
    return {
        "useful_missing_count": len(useful),
        "already_present_count": 0,
        "unknown_unmatched_count": unknown,
        "useful_missing_sample": useful[:20],
        "already_present_sample": [],
        "coverage_source": "inkdrop_complete_keyword" if complete_series_pack else ("inkdrop_explicit_range" if numbers else "inkdrop_unknown"),
    }


def collected_edition_pack_info(pack_info):
    pack_info = pack_info if isinstance(pack_info, dict) else {}
    units = pack_info.get("collected_units") if isinstance(pack_info.get("collected_units"), list) else []
    if units:
        return units
    keywords = {str(item).lower() for item in (pack_info.get("keywords") or [])}
    if {"book", "tpb", "trade paperback", "hardcover", "hc"}.intersection(keywords):
        return [{"kind": "collected_edition", "unit": sorted(keywords)[0]}]
    return []


def collected_edition_range_hints():
    hints = json.loads(json.dumps(DEFAULT_COLLECTED_EDITION_RANGE_HINTS))
    custom = inkdrop_app_setting_value("automation.collected_edition_range_hints", None)
    if not isinstance(custom, dict):
        return hints
    for series, value in custom.items():
        series_key = normalize(series)
        if not series_key or not isinstance(value, dict):
            continue
        target = hints.setdefault(series_key, {})
        for unit_name, unit_ranges in value.items():
            unit_key = normalize(unit_name)
            if not unit_key or not isinstance(unit_ranges, dict):
                continue
            target_unit = target.setdefault(unit_key, {})
            for unit_number, issue_range in unit_ranges.items():
                if (
                    isinstance(issue_range, (list, tuple))
                    and len(issue_range) >= 2
                ):
                    target_unit[str(unit_number)] = [issue_range[0], issue_range[1]]
    return hints


def collected_edition_range_hint_for_row(row):
    row = row if isinstance(row, dict) else {}
    issue = issue_int(row.get("issue_number") or row.get("issue") or row.get("chapter"))
    if issue is None:
        return None
    series_hints = collected_edition_range_hints().get(normalize(row.get("title"))) or {}
    for unit_name, ranges in series_hints.items():
        if not isinstance(ranges, dict):
            continue
        for unit_number, issue_range in ranges.items():
            if not isinstance(issue_range, (list, tuple)) or len(issue_range) < 2:
                continue
            try:
                start = int(float(str(issue_range[0])))
                end = int(float(str(issue_range[1])))
                unit_number_int = int(float(str(unit_number)))
            except (TypeError, ValueError):
                continue
            if start <= issue <= end:
                return {
                    "series": row.get("title"),
                    "issue": row.get("issue_number") or row.get("issue") or row.get("chapter"),
                    "unit": unit_name,
                    "unit_number": unit_number_int,
                    "range": [start, end],
                    "priority_reason": "known_collected_edition_range_hint",
                }
    return None


def missing_row_priority(row, index=0):
    row = row if isinstance(row, dict) else {}
    range_hint = collected_edition_range_hint_for_row(row)
    source = str(row.get("current_source") or "").strip().lower()
    last_event = str(row.get("last_event") or "").strip().lower()
    stale_handoff = any(
        token in " ".join([source, last_event])
        for token in (
            "sabnzbd handoff is no longer in the client",
            "download_client",
            "sabnzbd",
        )
    )
    return (
        0 if range_hint else 1,
        1 if stale_handoff else 0,
        normalize(row.get("title")),
        issue_int(row.get("issue_number") or row.get("issue") or row.get("chapter")) or 999999,
        int(index),
    )


def order_missing_rows_for_acquisition(rows):
    return [
        row
        for _priority, row in sorted(
            ((missing_row_priority(row, index), row) for index, row in enumerate(rows or [])),
            key=lambda item: item[0],
        )
    ]


def collected_edition_range_match_for_row(row, raw_title, pack_info):
    if not series_title_starts_release(row.get("title"), raw_title):
        return None
    issue = issue_int(row.get("issue_number") or row.get("issue") or row.get("chapter"))
    if issue is None:
        return None
    series_hints = collected_edition_range_hints().get(normalize(row.get("title"))) or {}
    if not series_hints:
        return None
    for unit in collected_edition_pack_info(pack_info):
        if not isinstance(unit, dict):
            continue
        unit_number = unit.get("number")
        if unit_number in (None, ""):
            continue
        try:
            unit_number_int = int(float(str(unit_number)))
        except (TypeError, ValueError):
            continue
        unit_keys = unique([
            unit.get("unit"),
            unit.get("kind"),
            "book" if normalize(raw_title).find("book") >= 0 else "",
        ])
        for unit_key in unit_keys:
            ranges = series_hints.get(normalize(unit_key)) or {}
            issue_range = ranges.get(str(unit_number_int)) or ranges.get(str(unit_number))
            if not isinstance(issue_range, (list, tuple)) or len(issue_range) < 2:
                continue
            try:
                start = int(float(str(issue_range[0])))
                end = int(float(str(issue_range[1])))
            except (TypeError, ValueError):
                continue
            if start <= issue <= end:
                return {
                    "useful_missing_count": 1,
                    "already_present_count": 0,
                    "unknown_unmatched_count": 0,
                    "useful_missing_sample": [
                        {
                            "issue": row.get("issue_number"),
                            "calculated": row.get("normalized_number") or row.get("calculated_issue_number"),
                            "presence": "inkdrop_wanted",
                            "match": "collected_edition_range_hint",
                            "unit": unit_key,
                            "unit_number": unit_number_int,
                            "range": [start, end],
                        }
                    ],
                    "already_present_sample": [],
                    "coverage_source": "collected_edition_range_hint",
                    "collected_edition": True,
                    "unit": unit_key,
                    "unit_number": unit_number_int,
                    "range": [start, end],
                }
    return None


def collected_edition_story_key(issue_title):
    text = str(issue_title or "").strip()
    if not text:
        return ""
    text = re.sub(r"\([^)]*\b(?:part|pt)\b[^)]*\)", " ", text, flags=re.I)
    text = re.sub(r"\b(?:part|pt)\s*(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\b.*$", " ", text, flags=re.I)
    text = re.sub(r"[,;:]\s*(?:part|pt)\b.*$", " ", text, flags=re.I)
    text = re.sub(r"\b(?:conclusion|finale)\b", " ", text, flags=re.I)
    text = re.sub(r"\b(?:chapter|issue)\s*\d+\b", " ", text, flags=re.I)
    text = re.sub(r"\b(?:i|ii|iii|iv|v|vi|vii|viii|ix|x)\b$", " ", text.strip(), flags=re.I)
    words = [
        word
        for word in re.findall(r"[a-z0-9]+", text.lower())
        if word not in {"a", "an", "and", "of", "the", "to"} and not word.isdigit()
    ]
    if len(words) >= 2:
        return " ".join(words)
    if len(words) == 1 and len(words[0]) >= 6:
        return words[0]
    return ""


def collected_edition_story_key_matches_release(story_key, raw_title):
    words = [word for word in re.findall(r"[a-z0-9]+", str(story_key or "").lower()) if word]
    if not words:
        return False
    release = normalize(raw_title)
    if normalize(story_key) and normalize(story_key) in release:
        return True
    release_words = re.findall(r"[a-z0-9]+", release)
    if len(words) == 1:
        return words[0] in release_words
    pos = -1
    for word in words:
        try:
            pos = release_words.index(word, pos + 1)
        except ValueError:
            return False
    return True


def collected_edition_pack_match_for_row(row, raw_title, pack_info):
    if not collected_edition_pack_info(pack_info):
        return None
    if not series_title_starts_release(row.get("title"), raw_title):
        return None
    range_match = collected_edition_range_match_for_row(row, raw_title, pack_info)
    story_key = collected_edition_story_key(row.get("issue_title") or row.get("title_issue") or row.get("issueTitle"))
    if not story_key:
        if range_match:
            return range_match
        return {
            "useful_missing_count": 0,
            "already_present_count": 0,
            "unknown_unmatched_count": 1,
            "useful_missing_sample": [],
            "already_present_sample": [],
            "coverage_source": "collected_edition_unknown",
            "collected_edition": True,
            "story_key": "",
        }
    if not collected_edition_story_key_matches_release(story_key, raw_title):
        if range_match:
            range_match["story_key"] = story_key
            return range_match
        return {
            "useful_missing_count": 0,
            "already_present_count": 0,
            "unknown_unmatched_count": 1,
            "useful_missing_sample": [],
            "already_present_sample": [],
            "coverage_source": "collected_edition_story_mismatch",
            "collected_edition": True,
            "story_key": story_key,
        }
    return {
        "useful_missing_count": 1,
        "already_present_count": 0,
        "unknown_unmatched_count": 0,
        "useful_missing_sample": [
            {
                "issue": row.get("issue_number"),
                "calculated": row.get("normalized_number") or row.get("calculated_issue_number"),
                "presence": "inkdrop_wanted",
                "match": "collected_edition_story_title",
                "story_key": story_key,
            }
        ],
        "already_present_sample": [],
        "coverage_source": "collected_edition_story_title",
        "collected_edition": True,
        "story_key": story_key,
    }


def pack_content_text_values(value):
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (int, float, bool)):
        return []
    if isinstance(value, dict):
        values = []
        for key in PACK_CONTENT_TEXT_KEYS:
            values.extend(pack_content_text_values(value.get(key)))
        if not values:
            for item in value.values():
                values.extend(pack_content_text_values(item))
        return values
    if isinstance(value, (list, tuple, set)):
        values = []
        for item in value:
            values.extend(pack_content_text_values(item))
        return values
    return [str(value)]


def pack_content_fragments(result):
    result = result if isinstance(result, dict) else {}
    fragments = []
    for key in PACK_CONTENT_TEXT_KEYS:
        fragments.extend(pack_content_text_values(result.get(key)))
    unique_fragments = []
    seen = set()
    for fragment in fragments:
        text = str(fragment or "").replace("\\n", "\n").replace("\\r", "\n").strip()
        if not text:
            continue
        if len(text) > 20000:
            text = text[:20000]
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        unique_fragments.append(text)
    return unique_fragments


def pack_content_entry_candidates(result, limit=PACK_DETAIL_MAX_ENTRIES):
    entries = []
    seen = set()

    def add_entry(value):
        text = str(value or "").strip(" \t\r\n-")
        if not text:
            return
        text = re.sub(r"\s+", " ", text)
        if len(text) > 280:
            text = text[-280:]
        key = text.lower()
        if key in seen:
            return
        seen.add(key)
        entries.append(text)

    for fragment in pack_content_fragments(result):
        for line in re.split(r"[\r\n]+", fragment):
            line = line.strip()
            if not line:
                continue
            if COMIC_FILE_EXTENSION_RE.search(line):
                add_entry(line)
                continue
            if "/" in line or "\\" in line:
                add_entry(line)
                add_entry(re.split(r"[\\/]", line)[-1])
                continue
            if WEEKLY_COMICS_PACK_RE.search(fragment) and len(line) <= 240:
                add_entry(line)
            if len(entries) >= limit:
                return entries[:limit]
    return entries[:limit]


def unique_text_entries(values, limit=PACK_DETAIL_MAX_ENTRIES):
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
        if len(out) >= limit:
            break
    return out


def comic_file_entry_text_candidates(text):
    raw = str(text or "")
    if not raw:
        return []
    try:
        raw = html.unescape(raw)
    except Exception:
        pass
    candidates = []
    attr_re = re.compile(
        r"(?is)\b(?:subject|filename|fileName|name|title)\s*=\s*(['\"])(.*?)\1"
    )
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


def comic_file_entries_from_text(text, limit=PACK_DETAIL_MAX_ENTRIES):
    entries = []
    for line in re.split(r"[\r\n]+", str(text or "")):
        line = line.strip()
        if not line:
            continue
        for candidate in comic_file_entry_text_candidates(line):
            entries.append(candidate)
            if len(entries) >= limit:
                break
        if len(entries) >= limit:
            break
        if not COMIC_FILE_EXTENSION_RE.search(line):
            continue
        entries.append(line)
        if len(entries) >= limit:
            break
    return unique_text_entries(entries, limit=limit)


def bdecode_value(data):
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError("bencode payload must be bytes")
    data = bytes(data)

    def parse(index):
        if index >= len(data):
            raise ValueError("unexpected end of bencode payload")
        token = data[index:index + 1]
        if token == b"i":
            end = data.index(b"e", index)
            return int(data[index + 1:end]), end + 1
        if token == b"l":
            values = []
            index += 1
            while index < len(data) and data[index:index + 1] != b"e":
                value, index = parse(index)
                values.append(value)
            if index >= len(data):
                raise ValueError("unterminated bencode list")
            return values, index + 1
        if token == b"d":
            values = {}
            index += 1
            while index < len(data) and data[index:index + 1] != b"e":
                key, index = parse(index)
                value, index = parse(index)
                values[key] = value
            if index >= len(data):
                raise ValueError("unterminated bencode dict")
            return values, index + 1
        if token.isdigit():
            colon = data.index(b":", index)
            length = int(data[index:colon])
            start = colon + 1
            end = start + length
            if end > len(data):
                raise ValueError("bencode string length exceeds payload")
            return data[start:end], end
        raise ValueError(f"unexpected bencode token: {token!r}")

    value, offset = parse(0)
    if offset > len(data):
        raise ValueError("bencode parse exceeded payload")
    return value


def torrent_dict_get(mapping, *names):
    if not isinstance(mapping, dict):
        return None
    for name in names:
        for key in (name, str(name).encode("utf-8")):
            if key in mapping:
                return mapping.get(key)
    return None


def torrent_text(value):
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.decode("latin-1", errors="replace")
    if value is None:
        return ""
    return str(value)


def torrent_path_text(value):
    if isinstance(value, list):
        parts = [torrent_text(item).strip(" /\\") for item in value]
        return "/".join(part for part in parts if part)
    return torrent_text(value).strip(" /\\")


def torrent_file_entries(data, limit=PACK_DETAIL_MAX_ENTRIES):
    root = bdecode_value(data)
    info = torrent_dict_get(root, "info")
    if not isinstance(info, dict):
        return []
    entries = []
    files = torrent_dict_get(info, "files")
    if isinstance(files, list):
        for item in files:
            if not isinstance(item, dict):
                continue
            path = torrent_dict_get(item, "path.utf-8", "path")
            entry = torrent_path_text(path)
            if entry:
                entries.append(entry)
            if len(entries) >= limit:
                break
    else:
        name = torrent_dict_get(info, "name.utf-8", "name")
        entry = torrent_path_text(name)
        if entry:
            entries.append(entry)
    return unique_text_entries(entries, limit=limit)


def xml_tag_name(tag):
    return str(tag or "").rsplit("}", 1)[-1].lower()


def nzb_file_entries(data, limit=PACK_DETAIL_MAX_ENTRIES):
    try:
        text = bytes(data).decode("utf-8", errors="replace")
    except Exception:
        text = str(data or "")
    entries = []
    try:
        root = ET.fromstring(text)
        for elem in root.iter():
            tag = xml_tag_name(elem.tag)
            if tag == "file":
                subject = elem.attrib.get("subject") or ""
                entries.extend(comic_file_entries_from_text(subject, limit=limit))
            elif tag == "meta":
                meta_type = str(elem.attrib.get("type") or "").lower()
                if meta_type in {"filename", "name", "title"}:
                    entries.extend(comic_file_entries_from_text(elem.text or "", limit=limit))
            if len(entries) >= limit:
                break
    except ET.ParseError:
        pass
    if len(entries) < limit:
        entries.extend(comic_file_entries_from_text(text, limit=limit - len(entries)))
    return unique_text_entries(entries, limit=limit)


def pack_detail_entries_from_bytes(data, result=None, limit=PACK_DETAIL_MAX_ENTRIES):
    result = result if isinstance(result, dict) else {}
    protocol = normalize_protocol_name(result.get("protocol"))
    payload = bytes(data or b"")
    if not payload:
        return []
    if protocol == "torrent" or (payload[:1] == b"d" and b"4:info" in payload[:4096]):
        try:
            entries = torrent_file_entries(payload, limit=limit)
            if entries:
                return entries
        except Exception as exc:
            audit("pack_detail_torrent_parse_failed", {
                "title": release_title(result),
                "indexer": result_source(result),
                "error": f"{type(exc).__name__}: {exc}",
            })
    if protocol == "usenet" or b"<nzb" in payload[:4096].lower():
        entries = nzb_file_entries(payload, limit=limit)
        if entries:
            return entries
    if inkdrop_nfo_parser is not None:
        try:
            # Scene-era pack listings are commonly CP437, not UTF-8; a plain
            # UTF-8 decode silently mangles their extended characters into
            # replacement chars. Reuse the NFO parser's decode chain
            # (utf-8 -> cp437 -> latin-1) instead of guessing UTF-8 alone.
            text, _decode_meta = inkdrop_nfo_parser.decode_nfo_bytes(payload)
        except Exception:
            text = ""
    else:
        try:
            text = payload.decode("utf-8", errors="replace")
        except Exception:
            text = ""
    return comic_file_entries_from_text(text, limit=limit)


def pack_detail_cache_key(result):
    result = result if isinstance(result, dict) else {}
    identity = result_download_url_hash(result)
    if not identity:
        for key in ("guid", "infoUrl", "magnetUrl", "title"):
            value = str(result.get(key) or "").strip()
            if value:
                identity = hashlib.sha256(value.encode("utf-8")).hexdigest()
                break
    if not identity:
        return ""
    return "|".join([
        normalize(result.get("indexer") or result.get("source")),
        normalize_protocol_name(result.get("protocol")),
        identity,
    ])


def pack_detail_fetch_enabled():
    if env_is_set("INKDROP_PACK_DETAIL_FETCH"):
        return truthy_env("INKDROP_PACK_DETAIL_FETCH")
    return boolish_value(inkdrop_app_setting_value("automation.pack_detail_fetch", True), True)


def pack_detail_merge_result(result, detail):
    entries = unique_text_entries((detail or {}).get("entries") or [])
    if not entries:
        return result
    merged = dict(result or {})
    existing = pack_content_text_values(merged.get("files"))
    merged["files"] = unique_text_entries([*existing, *entries], limit=PACK_DETAIL_MAX_ENTRIES)
    merged["pack_detail_entries"] = entries
    merged["pack_detail_source"] = (detail or {}).get("source")
    merged["pack_detail_status"] = (detail or {}).get("status")
    merged["pack_detail_fetched_at"] = (detail or {}).get("ts")
    return merged


def cached_pack_detail(cache, result):
    key = pack_detail_cache_key(result)
    if not key:
        return None
    details = cache.setdefault("pack_detail_results", {})
    detail = details.get(key)
    if not isinstance(detail, dict):
        return None
    age = time.time() - float(detail.get("ts") or 0)
    ttl = PACK_DETAIL_CACHE_TTL_SECONDS if detail.get("entries") else 6 * 3600
    if detail.get("entries"):
        cached_limit = int(detail.get("max_entries") or 250)
        if cached_limit < PACK_DETAIL_MAX_ENTRIES and int(detail.get("entry_count") or 0) >= cached_limit:
            return None
    if age <= ttl:
        return detail
    return None


def fetch_pack_detail_url_bytes(acquire, url, timeout=8.0, max_bytes=PACK_DETAIL_MAX_BYTES):
    url = str(url or "").strip()
    if not url or url.lower().startswith("magnet:"):
        raise ValueError("candidate has no metadata fetch URL")
    headers = {"User-Agent": "InkDrop pack detail probe"}
    try:
        parsed = urllib.parse.urlparse(url)
        if str(parsed.hostname or "").lower() in TRUSTED_PROWLARR_HOSTS and hasattr(acquire, "load_prowlarr_key"):
            api_key = acquire.load_prowlarr_key()
            if api_key:
                headers["X-Api-Key"] = api_key
    except Exception:
        pass
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=max(1.0, float(timeout or 8.0))) as response:
        data = response.read(int(max_bytes) + 1)
        headers = dict(response.headers.items())
        if len(data) > int(max_bytes):
            data = data[: int(max_bytes)]
            headers["X-InkDrop-Truncated"] = "1"
        return data, headers


def fetch_pack_detail_bytes(acquire, result, timeout=8.0, max_bytes=PACK_DETAIL_MAX_BYTES):
    url = str((result or {}).get("downloadUrl") or (result or {}).get("download_url") or "").strip()
    return fetch_pack_detail_url_bytes(acquire, url, timeout=timeout, max_bytes=max_bytes)


def pack_detail_sidecar_urls(headers, result=None):
    headers = headers if isinstance(headers, dict) else {}
    result = result if isinstance(result, dict) else {}
    lowered = {str(key or "").strip().lower(): str(value or "").strip() for key, value in headers.items()}
    urls = []
    for key in PACK_DETAIL_SIDECAR_HEADER_KEYS:
        value = lowered.get(key)
        if value and value.lower().startswith(("http://", "https://")) and value not in urls:
            urls.append(value)
    info_url = str(result.get("infoUrl") or result.get("info_url") or result.get("detailsUrl") or "").strip()
    download_url = str(result.get("downloadUrl") or result.get("download_url") or "").strip()
    if info_url and info_url.lower().startswith(("http://", "https://")) and info_url != download_url and info_url not in urls:
        urls.append(info_url)
    return urls[:3]


def pack_detail_headers_truncated(headers):
    headers = headers if isinstance(headers, dict) else {}
    return str(headers.get("X-InkDrop-Truncated") or headers.get("x-inkdrop-truncated") or "").lower() in {"1", "true", "yes"}


def enrich_pack_result_with_detail(acquire, result, cache, args=None, fetch_state=None):
    result = result if isinstance(result, dict) else {}
    detail = cached_pack_detail(cache, result)
    if detail:
        return pack_detail_merge_result(result, detail)
    if not pack_detail_fetch_enabled():
        return result
    key = pack_detail_cache_key(result)
    if not key:
        return result
    protocol = normalize_protocol_name(result.get("protocol"))
    if protocol not in {"torrent", "usenet"}:
        return result
    url = str(result.get("downloadUrl") or result.get("download_url") or "").strip()
    if not url or url.lower().startswith("magnet:"):
        return result
    fetch_state = fetch_state if isinstance(fetch_state, dict) else {}
    max_fetches = env_int(
        "INKDROP_PACK_DETAIL_MAX_FETCHES_PER_RUN",
        getattr(args, "pack_detail_fetch_max", PACK_DETAIL_MAX_FETCHES_PER_RUN) if args is not None else PACK_DETAIL_MAX_FETCHES_PER_RUN,
        0,
        50,
    )
    if int(fetch_state.get("count") or 0) >= max_fetches:
        return result
    max_bytes = env_int("INKDROP_PACK_DETAIL_MAX_BYTES", PACK_DETAIL_MAX_BYTES, 1024, 64 * 1024 * 1024)
    timeout = min(float(getattr(args, "prowlarr_timeout_seconds", 8.0) or 8.0), env_float("INKDROP_PACK_DETAIL_TIMEOUT_SECONDS", 8.0))
    details = cache.setdefault("pack_detail_results", {})
    fetch_state["count"] = int(fetch_state.get("count") or 0) + 1
    try:
        payload, headers = fetch_pack_detail_bytes(acquire, result, timeout=timeout, max_bytes=max_bytes)
        entries = pack_detail_entries_from_bytes(payload, result=result)
        truncated = pack_detail_headers_truncated(headers)
        source = "download_metadata"
        sidecar_results = []
        if not entries:
            sidecar_max_bytes = env_int(
                "INKDROP_PACK_DETAIL_SIDECAR_MAX_BYTES",
                PACK_DETAIL_SIDECAR_MAX_BYTES,
                1024,
                16 * 1024 * 1024,
            )
            for sidecar_url in pack_detail_sidecar_urls(headers, result):
                sidecar_record = {"url_hash": hashlib.sha256(sidecar_url.encode("utf-8")).hexdigest()}
                try:
                    sidecar_payload, sidecar_headers = fetch_pack_detail_url_bytes(
                        acquire,
                        sidecar_url,
                        timeout=timeout,
                        max_bytes=sidecar_max_bytes,
                    )
                    sidecar_entries = pack_detail_entries_from_bytes(sidecar_payload, result=result)
                    sidecar_truncated = pack_detail_headers_truncated(sidecar_headers)
                    sidecar_record.update({
                        "status": "ok" if sidecar_entries else "no_entries",
                        "entries": len(sidecar_entries),
                        "truncated": sidecar_truncated,
                        "content_type": sidecar_headers.get("Content-Type") or sidecar_headers.get("content-type"),
                    })
                    sidecar_results.append(sidecar_record)
                    if sidecar_entries:
                        entries = sidecar_entries
                        truncated = sidecar_truncated
                        source = "download_metadata_sidecar"
                        break
                except Exception as sidecar_exc:
                    sidecar_record.update({
                        "status": "error",
                        "error": redact_error(sidecar_exc),
                    })
                    sidecar_results.append(sidecar_record)
        detail = {
            "ts": time.time(),
            "status": ("partial_ok" if truncated else "ok") if entries else ("partial_no_entries" if truncated else "no_entries"),
            "source": source,
            "entries": entries,
            "entry_count": len(entries),
            "max_entries": PACK_DETAIL_MAX_ENTRIES,
            "truncated": truncated,
            "sidecar_results": sidecar_results,
            "content_type": headers.get("Content-Type") or headers.get("content-type"),
            "protocol": protocol,
            "indexer": result.get("indexer") or result.get("source"),
            "title": release_title(result),
        }
        details[key] = detail
        audit("pack_detail_metadata_fetched", {
            "title": release_title(result),
            "indexer": result_source(result),
            "protocol": protocol,
            "entries": len(entries),
            "status": detail["status"],
        })
        return pack_detail_merge_result(result, detail)
    except Exception as exc:
        detail = {
            "ts": time.time(),
            "status": "error",
            "source": "download_metadata",
            "entries": [],
            "entry_count": 0,
            "protocol": protocol,
            "indexer": result.get("indexer") or result.get("source"),
            "title": release_title(result),
            "error": redact_error(exc),
        }
        details[key] = detail
        audit("pack_detail_metadata_fetch_failed", {
            "title": release_title(result),
            "indexer": result_source(result),
            "protocol": protocol,
            "error": redact_error(exc),
        })
        return result


def pack_content_entry_basename(entry):
    text = str(entry or "").replace("\\", "/").strip()
    if re.search(r"/[^/]*\.(?:cbz|cbr|pdf|epub|zip|rar|7z)\b", text, re.I):
        text = text.rsplit("/", 1)[-1]
    text = COMIC_FILE_EXTENSION_RE.sub("", text)
    text = re.sub(r"\[[^\]]+\]|\{[^}]+\}", " ", text)
    text = re.sub(r"\s+", " ", text.replace("_", " ")).strip()
    return text


def series_collection_entry_match(series, raw_title):
    if not series or not raw_title:
        return None
    title_text = strip_release_prefixes(raw_title)
    match = None
    for pattern in title_words_patterns(series):
        if not pattern:
            continue
        match = re.match(rf"^\s*{pattern}(?P<tail>.*)$", title_text, re.I)
        if match:
            break
    if not match:
        return None
    tail_text = match.group("tail") or ""
    marker_text = f"{tail_text} {title_text}"
    collection_marker = re.search(
        r"\b(?:omnibus|compendium|complete|collection|collected|library\s+edition|deluxe\s+edition|tpb|trade\s+paperback|hardcover|hc)\b",
        marker_text,
        re.I,
    )
    year_range_marker = re.search(r"\b(?:19|20)\d{2}\s*[-–]\s*(?:19|20)\d{2}\b", marker_text)
    if not collection_marker and not year_range_marker:
        return None
    words = re.findall(r"[a-z0-9]+", tail_text.lower())
    title_words = set(re.findall(r"[a-z0-9]+", str(series or "").lower()))
    allowed = {
        "a",
        "an",
        "and",
        "by",
        "cbz",
        "cbr",
        "collection",
        "collected",
        "complete",
        "compendium",
        "deluxe",
        "digital",
        "edition",
        "english",
        "fan",
        "fixed",
        "hardcover",
        "hc",
        "hybrid",
        "library",
        "made",
        "of",
        "omnibus",
        "paperback",
        "saga",
        "scan",
        "scans",
        "set",
        "the",
        "tpb",
        "trade",
        "volume",
        "volumes",
    }
    suspicious = []
    for word in words:
        if word in allowed or word in title_words:
            continue
        if word.isdigit() and 1900 <= int(word) <= 2099:
            continue
        if re.fullmatch(r"(?:v|vol|volume|book|part|pt|issue|ch|chapter)0*\d{1,4}", word):
            continue
        suspicious.append(word)
    if suspicious:
        return None
    return {
        "entry": str(raw_title),
        "basename": pack_content_entry_basename(raw_title),
        "collection_marker": collection_marker.group(0).lower() if collection_marker else "year_range",
    }


def pack_content_entry_matches_row(row, entry):
    series = row.get("title")
    issue = issue_int(row.get("issue_number"))
    if not series or issue is None or issue <= 0:
        return None
    basename = pack_content_entry_basename(entry)
    if not basename or not series_title_starts_release(series, basename):
        return None
    for pattern in title_words_patterns(series):
        if not pattern:
            continue
        match = re.search(
            rf"^\s*{pattern}[\W_]+(?!(?:v|vol(?:ume)?|book|books|tpb|hc|hardcover|trade)\b)"
            rf"(?:#|issue|no\.?)?[\W_]*0*{issue}(?:[^0-9]|$)",
            basename,
            re.I,
        )
        if match:
            return {
                "entry": str(entry),
                "basename": basename,
                "issue": row.get("issue_number"),
                "calculated": row.get("normalized_number") or row.get("calculated_issue_number") or row.get("issue_number"),
            }
    collection_match = series_collection_entry_match(series, basename)
    if collection_match:
        return {
            "entry": str(entry),
            "basename": basename,
            "issue": row.get("issue_number"),
            "calculated": row.get("normalized_number") or row.get("calculated_issue_number") or row.get("issue_number"),
            "collection_entry": True,
            "collection_marker": collection_match.get("collection_marker"),
        }
    return None


def pack_content_match_sample(row, match):
    row = row if isinstance(row, dict) else {}
    match = match if isinstance(match, dict) else {}
    sample = {
        "issue": match.get("issue"),
        "calculated": match.get("calculated"),
        "presence": row.get("presence") or "inkdrop_wanted",
        "match": "pack_contents_series_collection" if match.get("collection_entry") else "pack_contents_filename",
        "file_entry": match.get("basename"),
    }
    if match.get("collection_entry"):
        sample["collection_entry"] = True
        sample["collection_marker"] = match.get("collection_marker")
    for source_key, target_key in (
        ("title", "series"),
        ("series_id", "series_id"),
        ("issue_id", "issue_id"),
        ("wanted_id", "wanted_id"),
        ("queue_id", "queue_id"),
        ("media_type", "media_type"),
        ("publisher", "publisher"),
        ("metadata_provider", "metadata_provider"),
        ("metadata_id", "metadata_id"),
    ):
        value = row.get(source_key)
        if value not in (None, ""):
            sample[target_key] = value
    return sample


def active_inkdrop_wanted_rows_for_pack_contents(limit=5000):
    if not INKDROP_STATE_DB.exists():
        return []
    try:
        con = inkdrop_db.open_connection(
            INKDROP_STATE_DB,
            readonly=True,
            operation="missing_acquire_pack_contents",
        )
        try:
            rows = con.execute(
                """
                select
                    s.id as series_id,
                    s.title as title,
                    s.media_type as media_type,
                    s.publisher as publisher,
                    s.metadata_provider as metadata_provider,
                    s.metadata_id as metadata_id,
                    i.id as issue_id,
                    i.issue_number as issue_number,
                    i.normalized_number as normalized_number,
                    i.title as issue_title,
                    w.id as wanted_id,
                    w.status as wanted_status,
                    q.id as queue_id,
                    q.state as queue_state,
                    q.query as queue_query
                from wanted_items w
                join series s on s.id = w.series_id
                left join issues i on i.id = w.issue_id
                left join queue_items q on q.wanted_id = w.id and q.active = 1
                where coalesce(q.active, 1) = 1
                  and coalesce(q.state, w.status) not in (
                      'verified','satisfied','inactive','stale_source_absent',
                      'ignored','unmonitored','removed'
                  )
                  and not exists (
                      select 1
                      from queue_items q_done
                      left join issues i_done on i_done.id = q_done.issue_id
                      where q_done.series_id = w.series_id
                        and coalesce(q_done.state, '') in ('verified','satisfied')
                        and coalesce(i_done.normalized_number, i_done.issue_number, '') = coalesce(i.normalized_number, i.issue_number, '')
                  )
                  and coalesce(i.issue_number, i.normalized_number, '') <> ''
                order by s.title, i.normalized_number, i.issue_number
                limit ?
                """,
                (max(1, int(limit or 5000)),),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            con.close()
    except Exception as exc:
        audit("inkdrop_pack_content_wanted_rows_failed", {"error": f"{type(exc).__name__}: {exc}"})
        return []


def pack_content_row_dedupe_keys(row):
    row = row if isinstance(row, dict) else {}
    keys = []
    issue_key = normalized_number(row.get("issue_number") or row.get("normalized_number"))
    series_id = str(row.get("series_id") or "").strip()
    issue_id = str(row.get("issue_id") or "").strip()
    if series_id or issue_id:
        keys.append(("id", series_id, issue_id or issue_key))
    title_key = normalize(row.get("title"))
    if title_key and issue_key:
        keys.append(("title", title_key, issue_key))
    return [key for key in keys if any(str(part or "").strip() for part in key[1:])]


def pack_contents_match_for_wanted_rows(trigger_row, result, max_rows=5000):
    entries = pack_content_entry_candidates(result)
    if not entries:
        return None
    wanted_rows = active_inkdrop_wanted_rows_for_pack_contents(limit=max_rows)
    rows = list(wanted_rows or [])
    trigger_row = trigger_row if isinstance(trigger_row, dict) else {}
    if trigger_row.get("title") and trigger_row.get("issue_number"):
        rows.append({
            "title": trigger_row.get("title"),
            "issue_number": trigger_row.get("issue_number"),
            "normalized_number": trigger_row.get("normalized_number") or trigger_row.get("calculated_issue_number"),
            "series_id": trigger_row.get("series_id"),
            "issue_id": trigger_row.get("issue_id"),
            "wanted_id": trigger_row.get("wanted_id"),
            "queue_id": trigger_row.get("queue_id"),
            "media_type": trigger_row.get("media_type"),
            "publisher": trigger_row.get("publisher"),
            "metadata_provider": trigger_row.get("metadata_provider"),
            "metadata_id": trigger_row.get("metadata_id"),
            "presence": "trigger_wanted",
        })
    matches = []
    seen = set()
    for row in rows:
        row_keys = pack_content_row_dedupe_keys(row)
        if row_keys and any(key in seen for key in row_keys):
            continue
        for entry in entries:
            match = pack_content_entry_matches_row(row, entry)
            if not match:
                continue
            for key in row_keys:
                seen.add(key)
            sample = pack_content_match_sample(row, match)
            sample["matching_entry"] = match.get("entry")
            matches.append(sample)
            break
    if not matches:
        return None
    series_seen = []
    queue_ids = []
    for sample in matches:
        series = sample.get("series")
        if series and series not in series_seen:
            series_seen.append(series)
        queue_id = sample.get("queue_id")
        if queue_id and queue_id not in queue_ids:
            queue_ids.append(queue_id)
    return {
        "useful_missing_count": len(matches),
        "already_present_count": 0,
        "unknown_unmatched_count": 0,
        "useful_missing_sample": matches[:100],
        "already_present_sample": [],
        "coverage_source": "pack_contents_filename",
        "content_entry_count": len(entries),
        "matching_entry": matches[0].get("matching_entry"),
        "covered_series_count": len(series_seen),
        "covered_series": series_seen[:50],
        "covered_queue_ids": queue_ids[:200],
        "multi_series": len(series_seen) > 1,
    }


def pack_contents_match_for_row(row, result):
    entries = pack_content_entry_candidates(result)
    for entry in entries:
        match = pack_content_entry_matches_row(row, entry)
        if not match:
            continue
        return {
            "useful_missing_count": 1,
            "already_present_count": 0,
            "unknown_unmatched_count": 0,
            "useful_missing_sample": [
                pack_content_match_sample(row, match)
            ],
            "already_present_sample": [],
            "coverage_source": "pack_contents_filename",
            "content_entry_count": len(entries),
            "matching_entry": match.get("entry"),
        }
    return None


def series_collection_release_match_for_row(row, raw_title):
    row = row if isinstance(row, dict) else {}
    match = series_collection_entry_match(row.get("title"), raw_title)
    if not match:
        return None
    issue = row.get("issue_number")
    if issue_int(issue) is None:
        return None
    return {
        "useful_missing_count": 1,
        "already_present_count": 0,
        "unknown_unmatched_count": 0,
        "useful_missing_sample": [
            {
                "issue": issue,
                "calculated": row.get("normalized_number") or row.get("calculated_issue_number") or issue,
                "presence": row.get("presence") or "trigger_wanted",
                "match": "series_collection_release_title",
                "file_entry": match.get("basename"),
                "collection_entry": True,
                "collection_marker": match.get("collection_marker"),
            }
        ],
        "already_present_sample": [],
        "coverage_source": "series_collection_release_title",
        "matching_entry": match.get("entry"),
        "collection_entry": True,
        "collection_marker": match.get("collection_marker"),
    }


def pack_decision(pack_match, result):
    useful = int((pack_match or {}).get("useful_missing_count") or 0)
    present = int((pack_match or {}).get("already_present_count") or 0)
    unknown = int((pack_match or {}).get("unknown_unmatched_count") or 0)
    seeders = int((result or {}).get("seeders") or 0)
    protocol = (result or {}).get("protocol")
    if useful <= 0 and unknown <= 0:
        state = "not_useful"
        recommendation = "Ignore"
        summary = f"covers 0 missing items; {present} already present"
    elif useful > 0 and unknown == 0:
        state = "useful_review_candidate"
        recommendation = "Review / approve pack"
        summary = f"may satisfy {useful} missing item(s); {present} already present"
    else:
        state = "needs_manual_review"
        recommendation = "Review pack details"
        summary = f"may satisfy {useful} missing item(s); {present} already present; {unknown} unknown"
    score = min(100, 50 + useful * 6 + min(seeders, 20) - present - unknown * 4)
    has_seeders = protocol != "torrent" or seeders >= 1
    safe_to_auto_approve = state == "useful_review_candidate" and has_seeders
    if safe_to_auto_approve:
        recommendation = "Auto-approve pack"
    if protocol == "torrent" and seeders < 1:
        score -= 30
        summary += "; torrent has no visible seeders"
    return {
        "state": state,
        "recommendation": recommendation,
        "summary": summary,
        "score": max(0, score),
        "safe_to_auto_approve": safe_to_auto_approve,
    }


def pack_candidate_sort_key(item):
    pack_match = item.get("pack_match") or {}
    candidate = item.get("candidate") or {}
    english = item.get("english_confidence") or {}
    english_score = int(english.get("score") or 0)
    candidate_protocol_rank = protocol_rank(candidate.get("protocol"))
    return (
        -(pack_match.get("useful_missing_count") or 0),
        pack_match.get("unknown_unmatched_count") or 0,
        pack_match.get("already_present_count") or 0,
        candidate_protocol_rank,
        -english_score,
        -(candidate.get("seeders") or 0),
    )


def pack_candidate_identity(item):
    candidate = item.get("candidate") or {}
    return (
        normalize(item.get("series")),
        normalized_number(item.get("issue")),
        normalize(candidate.get("title")),
        str(candidate.get("indexer") or candidate.get("source") or ""),
        str(candidate.get("protocol") or ""),
    )


def pack_release_identity(item):
    candidate = item.get("candidate") or {}
    pack_info = item.get("pack_info") or {}
    pack_match = item.get("pack_match") or {}
    broad_manifest_pack = bool(
        WEEKLY_COMICS_PACK_RE.search(candidate.get("title") or item.get("title") or item.get("query") or "")
        or (
            pack_match.get("coverage_source") == "pack_contents_filename"
            and pack_match.get("multi_series")
        )
    )
    return (
        "" if broad_manifest_pack else normalize(item.get("series")),
        normalize(candidate.get("title")),
        normalize(pack_info.get("summary") or pack_match.get("summary") or ""),
        str(candidate.get("indexer") or candidate.get("source") or candidate.get("indexerId") or ""),
        str(candidate.get("protocol") or ""),
    )


def pack_range_contains_issue(pack_info, issue_number):
    issue = issue_int(issue_number)
    if issue is None:
        return False
    for row in (pack_info or {}).get("ranges") or []:
        if not isinstance(row, dict):
            continue
        try:
            start = int(float(row.get("start")))
            end = int(float(row.get("end")))
        except (TypeError, ValueError):
            continue
        low, high = sorted((start, end))
        if low <= issue <= high:
            return True
    return False


def pack_match_sample_contains_issue(pack_match, issue_number, series=None):
    issue_key = normalized_number(issue_number)
    if not issue_key:
        return False
    series_key = normalize(series)
    for row in (pack_match or {}).get("useful_missing_sample") or []:
        if not isinstance(row, dict):
            continue
        row_series = normalize(row.get("series"))
        if series_key and row_series and row_series != series_key:
            continue
        if normalized_number(row.get("issue")) == issue_key or normalized_number(row.get("calculated")) == issue_key:
            return True
    return False


def pack_covers_trigger_issue(item):
    pack_info = item.get("pack_info") or {}
    pack_match = item.get("pack_match") or {}
    issue = item.get("issue")
    if pack_match.get("coverage_source") == "pack_contents_filename":
        return pack_match_sample_contains_issue(pack_match, issue, series=item.get("series"))
    if pack_match_sample_contains_issue(pack_match, issue, series=item.get("series")):
        return True
    if pack_range_contains_issue(pack_info, issue):
        return True
    if pack_match.get("coverage_source") == "collected_edition_story_title":
        return True
    return bool(pack_match.get("coverage_source") in {"complete_keyword", "inkdrop_complete_keyword"} and not pack_info.get("ranges"))


def inkdrop_queue_row_for_pack_match_sample(sample):
    sample = sample if isinstance(sample, dict) else {}
    if not INKDROP_STATE_DB.exists():
        return None
    queue_id = str(sample.get("queue_id") or "").strip()
    wanted_id = str(sample.get("wanted_id") or "").strip()
    series_id = str(sample.get("series_id") or "").strip()
    issue_id = str(sample.get("issue_id") or "").strip()
    series_title = str(sample.get("series") or "").strip()
    issue_text = str(sample.get("issue") or sample.get("calculated") or "").strip()
    issue_key = normalized_number(issue_text)
    if not any((queue_id, wanted_id, series_id and issue_id, series_title and issue_key)):
        return None
    clauses = []
    params = []
    if queue_id:
        clauses.append("q.id = ?")
        params.append(queue_id)
    if wanted_id:
        clauses.append("w.id = ?")
        params.append(wanted_id)
    if series_id and issue_id:
        clauses.append("(s.id = ? and i.id = ?)")
        params.extend([series_id, issue_id])
    if series_title and issue_key:
        clauses.append(
            "(lower(s.title) = lower(?) and (i.issue_number = ? or i.normalized_number = ? or i.normalized_number = ?))"
        )
        params.extend([series_title, issue_text, issue_text, issue_key])
    try:
        con = inkdrop_db.open_connection(
            INKDROP_STATE_DB,
            readonly=True,
            operation="missing_acquire_queue_lookup",
        )
        try:
            row = con.execute(
                f"""
                select q.id as queue_id, q.state as queue_state, q.current_source,
                       q.query, q.raw_json as queue_raw_json,
                       s.id as series_id, s.title, s.publisher, s.year,
                       s.source as series_source, s.metadata_provider, s.metadata_id,
                       s.kapowarr_id,
                       i.id as state_issue_id, i.issue_number, i.normalized_number,
                       i.title as issue_title, i.release_date,
                       i.metadata_id as issue_metadata_id, i.kapowarr_issue_id
                from queue_items q
                join wanted_items w on w.id = q.wanted_id
                join series s on s.id = q.series_id
                left join issues i on i.id = q.issue_id
                where q.active = 1
                  and q.state in ('queued','searching')
                  and ({' or '.join(clauses)})
                order by case
                    when q.id = ? then 0
                    when w.id = ? then 1
                    else 2
                end, q.updated_at desc
                limit 1
                """,
                [*params, queue_id, wanted_id],
            ).fetchone()
        finally:
            con.close()
    except Exception as exc:
        audit("pack_manifest_retarget_lookup_failed", {"error": f"{type(exc).__name__}: {exc}", "sample": sample})
        return None
    return inkdrop_missing_row_from_queue_record(row) if row else None


def retarget_pack_candidate_to_manifest_wanted(item):
    item = item if isinstance(item, dict) else {}
    if pack_covers_trigger_issue(item):
        return item, None
    pack_match = item.get("pack_match") if isinstance(item.get("pack_match"), dict) else {}
    if pack_match.get("coverage_source") != "pack_contents_filename":
        return item, None
    trigger_issue_key = normalized_number(item.get("issue"))
    samples = list(pack_match.get("useful_missing_sample") or [])
    samples.sort(
        key=lambda sample: (
            0
            if trigger_issue_key
            and (
                normalized_number((sample or {}).get("issue")) == trigger_issue_key
                or normalized_number((sample or {}).get("calculated")) == trigger_issue_key
            )
            else 1
        )
    )
    for sample in samples:
        row = inkdrop_queue_row_for_pack_match_sample(sample)
        if not row:
            continue
        retargeted = dict(item)
        original_context = {
            "series": item.get("series"),
            "issue": item.get("issue"),
            "queue_id": item.get("queue_id"),
            "wanted_id": item.get("wanted_id"),
            "issue_id": item.get("issue_id"),
        }
        retargeted.update({
            "series": row.get("title"),
            "issue": row.get("issue_number"),
            "volume_id": row.get("volume_id"),
            "folder": row.get("folder"),
            "manifest_retargeted": True,
            "manifest_retarget_reason": "pack_covers_another_active_wanted_row",
            "manifest_trigger_context": original_context,
            "manifest_retarget_context": row_output_context(row),
        })
        retargeted.update(row_output_context(row))
        return retargeted, {
            "row": row,
            "from": original_context,
            "to": row_output_context(row),
            "matching_entry": sample.get("matching_entry") or sample.get("file_entry"),
        }
    return item, None


def prepare_pack_candidate_for_action(item, row, row_context):
    item = dict(item or {})
    action_row = row
    action_context = dict(row_context or {})
    item.update(action_context)
    retargeted_item, retarget = retarget_pack_candidate_to_manifest_wanted(item)
    if retarget:
        item = dict(retargeted_item)
        action_row = retarget.get("row") or row
        action_context = row_output_context(action_row)
        item.update(action_context)
        item["manifest_retarget"] = {
            "from": retarget.get("from"),
            "to": retarget.get("to"),
            "matching_entry": retarget.get("matching_entry"),
        }
    return item, action_row, action_context, retarget


def unique_pack_candidates(items):
    seen = set()
    out = []
    for item in items or []:
        key = pack_candidate_identity(item)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def collected_unit_numbers_for_issue(issue_number):
    n = issue_int(issue_number)
    if n is None or n <= 0:
        return [1]
    # Most modern comic trades collect roughly 5-6 issues. Use a small
    # neighborhood only for search discovery; import still requires exact
    # pack/member evidence or story-title matching before automation.
    estimated = max(1, int((n - 1) // 5) + 1)
    out = []
    for value in (estimated, estimated - 1, estimated + 1, 1):
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number <= 0 or number in out:
            continue
        out.append(number)
    return out


def series_pack_queries(title, alt_titles=(), unit_model=None, issue_number=None):
    queries = []
    for name in expanded_search_titles(title, alt_titles):
        queries.append(name)
        if (unit_model or "").lower() == "chapter":
            queries.extend([f"{name} chapters", f"{name} complete"])
        else:
            queries.extend([
                f"{name} book",
                f"{name} complete",
            ])
            unit_numbers = [number for number in collected_unit_numbers_for_issue(issue_number) if number and number > 0]
            for number in unit_numbers[:3]:
                queries.extend([
                    f"{name} v{number:02d}",
                    f"{name} vol {number}",
                    f"{name} volume {number}",
                    f"{name} book {number}",
                ])
            queries.extend([
                f"{name} tpb",
                f"{name} trade paperback",
                f"{name} hardcover",
                f"{name} hc",
                f"{name} omnibus",
            ])
    return unique(queries)


def weekly_pack_queries(row):
    publisher = normalize((row or {}).get("publisher"))
    try:
        current_year = int(time.localtime().tm_year)
    except Exception:
        current_year = None
    explicit_years = []
    release_date = parse_release_date((row or {}).get("release_date"))
    if release_date:
        explicit_years.append(release_date.year)
    for value in ((row or {}).get("year"),):
        try:
            year = int(value)
        except (TypeError, ValueError):
            continue
        if 2000 <= year <= 2100 and year not in explicit_years:
            explicit_years.append(year)
    if not publisher and not explicit_years:
        return []
    years = list(explicit_years)
    current_year_is_relevant = bool(current_year and (
        not years
        or any(abs(current_year - year) <= 1 for year in years)
    ))
    if current_year_is_relevant:
        if current_year not in years:
            years.append(current_year)
    queries = []
    date_queries = weekly_pack_date_queries(row)
    if current_year_is_relevant:
        queries.extend(date_queries)
    if "dc" in publisher:
        queries.append("DC Week")
        for year in years:
            queries.append(f"DC Comics Weekly Releases {year}")
            queries.append(f"DC Comics {year}")
            queries.append(f"Complete DC Comics - {year}")
            if 2000 <= year <= 2004:
                queries.append("DC 2000 - 2004")
    elif "image" in publisher:
        queries.append("Image Week")
        for year in years:
            queries.append(f"Image Comics Weekly Releases {year}")
            queries.append(f"Image Comics {year}")
    elif publisher:
        for year in years:
            queries.append(f"{(row or {}).get('publisher')} Weekly Releases {year}")
            queries.append(f"{(row or {}).get('publisher')} {year}")
    if not current_year_is_relevant:
        queries.extend(date_queries)
    for year in years:
        queries.append(f"Weekly Comics Pack {year}")
    queries.append("Weekly Comics Pack")
    return unique_weekly_pack_queries(queries)


def should_try_weekly_pack_before_issue(row, is_manga=False, unit_model=None):
    if is_manga or str(unit_model or "").strip().lower() == "chapter":
        return False
    if not weekly_pack_queries(row):
        return False
    if parse_release_date((row or {}).get("release_date")):
        return True
    publisher = normalize((row or {}).get("publisher"))
    return any(token in publisher for token in ("dc", "marvel", "image", "dark horse", "idw"))


def parse_release_date(value):
    text = str(value or "").strip()
    if not text:
        return None
    match = re.search(r"\b(20\d{2})[-./](\d{1,2})[-./](\d{1,2})\b", text)
    if not match:
        return None
    try:
        return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3))).date()
    except ValueError:
        return None


def weekly_pack_date_queries(row, max_queries=5):
    release_date = parse_release_date((row or {}).get("release_date"))
    if not release_date:
        return []
    # Weekly comic-pack titles usually use the release Wednesday. Try the exact
    # release date first, then nearby Wednesdays so metadata/date drift does not
    # force broad year-pack results to win.
    candidate_dates = []
    for offset in (0, -7, 7, -14, 14, -21, 21):
        current = release_date + timedelta(days=offset)
        if current.weekday() == 2 and current not in candidate_dates:
            candidate_dates.append(current)
    if release_date.weekday() != 2:
        days_back = (release_date.weekday() - 2) % 7
        nearest = release_date - timedelta(days=days_back)
        for offset in (0, 7, -7, 14, -14):
            current = nearest + timedelta(days=offset)
            if current not in candidate_dates:
                candidate_dates.append(current)
    queries = []
    for date_value in candidate_dates[: max(1, int(max_queries or 5))]:
        queries.append(f"{date_value:%Y-%m-%d} Weekly Comics Pack")
        queries.append(f"{date_value:%Y-%m-%d} Weekly Pack")
    return unique_weekly_pack_queries(queries)


def unique_weekly_pack_queries(values):
    seen = set()
    out = []
    for value in values or []:
        text = re.sub(r"\s+", " ", str(value or "").strip())
        key = normalize(text)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def years_from_text(value):
    years = []
    for match in re.finditer(r"\b(20\d{2})\b", str(value or "")):
        try:
            year = int(match.group(1))
        except ValueError:
            continue
        if 2000 <= year <= 2100 and year not in years:
            years.append(year)
    return years


def weekly_pack_result_date(result):
    result = result if isinstance(result, dict) else {}
    for key in ("title", "name", "publishDate", "publishedAt", "date"):
        parsed = parse_release_date(result.get(key))
        if parsed:
            return parsed
    return None


def weekly_pack_result_sort_key(row, result):
    try:
        current_year = int(time.localtime().tm_year)
    except Exception:
        current_year = None
    release_date = parse_release_date((row or {}).get("release_date"))
    pack_date = weekly_pack_result_date(result)
    if release_date and pack_date:
        date_missing = 0
        date_distance = abs((pack_date - release_date).days)
    else:
        date_missing = 1
        date_distance = 9999
    target_years = []
    for value in (
        release_date.year if release_date else None,
        (row or {}).get("year"),
        current_year,
    ):
        try:
            year = int(value)
        except (TypeError, ValueError):
            continue
        if 2000 <= year <= 2100 and year not in target_years:
            target_years.append(year)
    years = years_from_text((result or {}).get("title"))
    years.extend(year for year in years_from_text((result or {}).get("publishDate")) if year not in years)
    if years and target_years:
        year_distance = min(abs(year - target) for year in years for target in target_years)
        newest_year = max(years)
    else:
        year_distance = 99
        newest_year = 0
    target_year_hit = 0 if target_years and any(year in target_years for year in years) else 1
    return (
        date_missing,
        date_distance,
        target_year_hit,
        year_distance,
        -newest_year,
        protocol_rank((result or {}).get("protocol")),
        -int((result or {}).get("seeders") or 0),
        int((result or {}).get("size") or 0),
        normalize((result or {}).get("title")),
    )


def make_pack_candidate(acquire, row, query, result, source, is_manga=False, unit_model=None, quality_rules=None):
    title = row.get("title")
    issue = row.get("issue_number")
    raw_title = result.get("title")
    content_match = pack_contents_match_for_wanted_rows(row, result)
    title_matches_release = series_title_starts_release(title, raw_title)
    if not title_matches_release and not content_match:
        return None
    title_blocker = (
        related_subseries_title_blocker(
            title,
            raw_title,
            issue_title=row.get("issue_title"),
            issue_number=issue,
            publisher=row.get("publisher"),
        )
        if title_matches_release
        else ""
    )
    if title_blocker and not content_match:
        audit("pack_candidate_rejected_related_subseries", {
            "series": title,
            "issue": issue,
            "query": query,
            "candidate": raw_title,
            "reason": title_blocker,
            "source": source,
        })
        return None
    sample = (
        {
            "title": raw_title,
            "indexer": result.get("indexer"),
            "protocol": result.get("protocol"),
            "seeders": result.get("seeders"),
            "categories": sorted(categories(result)),
            "score": 96,
            "reason": "pack contents filename matched wanted issue",
            "english_confidence": english_confidence(result),
        }
        if content_match and not title_matches_release
        else sample_result(title, issue, result, is_manga=is_manga, unit_model=unit_model, quality_rules=quality_rules)
    )
    pack_info = acquire.detect_pack_info(raw_title)
    if row.get("volume_id") in (None, "") and row.get("inkdrop_queue_row"):
        pack_match = inkdrop_pack_match_estimate(title, pack_info)
    else:
        pack_match = pack_match_estimate(row.get("volume_id"), pack_info)
    collected_match = collected_edition_pack_match_for_row(row, raw_title, pack_info)
    series_collection_match = series_collection_release_match_for_row(row, raw_title)
    has_explicit_range = any(
        isinstance(item, dict) and item.get("start") is not None and item.get("end") is not None
        for item in (pack_info.get("ranges") or [])
    )
    if content_match:
        pack_match = content_match
    elif collected_match and not has_explicit_range:
        pack_match = collected_match
    elif series_collection_match and not has_explicit_range:
        pack_match = series_collection_match
    decision = pack_decision(pack_match, result)
    if decision.get("state") == "not_useful":
        return None
    return {
        "series": title,
        "issue": issue,
        "volume_id": row.get("volume_id"),
        "folder": row.get("folder"),
        "query": query,
        "source": source,
        "candidate": candidate_payload(result),
        "english_confidence": acquire.classify_english_result(result),
        "pack_info": pack_info,
        "pack_match": pack_match,
        "pack_decision": decision,
        "confidence_score": sample.get("score"),
        "why": [
            sample.get("reason"),
            decision.get("summary"),
            (
                "clear English pack can be auto-approved"
                if decision.get("safe_to_auto_approve")
                else "pack/range requires review before approval"
            ),
        ],
    }


def add_pack_candidate(pack_candidates, acquire, row, query, result, source, is_manga=False, unit_model=None, limit=8, quality_rules=None):
    if len(pack_candidates) >= limit:
        return False
    if not is_pack_result(result.get("title")):
        return False
    candidate = make_pack_candidate(acquire, row, query, result, source, is_manga=is_manga, unit_model=unit_model, quality_rules=quality_rules)
    if not candidate:
        return False
    key = normalize((candidate.get("candidate") or {}).get("title"))
    if any(normalize((item.get("candidate") or {}).get("title")) == key for item in pack_candidates):
        return False
    pack_candidates.append(candidate)
    return True


def remaining_search_seconds(search_deadline):
    if search_deadline is None:
        return None
    return max(0.0, float(search_deadline - time.monotonic()))


def search_deadline_from_budget(search_budget_seconds, *, started_at=None):
    budget = max(0.0, float(search_budget_seconds or 0))
    if budget <= 0:
        return None
    origin = time.monotonic() if started_at is None else float(started_at)
    return origin + budget


def prowlarr_search_with_budget(acquire, query, media_type, args, *, search_deadline=None, limit=None):
    remaining = remaining_search_seconds(search_deadline)
    if remaining is not None and remaining <= 0:
        raise TimeoutError("search budget exhausted before Prowlarr request")
    timeout = getattr(args, "prowlarr_timeout_seconds", None)
    if remaining is not None:
        base_timeout = timeout if timeout is not None else env_float("INKDROP_PROWLARR_SEARCH_TIMEOUT_SECONDS", 12.0)
        timeout = max(1.0, min(remaining, float(base_timeout)))
    return acquire.prowlarr_search(
        query,
        media_type,
        limit=limit if limit is not None else args.limit,
        timeout_seconds=timeout,
    )


def prowlarr_exception_is_timeout(exc):
    text = redact_error(exc).lower()
    return isinstance(exc, TimeoutError) or "timed out" in text or "timeout" in text or "search budget exhausted" in text


def prowlarr_exception_attempt(exc):
    if prowlarr_exception_is_timeout(exc):
        return "retry_scheduled", "prowlarr_search_timeout"
    return "error", "prowlarr_search_error"


def collect_series_pack_candidates(
    acquire,
    row,
    cache,
    args,
    source,
    is_manga=False,
    unit_model=None,
    quality_rules=None,
    search_budget_exhausted=None,
    search_deadline=None,
    series_pack_result_cache=None,
    pack_detail_fetch_state=None,
    query_group_filter=None,
):
    candidates = []
    title = row.get("title")
    issue = row.get("issue_number")
    series_key = (
        normalize(title),
        normalize(row.get("alt_title")),
        str(unit_model or "").strip().lower(),
    )
    weekly_query_limit = env_int("INKDROP_WEEKLY_PACK_QUERY_LIMIT", 8, 1, 20)
    query_groups = [
        ("series_pack", limited_queries(series_pack_queries(title, alt_titles=[row.get("alt_title")], unit_model=unit_model, issue_number=issue), args)),
        ("weekly_pack", list(weekly_pack_queries(row))[:weekly_query_limit]),
    ]
    if query_group_filter:
        allowed_groups = {str(item or "").strip() for item in query_group_filter if str(item or "").strip()}
        query_groups = [(name, queries) for name, queries in query_groups if name in allowed_groups]
    for query_group, group_queries in query_groups:
        if (
            candidates
            and query_group == "weekly_pack"
            and any(pack_covers_trigger_issue(item) for item in candidates)
        ):
            break
        for query in group_queries:
            if callable(search_budget_exhausted) and search_budget_exhausted():
                audit("series_pack_search_budget_exhausted", {
                    "series": title,
                    "issue": issue,
                    "query": query,
                    "source": source,
                })
                break
            cache_key = (
                "weekly_pack",
                normalize(row.get("publisher")),
                normalize(query),
            ) if query_group == "weekly_pack" else (*series_key, normalize(query))
            cache_record = (series_pack_result_cache or {}).get(cache_key) if series_pack_result_cache is not None else None
            if cache_record is not None:
                results = list(cache_record.get("results") or [])
                if cache_record.get("error"):
                    audit("series_pack_search_cached_error", {
                        "series": title,
                        "issue": issue,
                        "query": query,
                        "source": source,
                        "error": cache_record.get("error"),
                    })
                    continue
            else:
                try:
                    results = prowlarr_search_with_budget(
                        acquire,
                        query,
                        "comics",
                        args,
                        search_deadline=search_deadline,
                        limit=args.limit,
                    )
                    if series_pack_result_cache is not None:
                        series_pack_result_cache[cache_key] = {
                            "query": query,
                            "results": list(results or []),
                            "error": "",
                        }
                except Exception as exc:
                    error = redact_error(exc)
                    if series_pack_result_cache is not None:
                        series_pack_result_cache[cache_key] = {
                            "query": query,
                            "results": [],
                            "error": error,
                        }
                    audit("series_pack_search_error", {
                        "series": title,
                        "issue": issue,
                        "query": query,
                        "error": error,
                    })
                    remaining = remaining_search_seconds(search_deadline)
                    if prowlarr_exception_is_timeout(exc) and (
                        (callable(search_budget_exhausted) and search_budget_exhausted())
                        or (remaining is not None and remaining <= 0)
                    ):
                        audit("series_pack_search_budget_exhausted_after_timeout", {
                            "series": title,
                            "issue": issue,
                            "query": query,
                            "source": source,
                        })
                        break
                    continue
            results, _quality_blocked_samples = filter_quality_allowed_results(
                acquire,
                row,
                title,
                issue,
                results,
                query=query,
                is_manga=is_manga,
                unit_model=unit_model,
                quality_rules=quality_rules,
                dry_run=args.dry_run,
            )
            results, _known_bad_samples = filter_known_bad_results(
                cache,
                row,
                title,
                issue,
                results,
                query=query,
                dry_run=args.dry_run,
            )
            if query_group == "weekly_pack":
                results = sorted(results, key=lambda result: weekly_pack_result_sort_key(row, result))
            for result in results:
                if is_pack_result(result.get("title")) and not pack_contents_match_for_row(row, result):
                    result = enrich_pack_result_with_detail(
                        acquire,
                        result,
                        cache,
                        args=args,
                        fetch_state=pack_detail_fetch_state,
                    )
                add_pack_candidate(
                    candidates,
                    acquire,
                    row,
                    query,
                    result,
                    source if query_group == "series_pack" else f"{source}_weekly",
                    is_manga=is_manga,
                    unit_model=unit_model,
                    quality_rules=quality_rules,
                )
        if callable(search_budget_exhausted) and search_budget_exhausted():
            break
    return candidates


def categories(result):
    ids = set()
    for category in result.get("categories") or []:
        if isinstance(category, dict):
            if category.get("id") is not None:
                ids.add(int(category["id"]))
            for sub in category.get("subCategories") or []:
                if isinstance(sub, dict) and sub.get("id") is not None:
                    ids.add(int(sub["id"]))
    for cid in result.get("categories") or []:
        if isinstance(cid, int):
            ids.add(cid)
    return ids


def _unicode_numeric_character(value):
    try:
        unicodedata.numeric(value)
        return True
    except (TypeError, ValueError):
        return False


def _safe_exact_unit_suffix(value, *, allow_volume_reference=False):
    suffix = str(value or "")
    metadata_suffix = re.sub(r"\.(?:cbz|cbr|pdf|epub|zip|rar|7z)\s*$", "", suffix, flags=re.I)
    non_year_suffix = re.sub(r"(?<!\d)(?:19|20)\d{2}(?!\d)", "", metadata_suffix)
    if allow_volume_reference:
        non_year_suffix = re.sub(r"\bv\.?\s*0*\d{1,4}\b|\bvol(?:ume)?\.?\s*0*\d{1,4}\b", "", non_year_suffix, flags=re.I)
    if any(char.isdigit() or _unicode_numeric_character(char) for char in non_year_suffix):
        return False
    if not re.fullmatch(
        r"(?:\s*(?:\([^()]*\)|\[[^\[\]]*\]|\{[^{}]*\}))*\s*(?:\.(?:cbz|cbr|pdf|epub|zip|rar|7z))?\s*",
        suffix,
        re.I,
    ):
        return False
    safe_descriptors = {
        "digital", "retail", "official", "english", "eng", "hq", "hd",
        "web", "webdl", "scan", "scans", "fixed", "repack", "cbz", "cbr",
        "pdf", "epub", "jko", "fullcolor", "darkhorse",
    }
    for group in re.findall(r"\(([^()]*)\)|\[([^\[\]]*)\]|\{([^{}]*)\}", suffix):
        content = next((part.strip() for part in group if part), "")
        compact = re.sub(r"[\s_-]+", "", content).lower()
        if re.fullmatch(r"(?:19|20)\d{2}", content):
            continue
        if compact in safe_descriptors:
            continue
        # A single volume-reference tag (e.g. "(v01)") on a manga chapter release
        # just states which collected volume the chapter belongs to -- it is not
        # evidence of a multi-item pack. Only allowed for the chapter-suffix
        # check; the volume-suffix check below never opts in, so a genuine
        # volume release still can't dodge the multi-volume-range checks.
        if allow_volume_reference and re.fullmatch(r"v\.?0*\d{1,4}|vol(?:ume)?\.?0*\d{1,4}", compact):
            continue
        return False
    return True


def result_quality(
    title,
    issue_number,
    result,
    is_manga=False,
    unit_model=None,
    quality_rules=None,
    wanted_unit_type=None,
):
    n = issue_int(issue_number)
    if n is None:
        return {"acceptable": False, "score": 0, "reason": "issue number is not numeric"}
    english = english_confidence(result)
    rule_blocker = quality_rule_block_reason(result, quality_rules, english=english)
    if rule_blocker:
        if str(rule_blocker).startswith("blocked_release_term:"):
            reason = "Blocked by quality/language settings: " + str(rule_blocker).split(":", 1)[1].strip()
        elif rule_blocker == "english_not_confirmed":
            reason = f"English gate rejected: {english.get('reason')}"
        elif rule_blocker == "pdf_disabled_by_quality_rules":
            reason = "PDF disabled by quality/language settings"
        else:
            reason = "extension disabled by quality/language settings"
        return {
            "acceptable": False,
            "score": 0,
            "reason": reason,
            "english_confidence": english,
            "quality_language_rules": quality_rule_summary(quality_rules),
        }
    raw = result.get("title") or ""
    series_pattern = title_words_pattern(title)
    if not series_pattern:
        return {"acceptable": False, "score": 0, "reason": "series title is empty"}
    match_raw = strip_release_prefixes(raw)
    if not series_title_starts_release(title, raw):
        return {
            "acceptable": False,
            "score": 0,
            "reason": "series title prefix mismatch",
            "english_confidence": english,
        }

    unit_model = (unit_model or "").lower()
    wanted_unit_type = str(wanted_unit_type or "").strip().lower()
    mixed_chapter_target = unit_model == "mixed_chapter_preferred" and wanted_unit_type in {"chapter", "oneshot"}
    target_volume_match = re.search(
        rf"^\s*{series_pattern}[\W_]+(?:v|vol(?:ume)?\.?)[\W_]*0*{n}(?=[^0-9]|$)",
        match_raw,
        re.I,
    )
    target_volume_suffix = match_raw[target_volume_match.end() :] if target_volume_match else ""
    # Release metadata may sit between the requested marker and a second unit
    # (for example `v38 (2026) & 39`).  Inspect the full remaining suffix,
    # rather than only an immediately adjacent range spelling.
    multi_volume_suffix = re.search(
        r"(?:[-–+&,/]|\band\b|\bto\b)\s*(?:(?:v|vol(?:ume)?\.?)\s*)?0*\d+\b",
        target_volume_suffix,
        re.I,
    )
    repeated_volume_marker = len(re.findall(r"\b(?:v|vol(?:ume)?\.?)\s*0*\d+\b", match_raw, re.I)) > 1
    exact_single_volume = bool(
        is_manga
        and unit_model in {"volume", "pack", "mixed_volume_preferred"}
        and target_volume_match
        and not multi_volume_suffix
        and not repeated_volume_marker
        and _safe_exact_unit_suffix(target_volume_suffix)
        and not re.search(
            r"\b(?:complete|omnibus|compendium|collection)\b|\b(?:v|vol(?:ume)?\.?)?\s*\d+\s*[-–+]\s*(?:v|vol(?:ume)?\.?)?\s*\d+",
            match_raw,
            re.I,
        )
    )
    target_chapter_match = re.search(
        rf"^\s*{series_pattern}[\W_]+(?:chapter[\W_]+|ch\.?[\W_]+|c)0*{n}(?=[^0-9]|$)",
        match_raw,
        re.I,
    )
    target_chapter_suffix = match_raw[target_chapter_match.end() :] if target_chapter_match else ""
    exact_single_chapter = bool(
        is_manga
        and (unit_model == "chapter" or mixed_chapter_target)
        and target_chapter_match
        # A chapter release may carry a "(v01)"-style collected-volume reference
        # tag alongside it -- that's not evidence of a multi-item pack.
        and _safe_exact_unit_suffix(target_chapter_suffix, allow_volume_reference=True)
    )

    # Single-issue matching stays strict; pack automation evaluates pack
    # coverage separately so useful ranges can fill multiple missing rows.
    # A single vNN release is not a pack when the trusted target is that manga
    # volume, and likewise a single cNN release is not a pack when the trusted
    # target is that manga chapter.  The broad historical pack detector
    # intentionally still catches it for comics and for range/omnibus/collection
    # targets.
    if is_pack_result(raw) and not exact_single_volume and not exact_single_chapter:
        return {"acceptable": False, "score": 72, "reason": "pack/range candidate", "english_confidence": english}

    if title.lower() == "saga":
        if not re.search(r"^saga([^a-z0-9]|$)", match_raw, re.I):
            return {"acceptable": False, "score": 0, "reason": "Saga title boundary mismatch", "english_confidence": english}
        if re.search(r"saga\\s+of|secret\\s+saga|spectacular.*saga", match_raw, re.I):
            return {"acceptable": False, "score": 0, "reason": "franchise-adjacent Saga result", "english_confidence": english}

    # Unattended grabs must match the exact series title followed immediately by
    # the requested issue/volume number. This rejects franchise-adjacent titles
    # like "Invincible Universe" or "The Invincible Red Sonja" for Invincible #1.
    issue_patterns = [
        rf"^\s*{series_pattern}[\W_]+#?\s*0*{n}([^0-9]|$)",
        rf"^\s*{series_pattern}[\W_]+issue[\W_]+0*{n}([^0-9]|$)",
    ]
    volume_patterns = [
        rf"^\s*{series_pattern}[\W_]+v0*{n}([^0-9]|$)",
        rf"^\s*{series_pattern}[\W_]+vol(?:ume)?\.?[\W_]+0*{n}([^0-9]|$)",
    ]
    chapter_patterns = [
        rf"^\s*{series_pattern}[\W_]+chapter[\W_]+0*{n}([^0-9]|$)",
        rf"^\s*{series_pattern}[\W_]+ch\.?[\W_]+0*{n}([^0-9]|$)",
        # Bare "c" fused directly to the number (no separator between the two,
        # e.g. "Series - c001") is a common scanlation/scene convention distinct
        # from "ch 001" / "ch. 001", which always have a separator before the digits.
        rf"^\s*{series_pattern}[\W_]+c0*{n}([^0-9]|$)",
    ]
    if is_manga and (unit_model == "chapter" or mixed_chapter_target):
        title_issue_patterns = []
        if exact_single_chapter:
            title_issue_patterns.extend(chapter_patterns)
        if mixed_chapter_target and exact_single_volume:
            title_issue_patterns.extend(volume_patterns)
    elif is_manga and unit_model in {"volume", "pack", "mixed_volume_preferred"}:
        # The strict grammar above is the only unattended acceptance path for
        # a manga volume.  Never fall through to the historical loose volume
        # regex after strict validation rejects an ambiguous suffix.
        title_issue_patterns = volume_patterns if exact_single_volume else []
    elif is_manga:
        return {
            "acceptable": False,
            "score": 0,
            "reason": "manga unit model is not an explicit chapter or volume target",
            "english_confidence": english,
        }
    else:
        title_issue_patterns = issue_patterns + volume_patterns + chapter_patterns
    if any(re.search(p, match_raw, re.I) for p in title_issue_patterns):
        score = 92
        cats = categories(result)
        if 7030 in cats:
            score += 4
        if 7000 in cats:
            score += 3
        if result.get("protocol") == "torrent" and (result.get("seeders") or 0) < 1:
            score -= 35
        return {
            "acceptable": score >= 80,
            "score": min(100, score),
            "reason": "exact title and issue/volume match",
            "source_unit": "chapter" if exact_single_chapter else "volume" if exact_single_volume else "issue",
            "english_confidence": english,
        }
    return {"acceptable": False, "score": 45, "reason": "not an exact single issue/volume match", "english_confidence": english}


def acceptable_result(title, issue_number, result, is_manga=False, unit_model=None, quality_rules=None, wanted_unit_type=None):
    return result_quality(
        title, issue_number, result, is_manga, unit_model,
        quality_rules=quality_rules, wanted_unit_type=wanted_unit_type,
    ).get("acceptable", False)


def choose_acceptable(title, issue_number, results, is_manga=False, unit_model=None, quality_rules=None, wanted_unit_type=None):
    accepted = [r for r in results if acceptable_result(
        title, issue_number, r, is_manga, unit_model,
        quality_rules=quality_rules, wanted_unit_type=wanted_unit_type,
    )]
    if not accepted:
        return None
    eligible = [
        r for r in accepted
        if normalize_protocol_name(r.get("protocol")) == "usenet"
        or (normalize_protocol_name(r.get("protocol")) == "torrent" and (r.get("seeders") or 0) >= 1)
    ]
    if not eligible:
        return None
    selected = sorted(
        eligible,
        key=lambda r: (
            protocol_rank(r.get("protocol")),
            -(r.get("seeders") or 0) if normalize_protocol_name(r.get("protocol")) == "torrent" else 0,
            r.get("size") or 0,
        ),
    )[0]
    quality = result_quality(
        title, issue_number, selected, is_manga, unit_model,
        quality_rules=quality_rules, wanted_unit_type=wanted_unit_type,
    )
    if is_manga and quality.get("source_unit") in {"chapter", "volume"}:
        selected = {**selected, "source_unit": quality["source_unit"]}
    return selected


def mixed_chapter_supersession_follow_up(unit_model, source_unit):
    if str(unit_model or "").strip().lower() != "mixed_chapter_preferred":
        return None
    if str(source_unit or "").strip().lower() != "chapter":
        return None
    return {
        "state": "future_volume_manifest_coverage_unproven",
        "automatic_replacement_supported": False,
        "chapter_artifact_deletion_allowed": False,
        "required_evidence": "artifact metadata or manifest must prove the later volume contains the exact chapter identity",
        "next_action": "retain chapter artifacts until exact chapter coverage and reader visibility are verified",
    }


def sample_result(title, issue_number, result, is_manga=False, unit_model=None, quality_rules=None, wanted_unit_type=None):
    quality = result_quality(
        title, issue_number, result, is_manga, unit_model,
        quality_rules=quality_rules, wanted_unit_type=wanted_unit_type,
    )
    return {
        "title": result.get("title"),
        "indexer": result.get("indexer"),
        "protocol": result.get("protocol"),
        "seeders": result.get("seeders"),
        "categories": sorted(categories(result)),
        "score": quality.get("score"),
        "reason": quality.get("reason"),
        "source_unit": quality.get("source_unit"),
        "english_confidence": quality.get("english_confidence") or english_confidence(result),
    }


def quality_block_attempt_status(blocker):
    text = str(blocker or "").lower()
    if "english" in text or "language" in text or "release_term" in text:
        return "language_blocked"
    return "quality_blocked"


def quality_block_message(blocker, english=None):
    text = str(blocker or "").strip()
    if text.startswith("blocked_release_term:"):
        return "blocked release term: " + text.split(":", 1)[1].strip()
    if text == "english_not_confirmed":
        reason = (english or {}).get("reason") if isinstance(english, dict) else ""
        return f"English gate rejected: {reason}" if reason else "English gate rejected"
    if text == "pdf_disabled_by_quality_rules":
        return "PDF disabled by quality/language settings"
    if text == "cover_only_artifact":
        return "cover-only artifact"
    if text.startswith("extension_"):
        return "extension disabled by quality/language settings"
    return text or "blocked by quality/language settings"


def filter_quality_allowed_results(
    acquire,
    row,
    title,
    issue_number,
    results,
    *,
    query=None,
    is_manga=False,
    unit_model=None,
    quality_rules=None,
    dry_run=False,
):
    allowed = []
    blocked_samples = []
    recorded = 0
    for result in results or []:
        english = acquire.classify_english_result(result) if hasattr(acquire, "classify_english_result") else english_confidence(result)
        blocker = quality_rule_block_reason(result, quality_rules, english=english)
        if not blocker:
            allowed.append(result)
            continue
        sample = sample_result(title, issue_number, result, is_manga=is_manga, unit_model=unit_model, quality_rules=quality_rules)
        sample["quality_blocker"] = blocker
        sample["quality_block_reason"] = quality_block_message(blocker, english=english)
        blocked_samples.append(sample)
        if recorded < 3:
            record_inkdrop_queue_attempt(
                row,
                quality_block_attempt_status(blocker),
                quality_block_message(blocker, english=english),
                query=query,
                candidate=result,
                dry_run=dry_run,
                extra={
                    "quality_blocker": blocker,
                    "english_confidence": english,
                    "quality_language_rules": quality_rule_summary(quality_rules),
                },
            )
            recorded += 1
    return allowed, blocked_samples


def send_failure_reason(exc, default):
    text = redact_error(exc).lower()
    if "duplicate nzb" in text:
        return "failed_download_duplicate_nzb"
    if "dognzb.cr/fail" in text or "url fetching failed" in text or "maximum retries" in text:
        return "sab_url_fetch_failed"
    if "sabnzbd.org/not-complete" in text or "cannot be completed" in text or "not complete" in text or "missing articles" in text:
        return "sab_not_complete"
    return default


def ensure_send_allowed(cache, series, issue_number, chosen):
    if is_known_bad_result(cache, series, issue_number, chosen):
        raise RuntimeError("selected result is a known failed/duplicate source")


def send(acquire, chosen, query, dry_run):
    title = chosen.get("title") or "unknown"
    media_type = "comics"
    url = chosen.get("downloadUrl")
    if not url:
        raise RuntimeError("selected result has no safe download URL")
    if dry_run:
        return {"dry_run": True}
    if chosen.get("protocol") == "torrent":
        outcome = acquire.qbit_add(url, title, media_type, dry_run=False)
    elif chosen.get("protocol") == "usenet":
        outcome = acquire.sab_add(url, title, dry_run=False)
    else:
        raise RuntimeError(f"unsupported protocol: {chosen.get('protocol')}")
    acquire.record_pending_import(query, media_type, chosen, outcome if isinstance(outcome, dict) else {})
    return outcome


def pack_auto_approve_reason(item, quality_rules=None):
    quality_rules = quality_rules or quality_language_rules()
    decision = item.get("pack_decision") or {}
    pack_match = item.get("pack_match") or {}
    candidate = item.get("candidate") or {}
    english = item.get("english_confidence") or {}
    issue = issue_int(item.get("issue"))
    content_evidence = pack_match.get("coverage_source") == "pack_contents_filename"
    if not quality_rules.get("packs_allowed", True):
        return "packs_disabled_by_quality_rules"
    if issue is None or issue <= 0:
        return "special_or_issue_zero_requires_review"
    if not content_evidence and not series_title_starts_release(item.get("series"), candidate.get("title")):
        return "series_title_prefix_mismatch"
    if not content_evidence and related_subseries_title_blocker(
        item.get("series"),
        candidate.get("title"),
        issue_title=item.get("issue_title"),
        issue_number=item.get("issue"),
        publisher=item.get("publisher"),
    ):
        return "related_subseries_title_mismatch"
    if not pack_covers_trigger_issue(item):
        return "pack_does_not_cover_trigger_issue"
    quality_blocker = quality_rule_block_reason(candidate, quality_rules, english=english)
    if quality_blocker:
        return quality_blocker
    bad_history = known_bad_pack_archive_history(item)
    if bad_history:
        item["bad_archive_history"] = bad_history
        return "known_bad_pack_archive_history"
    useful_missing_count = int(pack_match.get("useful_missing_count") or 0)
    min_missing = int(quality_rules.get("pack_auto_approve_min_missing") or 1)
    complete_min_missing = int(quality_rules.get("complete_pack_min_missing") or min_missing)
    if useful_missing_count <= 0:
        return "no_missing_items"
    if useful_missing_count < min_missing:
        return "pack_too_few_missing_items"
    if pack_match.get("coverage_source") in {"collected_edition_unknown", "collected_edition_story_mismatch"}:
        return "collected_edition_not_trigger_match"
    if int(pack_match.get("unknown_unmatched_count") or 0) != 0:
        return "unknown_pack_contents"
    if pack_match.get("coverage_source") == "complete_keyword" and useful_missing_count < complete_min_missing:
        return "complete_pack_too_small_for_auto_approval"
    if english.get("status") not in {"confirmed_english", "likely_english"}:
        return "english_not_confirmed"
    if not candidate.get("downloadUrl"):
        return "no_safe_download_url"
    protocol = str(candidate.get("protocol") or "").lower()
    if protocol not in {"torrent", "usenet"}:
        return "unsupported_protocol"
    if protocol == "torrent" and int(candidate.get("seeders") or 0) < 1:
        return "torrent_has_no_seeders"
    if not decision.get("safe_to_auto_approve"):
        return "pack_requires_review"
    return None


def pack_auto_approval_needs_review(reason):
    return str(reason or "") in PACK_REVIEWABLE_BLOCK_REASONS


def pack_auto_approve_sort_key(item):
    decision = item.get("pack_decision") or {}
    pack_match = item.get("pack_match") or {}
    candidate = item.get("candidate") or {}
    english = item.get("english_confidence") or {}
    candidate_protocol_rank = protocol_rank(candidate.get("protocol"))
    return (
        candidate_protocol_rank,
        -int(decision.get("score") or 0),
        -int(pack_match.get("useful_missing_count") or 0),
        int(pack_match.get("already_present_count") or 0),
        -int(candidate.get("seeders") or 0),
        -int(english.get("score") or 0),
        normalize((candidate or {}).get("title")),
    )


def choose_pack_candidate_for_automation(pack_candidates, quality_rules=None):
    ordered = sorted(unique_pack_candidates(pack_candidates), key=pack_candidate_sort_key)
    auto_approvable = [item for item in ordered if pack_auto_approve_reason(item, quality_rules=quality_rules) is None]
    if auto_approvable:
        return sorted(auto_approvable, key=pack_auto_approve_sort_key)[0], ordered
    actions = load_manual_review_actions()
    unhandled = []
    for item in ordered:
        review_record = {"reason": "pack_candidate_requires_review", **item}
        review_id = review_id_for(review_record)
        if not pack_is_handled(actions, review_id, item, item.get("candidate") or {}):
            unhandled.append(item)
    candidate_pool = unhandled or ordered
    non_reviewable_blocked = []
    for item in ordered:
        reason = pack_auto_approve_reason(item, quality_rules=quality_rules)
        if reason and not pack_auto_approval_needs_review(reason):
            non_reviewable_blocked.append(item)
    if non_reviewable_blocked:
        return sorted(non_reviewable_blocked, key=pack_auto_approve_sort_key)[0], ordered
    reviewable = [
        item for item in candidate_pool
        if pack_auto_approval_needs_review(pack_auto_approve_reason(item, quality_rules=quality_rules))
    ]
    if reviewable:
        return sorted(reviewable, key=pack_auto_approve_sort_key)[0], ordered
    return (candidate_pool[0] if candidate_pool else None), ordered


def auto_approve_pack_candidate(acquire, item, cache, dry_run, quality_rules=None):
    reason = pack_auto_approve_reason(item, quality_rules=quality_rules)
    if reason:
        return {"status": "not_auto_approved", "reason": reason}
    review_record = {"reason": "pack_candidate_requires_review", **item}
    review_id = review_id_for(review_record)
    candidate = item.get("candidate") or {}
    actions = load_manual_review_actions()
    if pack_is_handled(actions, review_id, item, candidate):
        return {
            "status": "pack_already_handled",
            "review_id": review_id,
            "title": candidate.get("title") or item.get("query") or "pack-review",
            "pack_handled_key": pack_handled_key_for_item(item, candidate),
        }
    state = load_pack_state()
    active = state.get("active")
    if active and active.get("review_id") != review_id and active_pack_blocks_new(review_id):
        return {"status": "blocked_active_pack", "active": active}
    ensure_send_allowed(cache, item.get("series"), item.get("issue"), candidate)
    title = candidate.get("title") or item.get("query") or "pack-review"
    if dry_run:
        return {"status": "dry_run", "review_id": review_id, "title": title}
    review("pack_candidate_requires_review", item)
    protocol = str(candidate.get("protocol") or "").lower()
    if protocol == "torrent":
        outcome = acquire.qbit_add(candidate["downloadUrl"], title, "comics", dry_run=False)
    elif protocol == "usenet":
        outcome = acquire.sab_add(candidate["downloadUrl"], title, dry_run=False)
    else:
        raise RuntimeError(f"unsupported protocol: {candidate.get('protocol')}")
    record = {
        "event": "pending_pack_import",
        "created_at": time.time(),
        "review_id": review_id,
        "status": "sent",
        "auto_approved": True,
        "series": item.get("series"),
        "issue": item.get("issue"),
        "volume_id": item.get("volume_id"),
        "query": item.get("query") or title,
        "title": title,
        "candidate": {
            key: candidate.get(key)
            for key in ("title", "indexer", "indexerId", "protocol", "seeders", "size")
            if candidate.get(key) is not None
        },
        "pack_info": item.get("pack_info"),
        "pack_match": item.get("pack_match"),
        "outcome": outcome,
    }
    append_pending_pack(record)
    active_record = {
        "review_id": review_id,
        "status": "auto_approved",
        "title": title,
        "series": item.get("series"),
        "volume_id": item.get("volume_id"),
        "approved_at": time.time(),
    }
    existing_active = state.get("active")
    replaced_nonblocking_active = bool(existing_active and existing_active.get("review_id") != review_id)
    state["active"] = active_record
    history_event = "auto_approved_replaced_nonblocking_active" if replaced_nonblocking_active else "auto_approved"
    history_record = {**active_record, "event": history_event}
    if replaced_nonblocking_active:
        history_record["previous_active_review_id"] = existing_active.get("review_id")
        history_record["previous_active_title"] = existing_active.get("title")
        history_record["previous_active_status"] = existing_active.get("status")
    state.setdefault("history", []).append(history_record)
    save_pack_state(state)
    if review_id not in actions.setdefault("pack_approved", []):
        actions["pack_approved"].append(review_id)
    handled_key = mark_pack_identity_in_flight(actions, review_id, item, candidate)
    write_json_file(MANUAL_REVIEW_ACTIONS_FILE, actions)
    return {
        "status": "pack_auto_approved",
        "review_id": review_id,
        "title": title,
        "outcome": outcome,
        "queued_behind_active": False,
        "replaced_nonblocking_active": replaced_nonblocking_active,
        "pack_handled_key": handled_key,
    }


def has_active_reconciled_download(title, issue_number):
    if not COMPLETION_DB.exists():
        return False
    variants = {normalize(value) for value in query_variants(title, issue_number)}
    con = sqlite3.connect(COMPLETION_DB)
    con.row_factory = sqlite3.Row
    try:
        table = con.execute(
            "select name from sqlite_master where type='table' and name='download_reconciliation'"
        ).fetchone()
        if not table:
            return False
        rows = con.execute(
            """
            select title, query, lifecycle_state
            from download_reconciliation
            where lifecycle_state in ('queued', 'downloading', 'stalled_downloading', 'completed_in_client', 'ready_to_import', 'importing', 'waiting_for_kavita_scan')
            """
        ).fetchall()
        for row in rows:
            if normalize(row["query"]) in variants:
                return True
            if acceptable_result(title, issue_number, {"title": row["title"] or ""}):
                return True
        return False
    finally:
        con.close()


def retry_failed_downloads(args):
    run_started = time.monotonic()
    search_budget_seconds = max(0.0, float(args.search_budget_seconds or 0))
    search_deadline = search_deadline_from_budget(search_budget_seconds, started_at=run_started)
    series_names = tuple(args.series) if args.series else monitored_series_names()
    rows = missing_issues(series_names, fresh_days=args.fresh_days)
    source_summary = missing_source_summary(rows)
    rows, suppressed = suppress_completed_reading(rows)
    rows = order_missing_rows_for_acquisition(rows)
    seen_items = set()
    sent_pack_identities = set()
    series_pack_result_cache = {}
    pack_detail_fetch_state = {"count": 0}
    summary = {
        "dry_run": args.dry_run,
        "mode": "retry_failed",
        "failure_records_seen": 0,
        "suppressed_completed": len(suppressed),
        "actions": [],
        "review": [],
        "skipped": [],
        "search_budget_seconds": search_budget_seconds,
        "search_budget_exhausted": False,
        "budget_skipped_count": 0,
        "budget_skipped_samples": [],
        "prowlarr_provider": prowlarr_provider_runtime_summary(args),
        "quality_language_rules": quality_rule_summary(DEFAULT_QUALITY_LANGUAGE_RULES),
        "bad_source_candidate_sync": {"ok": True, "skipped": True, "reason": "not_started"},
    }
    summary.update(source_summary)
    if not rows:
        summary["skipped"].append({"reason": "no_missing_rows"})
        summary["startup_short_circuit"] = True
        summary["attempted_total"] = 0
        summary["elapsed_seconds"] = round(max(0.0, time.monotonic() - run_started), 3)
        return summary

    failures = reconciliation_failure_records(limit=args.retry_failed_limit)
    summary["failure_records_seen"] = len(failures)
    if not failures:
        summary["skipped"].append({"reason": "no_failure_records"})
        summary["startup_short_circuit"] = True
        summary["attempted_total"] = 0
        summary["elapsed_seconds"] = round(max(0.0, time.monotonic() - run_started), 3)
        return summary

    acquire = load_acquire()
    quality_rules = quality_language_rules(refresh=True)
    summary["quality_language_rules"] = quality_rule_summary(quality_rules)
    cache = load_cache()
    cache.setdefault("bad_results", {})
    bad_source_candidate_sync = sync_bad_source_candidates_from_history(args.dry_run)
    summary["bad_source_candidate_sync"] = bad_source_candidate_sync
    ingest_reconciliation_bad_results(
        cache,
        rows,
        record_failures=(not args.dry_run and search_deadline is None),
        search_deadline=search_deadline,
    )
    active = set()
    if int(args.max_total or 0) > 0 and rows and failures:
        active = qbit_incomplete_series(acquire, series_names)

    def search_budget_exhausted():
        return search_deadline is not None and time.monotonic() >= search_deadline

    def note_budget_skip(row, tried_queries=None):
        summary["search_budget_exhausted"] = True
        summary["budget_skipped_count"] += 1
        if len(summary["budget_skipped_samples"]) < 12:
            summary["budget_skipped_samples"].append({
                "series": row.get("title"),
                "issue": row.get("issue_number"),
                "tried_queries": list(tried_queries or []),
            })

    failure_lookup = missing_row_lookup(rows)
    unit_model_cache = {}
    attempted_total = 0
    for failure in failures:
        if attempted_total >= args.max_total:
            break
        row = match_failure_to_missing(failure, rows, lookup=failure_lookup)
        if not row:
            summary["skipped"].append({
                "reason": "failed_record_not_currently_missing",
                "title": failure.get("title"),
                "query": failure.get("query"),
                "state": failure.get("lifecycle_state"),
            })
            continue
        title = row["title"]
        issue = row["issue_number"]
        row_context = row_output_context(row)
        if search_budget_exhausted():
            note_budget_skip(row)
            break
        item_key = (normalize(title), normalized_number(issue))
        if item_key in seen_items:
            continue
        seen_items.add(item_key)
        if has_active_reconciled_download(title, issue):
            summary["skipped"].append({"reason": "already_active_or_ready", "series": title, "issue": issue, **row_context})
            continue
        alternate_count = alternate_attempt_count(title, issue)
        if alternate_attempted(title, issue, args.retry_failed_max_attempts):
            summary["skipped"].append({
                "reason": "alternate_attempts_exhausted",
                "series": title,
                "issue": issue,
                "attempts": alternate_count,
                "max_attempts": args.retry_failed_max_attempts,
                **row_context,
            })
            continue
        if not folder_is_safe(row.get("folder")) and not row_can_search_without_folder(row):
            item = {"series": title, "issue": issue, "folder": row.get("folder"), "failed_release": failure.get("title"), **row_context}
            summary["review"].append({"reason": "unsafe_or_missing_target_folder", **item})
            if not args.dry_run:
                review("unsafe_or_missing_target_folder", item)
                mark_alternate_attempt(title, issue, "blocked_unsafe_folder", "unsafe_or_missing_target_folder")
            continue
        attempted_total += 1
        is_manga = row_is_manga(row)
        unit_model = row_unit_model(row, unit_model_cache) if is_manga else None
        tried_queries = []
        sample_results = []
        pack_candidates = []
        chosen = None
        chosen_query = None
        budget_stopped = False
        if collected_edition_range_hint_for_row(row):
            pack_candidates.extend(
                collect_series_pack_candidates(
                    acquire,
                    row,
                    cache,
                    args,
                    "retry_failed_collected_edition_preflight",
                    is_manga=is_manga,
                    unit_model=unit_model,
                    quality_rules=quality_rules,
                    search_budget_exhausted=search_budget_exhausted,
                    search_deadline=search_deadline,
                    series_pack_result_cache=series_pack_result_cache,
                    pack_detail_fetch_state=pack_detail_fetch_state,
                    query_group_filter={"series_pack"},
                )
            )
        if should_try_weekly_pack_before_issue(row, is_manga=is_manga, unit_model=unit_model):
            pack_candidates.extend(
                collect_series_pack_candidates(
                    acquire,
                    row,
                    cache,
                    args,
                    "retry_failed_weekly_pack_preflight",
                    is_manga=is_manga,
                    unit_model=unit_model,
                    quality_rules=quality_rules,
                    search_budget_exhausted=search_budget_exhausted,
                    search_deadline=search_deadline,
                    series_pack_result_cache=series_pack_result_cache,
                    pack_detail_fetch_state=pack_detail_fetch_state,
                    query_group_filter={"weekly_pack"},
                )
            )
        if not pack_candidates:
            for query in limited_queries(query_variants_for_row(row, is_manga=is_manga, unit_model=unit_model), args):
                if search_budget_exhausted():
                    budget_stopped = True
                    note_budget_skip(row, tried_queries)
                    break
                tried_queries.append(query)
                try:
                    results = prowlarr_search_with_budget(
                        acquire,
                        query,
                        "comics",
                        args,
                        search_deadline=search_deadline,
                        limit=args.limit,
                    )
                except Exception as exc:
                    attempt_status, attempt_reason = prowlarr_exception_attempt(exc)
                    item = {
                        "series": title,
                        "issue": issue,
                        "query": query,
                        "failed_release": failure.get("title"),
                        "error": redact_error(exc),
                        **row_context,
                    }
                    item["retryable"] = attempt_status == "retry_scheduled"
                    if item["retryable"]:
                        summary["skipped"].append({"reason": attempt_reason, **item})
                        audit(attempt_reason, item)
                    else:
                        summary["review"].append({"reason": "alternate_search_error", **item})
                    record_inkdrop_queue_attempt(
                        row,
                        attempt_status,
                        attempt_reason if item["retryable"] else "alternate_search_error",
                        query=query,
                        dry_run=args.dry_run,
                        extra={"error": redact_error(exc), "failed_release": failure.get("title"), "retryable": item["retryable"]},
                    )
                    if not args.dry_run and not item["retryable"]:
                        review("alternate_search_error", item)
                        mark_alternate_attempt(title, issue, "alternate_search_error", redact_error(exc))
                    if item["retryable"] and search_budget_exhausted():
                        budget_stopped = True
                        note_budget_skip(row, tried_queries)
                    chosen = None
                    break
                results, quality_blocked_samples = filter_quality_allowed_results(
                    acquire,
                    row,
                    title,
                    issue,
                    results,
                    query=query,
                    is_manga=is_manga,
                    unit_model=unit_model,
                    quality_rules=quality_rules,
                    dry_run=args.dry_run,
                )
                sample_results.extend(sample for sample in quality_blocked_samples if sample not in sample_results)
                results, known_bad_samples = filter_known_bad_results(
                    cache,
                    row,
                    title,
                    issue,
                    results,
                    query=query,
                    dry_run=args.dry_run,
                )
                sample_results.extend(sample for sample in known_bad_samples if sample not in sample_results)
                if results:
                    for result in results[:5]:
                        sample_results.append(sample_result(
                            title, issue, result, is_manga=is_manga, unit_model=unit_model,
                            quality_rules=quality_rules, wanted_unit_type=row.get("unit_type"),
                        ))
                        add_pack_candidate(
                            pack_candidates,
                            acquire,
                            row,
                            query,
                            result,
                            "retry_failed_pack",
                            is_manga=is_manga,
                            unit_model=unit_model,
                            quality_rules=quality_rules,
                        )
                chosen = choose_acceptable(
                    title, issue, results, is_manga=is_manga, unit_model=unit_model,
                    quality_rules=quality_rules, wanted_unit_type=row.get("unit_type"),
                )
                if chosen:
                    chosen_query = query
                    break
        if budget_stopped and not pack_candidates:
            summary["skipped"].append({
                "reason": "search_budget_exhausted",
                "series": title,
                "issue": issue,
                "tried_queries": tried_queries,
                **row_context,
            })
            continue
        if not chosen:
            if not pack_candidates:
                pack_candidates.extend(
                    collect_series_pack_candidates(
                        acquire,
                        row,
                        cache,
                        args,
                        "retry_failed_series_pack",
                        is_manga=is_manga,
                        unit_model=unit_model,
                        quality_rules=quality_rules,
                        search_budget_exhausted=search_budget_exhausted,
                        search_deadline=search_deadline,
                        series_pack_result_cache=series_pack_result_cache,
                        pack_detail_fetch_state=pack_detail_fetch_state,
                    )
                )
            if search_budget_exhausted() and not pack_candidates:
                note_budget_skip(row, tried_queries)
                summary["skipped"].append({
                    "reason": "search_budget_exhausted",
                    "series": title,
                    "issue": issue,
                    "tried_queries": tried_queries,
                    **row_context,
                })
                continue
            if pack_candidates:
                pack_candidates = unique_pack_candidates(pack_candidates)
                item, pack_candidates = choose_pack_candidate_for_automation(pack_candidates, quality_rules=quality_rules)
                item, action_row, action_context, retarget = prepare_pack_candidate_for_action(item, row, row_context)
                action_title = action_row.get("title") or item.get("series") or title
                action_issue = action_row.get("issue_number") or item.get("issue") or issue
                if retarget:
                    audit("retry_failed_pack_manifest_retargeted", {
                        "trigger_series": title,
                        "trigger_issue": issue,
                        "target_series": action_title,
                        "target_issue": action_issue,
                        "candidate": (item.get("candidate") or {}).get("title"),
                        "matching_entry": retarget.get("matching_entry"),
                    })
                item["candidate_options"] = pack_candidates
                item["option_count"] = len(pack_candidates)
                item["tried_queries"] = tried_queries
                item["failed_release"] = failure.get("title")
                item["failed_reason"] = failure.get("reason")
                pack_identity = pack_release_identity(item)
                if pack_identity in sent_pack_identities:
                    summary["skipped"].append({
                        "reason": "pack_already_selected_this_run",
                        "series": action_title,
                        "issue": action_issue,
                        "candidate": (item.get("candidate") or {}).get("title"),
                        "manifest_retarget": item.get("manifest_retarget"),
                        **action_context,
                    })
                    continue
                try:
                    auto_result = auto_approve_pack_candidate(acquire, item, cache, args.dry_run, quality_rules=quality_rules)
                except Exception as exc:
                    candidate = item.get("candidate") or {}
                    failure_reason = send_failure_reason(exc, "alternate_pack_send_failed")
                    remember_bad_result(cache, action_title, action_issue, candidate, failure_reason)
                    record_source_failure(
                        action_title,
                        action_issue,
                        release_title(candidate),
                        failure_reason,
                        source=result_source(candidate),
                        protocol=result_protocol(candidate),
                        download_url_hash=result_download_url_hash(candidate),
                        query=item.get("query"),
                    )
                    item["error"] = redact_error(exc)
                    item["failure_reason"] = failure_reason
                    auto_result = {"status": "failed", "reason": failure_reason}
                    record_inkdrop_queue_attempt(
                        action_row,
                        "error",
                        failure_reason,
                        query=item.get("query"),
                        candidate=candidate,
                        dry_run=args.dry_run,
                        extra={"error": redact_error(exc), "pack": True, "manifest_retarget": item.get("manifest_retarget")},
                    )
                if auto_result.get("status") in {"pack_auto_approved", "dry_run"}:
                    action = {
                        "series": action_title,
                        "issue": action_issue,
                        "query": item.get("query"),
                        "failed_release": failure.get("title"),
                        "title": (item.get("candidate") or {}).get("title"),
                        "indexer": (item.get("candidate") or {}).get("indexer"),
                        "protocol": (item.get("candidate") or {}).get("protocol"),
                        "seeders": (item.get("candidate") or {}).get("seeders"),
                        "manga_unit_model": unit_model,
                        "pack_auto_approved": True,
                        "pack_review_id": auto_result.get("review_id"),
                        "pack_info": item.get("pack_info"),
                        "pack_match": item.get("pack_match"),
                        "pack_decision": item.get("pack_decision"),
                        "outcome": auto_result,
                        "manifest_retarget": item.get("manifest_retarget"),
                        **action_context,
                    }
                    summary["actions"].append(action)
                    sent_pack_identities.add(pack_identity)
                    record_inkdrop_queue_attempt(
                        action_row,
                        "sent" if not args.dry_run else "dry_run",
                        "alternate pack sent to downloader",
                        query=item.get("query"),
                        candidate=item.get("candidate"),
                        outcome=auto_result.get("outcome"),
                        dry_run=args.dry_run,
                        extra={"pack_review_id": auto_result.get("review_id"), "pack": True, "failed_release": failure.get("title"), "manifest_retarget": item.get("manifest_retarget")},
                    )
                    if not args.dry_run:
                        mark_alternate_attempt(action_title, action_issue, "sent_alternate_pack", (item.get("candidate") or {}).get("title"))
                    audit("retry_failed_pack_auto_approved", action)
                elif auto_result.get("status") == "pack_already_handled":
                    sent_pack_identities.add(pack_identity)
                    skipped = {
                        "reason": "pack_already_in_flight_or_finished",
                        "series": action_title,
                        "issue": action_issue,
                        "candidate": (item.get("candidate") or {}).get("title"),
                        "pack_review_id": auto_result.get("review_id"),
                        "pack_handled_key": auto_result.get("pack_handled_key"),
                        "manifest_retarget": item.get("manifest_retarget"),
                        **action_context,
                    }
                    summary["skipped"].append(skipped)
                    audit("retry_failed_pack_already_handled", skipped)
                elif auto_result.get("status") == "blocked_active_pack":
                    item["auto_approval_blocked_reason"] = "blocked_active_pack"
                    item["active_pack"] = auto_result.get("active")
                    skipped = {
                        "reason": "pack_waiting_for_active_pack",
                        "series": action_title,
                        "issue": action_issue,
                        "query": item.get("query"),
                        "candidate": (item.get("candidate") or {}).get("title"),
                        "active_pack": auto_result.get("active"),
                        "manifest_retarget": item.get("manifest_retarget"),
                        **action_context,
                    }
                    summary["skipped"].append(skipped)
                    audit("retry_failed_pack_waiting_active_pack", skipped)
                    if not args.dry_run:
                        mark_alternate_attempt(action_title, action_issue, "blocked_active_pack", "another pack is active")
                else:
                    blocked_reason = auto_result.get("reason") or "not_auto_approved"
                    item["auto_approval_blocked_reason"] = blocked_reason
                    if pack_auto_approval_needs_review(blocked_reason):
                        summary["review"].append({"reason": "pack_candidate_requires_review", **item})
                        if not args.dry_run:
                            review("pack_candidate_requires_review", item)
                    else:
                        skipped = {
                            "reason": "pack_candidate_not_actionable",
                            "auto_approval_blocked_reason": blocked_reason,
                            "series": action_title,
                            "issue": action_issue,
                            "query": item.get("query"),
                            "candidate": (item.get("candidate") or {}).get("title"),
                            "protocol": (item.get("candidate") or {}).get("protocol"),
                            "seeders": (item.get("candidate") or {}).get("seeders"),
                            "pack_decision": item.get("pack_decision"),
                            "bad_archive_history": item.get("bad_archive_history"),
                            "manifest_retarget": item.get("manifest_retarget"),
                            **action_context,
                        }
                        summary["skipped"].append(skipped)
                        audit("pack_candidate_not_actionable", skipped)
                        if not args.dry_run:
                            mark_alternate_attempt(action_title, action_issue, f"pack_candidate_not_actionable:{blocked_reason}", blocked_reason)
                continue
            item = {
                "series": title,
                "issue": issue,
                "tried_queries": tried_queries,
                "failed_release": failure.get("title"),
                "failed_reason": failure.get("reason"),
                "sample": sample_results[:8],
                "note": "Known bad source was avoided; no clean alternate was found.",
                **row_context,
            }
            summary["review"].append({"reason": "no_safe_alternate_found", **item})
            record_inkdrop_queue_attempt(
                row,
                "no_candidate_retry",
                "no_safe_alternate_found",
                query=(tried_queries[-1] if tried_queries else None),
                dry_run=args.dry_run,
                extra={"tried_queries": tried_queries, "failed_release": failure.get("title"), "sample": sample_results[:5]},
            )
            if not args.dry_run:
                review("no_safe_alternate_found", item)
                mark_alternate_attempt(title, issue, "no_safe_alternate_found", failure.get("reason"))
            continue
        try:
            ensure_send_allowed(cache, title, issue, chosen)
            outcome = send(acquire, chosen, chosen_query, args.dry_run)
        except Exception as exc:
            failure_reason = send_failure_reason(exc, "alternate_send_failed")
            remember_bad_result(cache, title, issue, chosen, failure_reason)
            record_source_failure(
                title,
                issue,
                release_title(chosen),
                failure_reason,
                source=result_source(chosen),
                protocol=result_protocol(chosen),
                download_url_hash=result_download_url_hash(chosen),
                query=chosen_query,
            )
            item = {
                "series": title,
                "issue": issue,
                "query": chosen_query,
                "failed_release": failure.get("title"),
                "candidate": candidate_payload(chosen),
                "error": redact_error(exc),
                "failure_reason": failure_reason,
                **row_context,
            }
            review_reason = failure_reason if failure_reason == "failed_download_duplicate_nzb" else "alternate_send_failed"
            summary["review"].append({"reason": review_reason, **item})
            record_inkdrop_queue_attempt(
                row,
                "error",
                failure_reason,
                query=chosen_query,
                candidate=chosen,
                dry_run=args.dry_run,
                extra={"error": redact_error(exc), "review_reason": review_reason, "failed_release": failure.get("title")},
            )
            if not args.dry_run:
                review(review_reason, item)
                mark_alternate_attempt(title, issue, review_reason, redact_error(exc))
            continue
        action = {
            "series": title,
            "issue": issue,
            "query": chosen_query,
            "failed_release": failure.get("title"),
            "title": chosen.get("title"),
            "indexer": chosen.get("indexer"),
            "protocol": chosen.get("protocol"),
            "seeders": chosen.get("seeders"),
            "manga_unit_model": unit_model,
            "source_unit": chosen.get("source_unit"),
            "volume_supersession": mixed_chapter_supersession_follow_up(unit_model, chosen.get("source_unit")),
            "outcome": outcome,
            **row_context,
        }
        summary["actions"].append(action)
        record_inkdrop_queue_attempt(
            row,
            "sent" if not args.dry_run else "dry_run",
            "alternate sent to downloader",
            query=chosen_query,
            candidate=chosen,
            outcome=outcome,
            dry_run=args.dry_run,
            extra={"failed_release": failure.get("title")},
        )
        if not args.dry_run:
            mark_alternate_attempt(title, issue, "sent_alternate", chosen.get("title"))
        audit("retry_failed_selected", action)
    save_cache(cache)
    summary["pack_detail_fetch_count"] = int(pack_detail_fetch_state.get("count") or 0)
    summary["attempted_total"] = attempted_total
    summary["elapsed_seconds"] = round(max(0.0, time.monotonic() - run_started), 3)
    return summary


def main():
    parser = argparse.ArgumentParser(description="Guarded missing comics/manga acquisition via Kapowarr + Prowlarr")
    parser.add_argument("--series", action="append", default=[])
    parser.add_argument("--max-per-series", type=int, default=5)
    parser.add_argument("--max-total", type=int, default=25)
    parser.add_argument("--fresh-days", type=float)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--max-queries-per-issue", type=int, default=None)
    parser.add_argument("--no-result-cooldown-hours", type=float, default=None)
    parser.add_argument("--prowlarr-timeout-seconds", type=float, default=None)
    parser.add_argument("--search-budget-seconds", type=float, default=None, help="Stop Prowlarr searching after this many seconds and return partial JSON")
    parser.add_argument("--pack-detail-fetch-max", type=int, default=PACK_DETAIL_MAX_FETCHES_PER_RUN, help="Maximum pack metadata detail fetches per worker run")
    parser.add_argument("--retry-failed", action="store_true", help="Try bounded alternates for reconciled failed comic/manga downloads")
    parser.add_argument("--retry-failed-limit", type=int, default=100)
    parser.add_argument("--retry-failed-max-attempts", type=int, default=1)
    parser.add_argument("--json", action="store_true", help="Accepted for worker consistency; output is JSON by default")
    args = parser.parse_args()
    run_started = time.monotonic()

    if int(args.max_total or 0) <= 0:
        print(json.dumps(
            {
                "dry_run": args.dry_run,
                "mode": "retry_failed" if args.retry_failed else "missing_acquire",
                "ok": True,
                "reason": "max_total_zero",
                "series_count": len(args.series or []),
                "missing_candidates": 0,
                "actions": [],
                "review": [],
                "skipped": [{"reason": "max_total_zero"}],
                "search_budget_seconds": max(0.0, float(args.search_budget_seconds or 0)),
                "startup_short_circuit": True,
                "elapsed_seconds": round(max(0.0, time.monotonic() - run_started), 3),
            },
            indent=2,
        ))
        return

    provider_settings = apply_prowlarr_missing_provider_defaults(args)
    if not args.prowlarr_provider_enabled:
        print(json.dumps(
            {
                "dry_run": args.dry_run,
                "mode": "retry_failed" if args.retry_failed else "missing_acquire",
                "ok": True,
                "reason": "prowlarr_provider_disabled",
                "series_count": len(args.series or []),
                "missing_candidates": 0,
                "actions": [],
                "review": [],
                "skipped": [{"reason": "prowlarr_provider_disabled"}],
                "prowlarr_provider": prowlarr_provider_runtime_summary(args, provider_settings),
                "startup_short_circuit": True,
                "elapsed_seconds": round(max(0.0, time.monotonic() - run_started), 3),
            },
            indent=2,
        ))
        return

    if args.retry_failed:
        print(json.dumps(retry_failed_downloads(args), indent=2))
        return

    series_names = tuple(args.series) if args.series else monitored_series_names()
    search_budget_seconds = max(0.0, float(args.search_budget_seconds or 0))
    search_deadline = search_deadline_from_budget(search_budget_seconds, started_at=run_started)
    rows = missing_issues(series_names, fresh_days=args.fresh_days)
    source_summary = missing_source_summary(rows)
    rows, suppressed = suppress_completed_reading(rows)
    rows = order_missing_rows_for_acquisition(rows)
    selected_series_mode = bool(args.series)
    if not rows:
        summary = {
            "dry_run": args.dry_run,
            "series_count": len(series_names),
            "missing_candidates": 0,
            "suppressed_completed": len(suppressed),
            "suppressed_manga_completed": len(suppressed),
            "suppressed_samples": [
                {"series": item.get("title"), "issue": item.get("issue_number"), "normalized_number": item.get("normalized_number")}
                for item in suppressed[:20]
            ],
            "fresh_days": args.fresh_days,
            "active_qbit_series": [],
            "send_limit_mode": "actual_actions" if selected_series_mode else "searched_items",
            "skipped_active_qbit": [],
            "actions": [],
            "review": [],
            "skipped": [{"reason": "no_missing_rows"}],
            "search_budget_seconds": search_budget_seconds,
            "search_budget_exhausted": False,
            "budget_skipped_count": 0,
            "budget_skipped_samples": [],
            "prowlarr_provider": prowlarr_provider_runtime_summary(args, provider_settings),
            "quality_language_rules": quality_rule_summary(DEFAULT_QUALITY_LANGUAGE_RULES),
            "bad_source_candidate_sync": {"ok": True, "skipped": True, "reason": "no_missing_rows"},
            "startup_short_circuit": True,
            "elapsed_seconds": round(max(0.0, time.monotonic() - run_started), 3),
        }
        summary.update(source_summary)
        print(json.dumps(summary, indent=2))
        return

    acquire = load_acquire()
    quality_rules = quality_language_rules(refresh=True)
    pending, raw_pending = pending_entries()
    cache = load_cache()
    no_result = cache.setdefault("no_result", {})
    cache.setdefault("bad_results", {})
    bad_source_candidate_sync = sync_bad_source_candidates_from_history(args.dry_run)
    cutoff = time.time() - (args.no_result_cooldown_hours * 3600)
    ingest_reconciliation_bad_results(
        cache,
        rows,
        record_failures=(not args.dry_run and search_deadline is None),
        search_deadline=search_deadline,
    )
    active = set()
    if int(args.max_total or 0) > 0 and rows:
        active = qbit_incomplete_series(acquire, series_names)
    sent_by_series = {name: 0 for name in series_names}
    attempted_by_series = {name: 0 for name in series_names}
    attempted_total = 0
    sent_pack_identities = set()
    series_pack_result_cache = {}
    pack_detail_fetch_state = {"count": 0}
    summary = {
        "dry_run": args.dry_run,
        "series_count": len(series_names),
        "missing_candidates": len(rows),
        "suppressed_completed": len(suppressed),
        "suppressed_manga_completed": len(suppressed),
        "suppressed_samples": [
            {"series": item.get("title"), "issue": item.get("issue_number"), "normalized_number": item.get("normalized_number")}
            for item in suppressed[:20]
        ],
        "fresh_days": args.fresh_days,
        "active_qbit_series": sorted(active),
        "send_limit_mode": "actual_actions" if selected_series_mode else "searched_items",
        "skipped_active_qbit": [],
        "actions": [],
        "review": [],
        "skipped": [],
        "search_budget_seconds": search_budget_seconds,
        "search_budget_exhausted": False,
        "budget_skipped_count": 0,
        "budget_skipped_samples": [],
        "prowlarr_provider": prowlarr_provider_runtime_summary(args, provider_settings),
        "quality_language_rules": quality_rule_summary(quality_rules),
        "bad_source_candidate_sync": bad_source_candidate_sync,
    }
    summary.update(source_summary)

    def search_budget_exhausted():
        return search_deadline is not None and time.monotonic() >= search_deadline

    def note_budget_skip(row, tried_queries=None):
        summary["search_budget_exhausted"] = True
        summary["budget_skipped_count"] += 1
        if len(summary["budget_skipped_samples"]) < 12:
            summary["budget_skipped_samples"].append({
                "series": row.get("title"),
                "issue": row.get("issue_number"),
                "tried_queries": list(tried_queries or []),
            })

    unit_model_cache = {}
    for row in rows:
        if selected_series_mode:
            if len(summary["actions"]) >= args.max_total:
                break
        elif attempted_total >= args.max_total:
            break
        if search_budget_exhausted():
            note_budget_skip(row)
            break
        title = row["title"]
        is_manga = row_is_manga(row)
        unit_model = row_unit_model(row, unit_model_cache) if is_manga else None
        row_context = row_output_context(row)
        if has_active_reconciled_download(title, row["issue_number"]):
            summary["skipped"].append({
                "reason": "already_active_or_ready",
                "series": title,
                "issue": row["issue_number"],
                **row_context,
            })
            continue
        if not folder_is_safe(row.get("folder")) and not row_can_search_without_folder(row):
            item = {"series": title, "issue": row["issue_number"], "folder": row.get("folder"), **row_context}
            summary["review"].append({"reason": "unsafe_or_missing_target_folder", **item})
            if not args.dry_run:
                review("unsafe_or_missing_target_folder", item)
            continue
        if attempted_by_series.get(title, 0) >= args.max_per_series:
            continue
        if issue_has_pending(title, row["issue_number"], raw_pending):
            continue
        attempted_by_series[title] = attempted_by_series.get(title, 0) + 1
        attempted_total += 1
        tried_queries = []
        sample_results = []
        pack_candidates = []
        found_any_results = False
        sent_issue = False
        budget_stopped = False
        if collected_edition_range_hint_for_row(row):
            pack_candidates.extend(
                collect_series_pack_candidates(
                    acquire,
                    row,
                    cache,
                    args,
                    "missing_acquire_collected_edition_preflight",
                    is_manga=is_manga,
                    unit_model=unit_model,
                    quality_rules=quality_rules,
                    search_budget_exhausted=search_budget_exhausted,
                    search_deadline=search_deadline,
                    series_pack_result_cache=series_pack_result_cache,
                    pack_detail_fetch_state=pack_detail_fetch_state,
                    query_group_filter={"series_pack"},
                )
            )
        if should_try_weekly_pack_before_issue(row, is_manga=is_manga, unit_model=unit_model):
            pack_candidates.extend(
                collect_series_pack_candidates(
                    acquire,
                    row,
                    cache,
                    args,
                    "missing_acquire_weekly_pack_preflight",
                    is_manga=is_manga,
                    unit_model=unit_model,
                    quality_rules=quality_rules,
                    search_budget_exhausted=search_budget_exhausted,
                    search_deadline=search_deadline,
                    series_pack_result_cache=series_pack_result_cache,
                    pack_detail_fetch_state=pack_detail_fetch_state,
                    query_group_filter={"weekly_pack"},
                )
            )
        if not pack_candidates:
            for query in limited_queries(query_variants_for_row(row, is_manga=is_manga, unit_model=unit_model), args):
                if search_budget_exhausted():
                    budget_stopped = True
                    note_budget_skip(row, tried_queries)
                    break
                if normalize(query) in pending:
                    continue
                tried_queries.append(query)
                cache_key = f"{title}|{row['issue_number']}|{query}"
                if no_result.get(cache_key, 0) >= cutoff:
                    continue
                try:
                    results = prowlarr_search_with_budget(
                        acquire,
                        query,
                        "comics",
                        args,
                        search_deadline=search_deadline,
                        limit=args.limit,
                    )
                except Exception as exc:
                    attempt_status, attempt_reason = prowlarr_exception_attempt(exc)
                    item = {
                        "series": title,
                        "issue": row["issue_number"],
                        "query": query,
                        "tried_queries": tried_queries,
                        "source_strategy": "manga" if is_manga else "comic",
                        "error": redact_error(exc),
                        **row_context,
                    }
                    item["retryable"] = attempt_status == "retry_scheduled"
                    if item["retryable"]:
                        summary["skipped"].append({"reason": attempt_reason, **item})
                        audit(attempt_reason, item)
                    else:
                        summary["review"].append({"reason": "prowlarr_search_error", **item})
                        audit("prowlarr_search_error", item)
                    record_inkdrop_queue_attempt(
                        row,
                        attempt_status,
                        attempt_reason,
                        query=query,
                        dry_run=args.dry_run,
                        extra={"error": redact_error(exc), "tried_queries": tried_queries, "retryable": item["retryable"]},
                    )
                    if not args.dry_run and not item["retryable"]:
                        review("prowlarr_search_error", item)
                    if item["retryable"] and search_budget_exhausted():
                        budget_stopped = True
                        note_budget_skip(row, tried_queries)
                    break
                results, quality_blocked_samples = filter_quality_allowed_results(
                    acquire,
                    row,
                    title,
                    row["issue_number"],
                    results,
                    query=query,
                    is_manga=is_manga,
                    unit_model=unit_model,
                    quality_rules=quality_rules,
                    dry_run=args.dry_run,
                )
                sample_results.extend(sample for sample in quality_blocked_samples if sample not in sample_results)
                results, known_bad_samples = filter_known_bad_results(
                    cache,
                    row,
                    title,
                    row["issue_number"],
                    results,
                    query=query,
                    dry_run=args.dry_run,
                )
                sample_results.extend(sample for sample in known_bad_samples if sample not in sample_results)
                chosen = choose_acceptable(
                    title, row["issue_number"], results, is_manga=is_manga, unit_model=unit_model,
                    quality_rules=quality_rules, wanted_unit_type=row.get("unit_type"),
                )
                audit("search", {"series": title, "issue": row["issue_number"], "query": query, "results": len(results), "matched": bool(chosen)})
                if not chosen:
                    no_result[cache_key] = time.time()
                    if results:
                        found_any_results = True
                        for result in results[:5]:
                            sample = sample_result(
                                title, row["issue_number"], result, is_manga=is_manga, unit_model=unit_model,
                                quality_rules=quality_rules, wanted_unit_type=row.get("unit_type"),
                            )
                            if sample not in sample_results:
                                sample_results.append(sample)
                            add_pack_candidate(
                                pack_candidates,
                                acquire,
                                row,
                                query,
                                result,
                                "missing_acquire_pack",
                                is_manga=is_manga,
                                unit_model=unit_model,
                                quality_rules=quality_rules,
                            )
                    continue
                if normalize(chosen.get("title")) in pending:
                    continue
                try:
                    ensure_send_allowed(cache, title, row["issue_number"], chosen)
                    outcome = send(acquire, chosen, query, args.dry_run)
                except Exception as exc:
                    failure_reason = send_failure_reason(exc, "download_client_send_failed")
                    remember_bad_result(cache, title, row["issue_number"], chosen, failure_reason)
                    record_source_failure(
                        title,
                        row["issue_number"],
                        release_title(chosen),
                        failure_reason,
                        source=result_source(chosen),
                        protocol=result_protocol(chosen),
                        download_url_hash=result_download_url_hash(chosen),
                        query=query,
                    )
                    item = {
                        "series": title,
                        "issue": row["issue_number"],
                        "query": query,
                        "tried_queries": tried_queries,
                        "source_strategy": "manga" if is_manga else "comic",
                        "manga_unit_model": unit_model,
                        "candidate": candidate_payload(chosen),
                        "error": redact_error(exc),
                        "failure_reason": failure_reason,
                        "note": "Downloader rejected or failed to accept the selected result; no pending import was recorded.",
                        **row_context,
                    }
                    review_reason = failure_reason if failure_reason == "failed_download_duplicate_nzb" else "download_client_send_failed"
                    summary["review"].append({"reason": review_reason, **item})
                    audit(review_reason, item)
                    record_inkdrop_queue_attempt(
                        row,
                        "error",
                        failure_reason,
                        query=query,
                        candidate=chosen,
                        dry_run=args.dry_run,
                        extra={"error": redact_error(exc), "review_reason": review_reason},
                    )
                    if not args.dry_run:
                        review(review_reason, item)
                    sent_issue = True
                    break
                action = {
                    "series": title,
                    "issue": row["issue_number"],
                    "query": query,
                    "title": chosen.get("title"),
                    "indexer": chosen.get("indexer"),
                    "protocol": chosen.get("protocol"),
                    "seeders": chosen.get("seeders"),
                    "manga_unit_model": unit_model,
                    "source_unit": chosen.get("source_unit"),
                    "volume_supersession": mixed_chapter_supersession_follow_up(unit_model, chosen.get("source_unit")),
                    "outcome": outcome,
                    **row_context,
                }
                summary["actions"].append(action)
                sent_by_series[title] = sent_by_series.get(title, 0) + 1
                audit("selected", action)
                record_inkdrop_queue_attempt(
                    row,
                    "sent" if not args.dry_run else "dry_run",
                    "sent to downloader",
                    query=query,
                    candidate=chosen,
                    outcome=outcome,
                    dry_run=args.dry_run,
                )
                sent_issue = True
                break
        if budget_stopped and not pack_candidates:
            summary["skipped"].append({
                "reason": "search_budget_exhausted",
                "series": title,
                "issue": row["issue_number"],
                "tried_queries": tried_queries,
                **row_context,
            })
            continue
        if not sent_issue:
            if not pack_candidates:
                pack_candidates.extend(
                    collect_series_pack_candidates(
                        acquire,
                        row,
                        cache,
                        args,
                        "missing_acquire_series_pack",
                        is_manga=is_manga,
                        unit_model=unit_model,
                        quality_rules=quality_rules,
                        search_budget_exhausted=search_budget_exhausted,
                        search_deadline=search_deadline,
                        series_pack_result_cache=series_pack_result_cache,
                        pack_detail_fetch_state=pack_detail_fetch_state,
                    )
                )
            if search_budget_exhausted() and not pack_candidates:
                note_budget_skip(row, tried_queries)
                summary["skipped"].append({
                    "reason": "search_budget_exhausted",
                    "series": title,
                    "issue": row["issue_number"],
                    "tried_queries": tried_queries,
                    **row_context,
                })
                continue
            if pack_candidates:
                pack_candidates = unique_pack_candidates(pack_candidates)
                item, pack_candidates = choose_pack_candidate_for_automation(pack_candidates, quality_rules=quality_rules)
                item, action_row, action_context, retarget = prepare_pack_candidate_for_action(item, row, row_context)
                action_title = action_row.get("title") or item.get("series") or title
                action_issue = action_row.get("issue_number") or item.get("issue") or row["issue_number"]
                if retarget:
                    audit("pack_manifest_retargeted", {
                        "trigger_series": title,
                        "trigger_issue": row["issue_number"],
                        "target_series": action_title,
                        "target_issue": action_issue,
                        "candidate": (item.get("candidate") or {}).get("title"),
                        "matching_entry": retarget.get("matching_entry"),
                    })
                item["candidate_options"] = pack_candidates
                item["option_count"] = len(pack_candidates)
                pack_identity = pack_release_identity(item)
                if pack_identity in sent_pack_identities:
                    summary["skipped"].append({
                        "reason": "pack_already_selected_this_run",
                        "series": action_title,
                        "issue": action_issue,
                        "candidate": (item.get("candidate") or {}).get("title"),
                        "manifest_retarget": item.get("manifest_retarget"),
                        **action_context,
                    })
                    sent_issue = True
                    continue
                try:
                    auto_result = auto_approve_pack_candidate(acquire, item, cache, args.dry_run, quality_rules=quality_rules)
                except Exception as exc:
                    candidate = item.get("candidate") or {}
                    failure_reason = send_failure_reason(exc, "download_client_send_failed")
                    remember_bad_result(cache, action_title, action_issue, candidate, failure_reason)
                    record_source_failure(
                        action_title,
                        action_issue,
                        release_title(candidate),
                        failure_reason,
                        source=result_source(candidate),
                        protocol=result_protocol(candidate),
                        download_url_hash=result_download_url_hash(candidate),
                        query=item.get("query"),
                    )
                    item["error"] = redact_error(exc)
                    item["failure_reason"] = failure_reason
                    auto_result = {"status": "failed", "reason": failure_reason}
                    record_inkdrop_queue_attempt(
                        action_row,
                        "error",
                        failure_reason,
                        query=item.get("query"),
                        candidate=candidate,
                        dry_run=args.dry_run,
                        extra={"error": redact_error(exc), "pack": True, "manifest_retarget": item.get("manifest_retarget")},
                    )
                if auto_result.get("status") in {"pack_auto_approved", "dry_run"}:
                    action = {
                        "series": action_title,
                        "issue": action_issue,
                        "query": item.get("query"),
                        "title": (item.get("candidate") or {}).get("title"),
                        "indexer": (item.get("candidate") or {}).get("indexer"),
                        "protocol": (item.get("candidate") or {}).get("protocol"),
                        "seeders": (item.get("candidate") or {}).get("seeders"),
                        "manga_unit_model": unit_model,
                        "pack_auto_approved": True,
                        "pack_review_id": auto_result.get("review_id"),
                        "pack_info": item.get("pack_info"),
                        "pack_match": item.get("pack_match"),
                        "pack_decision": item.get("pack_decision"),
                        "outcome": auto_result,
                        "manifest_retarget": item.get("manifest_retarget"),
                        **action_context,
                    }
                    summary["actions"].append(action)
                    sent_pack_identities.add(pack_identity)
                    sent_by_series[action_title] = sent_by_series.get(action_title, 0) + 1
                    audit("pack_auto_approved", action)
                    record_inkdrop_queue_attempt(
                        action_row,
                        "sent" if not args.dry_run else "dry_run",
                        "pack sent to downloader",
                        query=item.get("query"),
                        candidate=item.get("candidate"),
                        outcome=auto_result.get("outcome"),
                        dry_run=args.dry_run,
                        extra={"pack_review_id": auto_result.get("review_id"), "pack": True, "manifest_retarget": item.get("manifest_retarget")},
                    )
                elif auto_result.get("status") == "pack_already_handled":
                    sent_pack_identities.add(pack_identity)
                    skipped = {
                        "reason": "pack_already_in_flight_or_finished",
                        "series": action_title,
                        "issue": action_issue,
                        "candidate": (item.get("candidate") or {}).get("title"),
                        "pack_review_id": auto_result.get("review_id"),
                        "pack_handled_key": auto_result.get("pack_handled_key"),
                        "manifest_retarget": item.get("manifest_retarget"),
                        **action_context,
                    }
                    summary["skipped"].append(skipped)
                    audit("pack_already_handled", skipped)
                elif auto_result.get("status") == "blocked_active_pack":
                    audit(
                        "pack_auto_waiting_active_pack",
                        {
                            "series": action_title,
                            "issue": action_issue,
                            "query": item.get("query"),
                            "candidate": (item.get("candidate") or {}).get("title"),
                            "active": auto_result.get("active"),
                            "manifest_retarget": item.get("manifest_retarget"),
                            **action_context,
                        },
                    )
                else:
                    blocked_reason = auto_result.get("reason") or "not_auto_approved"
                    item["auto_approval_blocked_reason"] = blocked_reason
                    if pack_auto_approval_needs_review(blocked_reason):
                        summary["review"].append({"reason": "pack_candidate_requires_review", **item})
                        if not args.dry_run:
                            review("pack_candidate_requires_review", item)
                    else:
                        skipped = {
                            "reason": "pack_candidate_not_actionable",
                            "auto_approval_blocked_reason": blocked_reason,
                            "series": action_title,
                            "issue": action_issue,
                            "query": item.get("query"),
                            "candidate": (item.get("candidate") or {}).get("title"),
                            "protocol": (item.get("candidate") or {}).get("protocol"),
                            "seeders": (item.get("candidate") or {}).get("seeders"),
                            "pack_decision": item.get("pack_decision"),
                            "bad_archive_history": item.get("bad_archive_history"),
                            "manifest_retarget": item.get("manifest_retarget"),
                            **action_context,
                        }
                        summary["skipped"].append(skipped)
                        audit("pack_candidate_not_actionable", skipped)
            elif found_any_results:
                item = {
                    "series": title,
                    "issue": row["issue_number"],
                    "query": tried_queries[-1],
                    "tried_queries": tried_queries,
                    "result_count": len(sample_results),
                    "source_strategy": "manga" if is_manga else "comic",
                    "manga_unit_model": unit_model,
                    "sample": sample_results[:8],
                    **row_context,
                }
                summary["review"].append({"reason": "ambiguous_results", **item})
                record_inkdrop_queue_attempt(
                    row,
                    "review",
                    "ambiguous_results",
                    query=tried_queries[-1],
                    dry_run=args.dry_run,
                    extra={"result_count": len(sample_results), "sample": sample_results[:5]},
                )
                if not args.dry_run:
                    review("ambiguous_results", item)
            elif edition_like(title):
                item = {
                    "series": title,
                    "issue": row["issue_number"],
                    "tried_queries": tried_queries,
                    "note": "Edition-style title found no exact result; may need omnibus/part mapping.",
                    **row_context,
                }
                summary["review"].append({"reason": "no_exact_result", **item})
                record_inkdrop_queue_attempt(
                    row,
                    "no_candidate_retry",
                    "no_exact_result",
                    query=(tried_queries[-1] if tried_queries else None),
                    dry_run=args.dry_run,
                    extra={"tried_queries": tried_queries},
                )
                if not args.dry_run:
                    review("no_exact_result", item)
            elif is_manga:
                item = {
                    "series": title,
                    "issue": row["issue_number"],
                    "tried_queries": tried_queries,
                    "source_strategy": "manga",
                    "manga_unit_model": unit_model,
                    "note": "Manga-style query variants found no safe exact result in this source pass; the watched queue will keep trying other sources and scheduled retries.",
                    **row_context,
                }
                summary["review"].append({"reason": "manga_no_safe_result", **item})
                record_inkdrop_queue_attempt(
                    row,
                    "no_candidate_retry",
                    "manga_no_safe_result",
                    query=(tried_queries[-1] if tried_queries else None),
                    dry_run=args.dry_run,
                    extra={"tried_queries": tried_queries, "manga_unit_model": unit_model},
                )
                if not args.dry_run:
                    review("manga_no_safe_result", item)
            else:
                item = {
                    "series": title,
                    "issue": row["issue_number"],
                    "tried_queries": tried_queries,
                    "source_strategy": "comic",
                    "note": "No safe exact comic result was found in this source pass; the watched queue will keep trying other sources and scheduled retries.",
                    **row_context,
                }
                summary["review"].append({"reason": "no_safe_source", **item})
                record_inkdrop_queue_attempt(
                    row,
                    "no_candidate_retry",
                    "no_safe_source",
                    query=(tried_queries[-1] if tried_queries else None),
                    dry_run=args.dry_run,
                    extra={"tried_queries": tried_queries},
                )
                if not args.dry_run:
                    review("no_safe_source", item)

    save_cache(cache)
    summary["pack_detail_fetch_count"] = int(pack_detail_fetch_state.get("count") or 0)
    summary["attempted_total"] = attempted_total
    summary["attempted_by_series"] = {k: v for k, v in attempted_by_series.items() if v}
    summary["elapsed_seconds"] = round(max(0.0, time.monotonic() - run_started), 3)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
