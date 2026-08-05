#!/usr/bin/env python3
"""Respectful RSS discovery for InkDrop.

The RSS feed is used as a light signal that something may have appeared.
InkDrop-owned wanted rows are the source of truth; Prowlarr remains the guarded
acquisition path. No GetComics post pages are scraped.
"""

import argparse
import email.utils
import hashlib
import importlib.util
import inkdrop_state
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

import inkdrop_runtime_config

try:
    import inkdrop_language
except Exception:
    inkdrop_language = None


STATE_DIR = inkdrop_runtime_config.state_dir()
LOG_DIR = inkdrop_runtime_config.log_dir()
INKDROP_STATE_DB = STATE_DIR / inkdrop_state.STATE_DB_NAME
ACQUIRE_PATH = Path(os.environ.get("INKDROP_ACQUIRE_SCRIPT") or Path(__file__).resolve().with_name("inkdrop_acquire.py"))
MISSING_PATH = Path(os.environ.get("INKDROP_MISSING_ACQUIRE_SCRIPT") or Path(__file__).resolve().with_name("inkdrop_missing_acquire.py"))
STATUS_FILE = STATE_DIR / "rss-discovery-status.json"
LOG_FILE = LOG_DIR / "rss-discovery.log"
REVIEW_FILE = STATE_DIR / "manual-review.jsonl"
ACTIONS_FILE = STATE_DIR / "manual-review-actions.json"
ALIASES_FILE = STATE_DIR / "rss-aliases.json"
BAD_MATCHES_FILE = STATE_DIR / "rss-bad-matches.json"
CACHE_FILE = STATE_DIR / "rss-discovery-cache.json"

FEED_URL = "https://getcomics.org/feed/"
USER_AGENT = "InkDrop-RSS-Discovery/1.0 (+respectful periodic RSS poll)"
DEFAULT_LIMIT = 12
DEFAULT_MAX_AUTO = 5
DEFAULT_MAX_PER_SERIES = 1
NO_RESULT_COOLDOWN_HOURS = 12
BAD_RESULT_COOLDOWN_HOURS = 24
MAX_BACKOFF_SECONDS = 6 * 3600
RSS_PROVIDER_ENABLED = True
QUEUE_MODE_REVIEW_REASONS = {
    "rss_unsafe_target_folder",
}
MANUAL_REVIEW_PERSIST_ENV = "INKDROP_PERSIST_SOFT_REVIEWS"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_acquire():
    return load_module("inkdrop_acquire", ACQUIRE_PATH)


def load_missing():
    return load_module("inkdrop_missing_acquire", MISSING_PATH)


def normalize(text):
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def slug_key(*parts):
    return "|".join(normalize(part) for part in parts if str(part or "").strip())


def stable_hash(*parts):
    raw = "|".join(str(part or "") for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def redact_text(value):
    text = str(value or "")
    text = re.sub(r"apikey=[^&\s]+", "apikey=<redacted>", text, flags=re.I)
    text = re.sub(r"api_key=[^&\s]+", "api_key=<redacted>", text, flags=re.I)
    text = re.sub(r"token=[^&\s]+", "token=<redacted>", text, flags=re.I)
    return text


def read_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path, payload):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def provider_config(provider_id):
    try:
        return inkdrop_state.provider_config(INKDROP_STATE_DB, provider_id) or {}
    except Exception:
        return {}


def int_setting(settings, key, default, minimum=None, maximum=None):
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
    try:
        value = float(settings.get(key, default))
    except (TypeError, ValueError):
        value = float(default)
    if minimum is not None:
        value = max(float(minimum), value)
    if maximum is not None:
        value = min(float(maximum), value)
    return value


def apply_rss_provider_settings():
    global FEED_URL, USER_AGENT, DEFAULT_LIMIT, DEFAULT_MAX_AUTO, DEFAULT_MAX_PER_SERIES
    global NO_RESULT_COOLDOWN_HOURS, BAD_RESULT_COOLDOWN_HOURS, MAX_BACKOFF_SECONDS, RSS_PROVIDER_ENABLED
    config = provider_config("rss")
    settings = config.get("settings") if isinstance(config.get("settings"), dict) else {}
    RSS_PROVIDER_ENABLED = bool(config.get("enabled", True))
    feed_url = str(settings.get("feed_url") or config.get("base_url") or FEED_URL).strip()
    if feed_url:
        FEED_URL = feed_url
    user_agent = str(settings.get("user_agent") or USER_AGENT).strip()
    if user_agent:
        USER_AGENT = user_agent
    DEFAULT_LIMIT = int_setting(settings, "default_limit", DEFAULT_LIMIT, 1, 100)
    DEFAULT_MAX_AUTO = int_setting(settings, "max_auto", DEFAULT_MAX_AUTO, 0, 50)
    DEFAULT_MAX_PER_SERIES = int_setting(settings, "max_per_series", DEFAULT_MAX_PER_SERIES, 0, 20)
    NO_RESULT_COOLDOWN_HOURS = float_setting(settings, "no_result_cooldown_hours", NO_RESULT_COOLDOWN_HOURS, 0, 24 * 30)
    BAD_RESULT_COOLDOWN_HOURS = float_setting(settings, "bad_result_cooldown_hours", BAD_RESULT_COOLDOWN_HOURS, 0, 24 * 30)
    MAX_BACKOFF_SECONDS = int(float_setting(settings, "max_backoff_hours", MAX_BACKOFF_SECONDS / 3600, 0.25, 24 * 30) * 3600)
    return {
        "enabled": RSS_PROVIDER_ENABLED,
        "source": config.get("source") if config else "defaults",
        "feed_url": FEED_URL,
        "default_limit": DEFAULT_LIMIT,
        "max_auto": DEFAULT_MAX_AUTO,
        "max_per_series": DEFAULT_MAX_PER_SERIES,
    }


def log(event, payload):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    safe = {"ts": time.time(), "event": event}
    for key, value in dict(payload).items():
        if "url" in key.lower() or "key" in key.lower() or "token" in key.lower():
            safe[key] = "<redacted>"
        elif isinstance(value, str):
            safe[key] = redact_text(value)
        else:
            safe[key] = value
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(safe, sort_keys=True) + "\n")


def review_id(reason, series, issue, candidate_title, rss_title=None):
    return stable_hash(reason, series, issue, candidate_title, rss_title)


def review(reason, payload):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    candidate = payload.get("candidate") or {}
    rss_item = payload.get("rss_item") or {}
    record = {
        "ts": time.time(),
        "reason": reason,
        "source": "rss_discovery",
        "review_id": review_id(reason, payload.get("series"), payload.get("issue"), candidate.get("title"), rss_item.get("title")),
        **payload,
    }
    if not should_persist_review(reason):
        return record
    with REVIEW_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    return record


def truthy_env(name):
    value = str(os.environ.get(name) or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def queue_mode_enabled():
    return truthy_env("INKDROP_QUEUE_MODE")


def should_persist_review(reason):
    if str(reason or "") in QUEUE_MODE_REVIEW_REASONS:
        return True
    return truthy_env(MANUAL_REVIEW_PERSIST_ENV)


def load_actions():
    data = read_json(ACTIONS_FILE, {})
    return data if isinstance(data, dict) else {}


def load_aliases():
    data = read_json(ALIASES_FILE, {})
    out = {}
    if isinstance(data, dict):
        for series, aliases in data.items():
            if isinstance(aliases, list):
                out[series] = [str(alias) for alias in aliases if str(alias).strip()]
    return out


def load_bad_matches():
    data = read_json(BAD_MATCHES_FILE, {})
    return data if isinstance(data, dict) else {}


def load_cache():
    data = read_json(CACHE_FILE, {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("feed", {})
    data.setdefault("processed", {})
    data.setdefault("no_result", {})
    data.setdefault("bad_result", {})
    return data


def save_cache(cache):
    write_json(CACHE_FILE, cache)


def title_words_pattern(title):
    words = re.findall(r"[a-z0-9]+", str(title or "").lower())
    return r"[\W_]+".join(re.escape(word) for word in words)


def issue_int(issue_number):
    try:
        return int(float(str(issue_number)))
    except (TypeError, ValueError):
        return None


def is_pack(raw_title):
    text = str(raw_title or "")
    return bool(re.search(r"\b(?:v|vol(?:ume)?\.?|ch(?:apter)?\.?|issue\s*)?0*\d{1,4}\s*[-–]\s*(?:v|vol(?:ume)?\.?|ch(?:apter)?\.?|issue\s*)?0*\d{1,4}\b|\(\s*\d{1,4}\s*[-–]\s*\d{1,4}\s*\+?\s*\)|\bcomplete\b|\bomnibus\b|\bcompendium\b", text, re.I))


def non_english_hint(raw_title):
    if inkdrop_language is not None:
        language = inkdrop_language.classify_release_language(
            title=raw_title,
            preferred_languages=("en",),
            unknown_policy="allow_if_exact",
        )
        return bool(language.get("blocked"))
    return bool(re.search(r"\b(spanish|espanol|spa|castellano|latino|latam|es-la|es-es|pt-br|french|german|italian|portuguese|russian)\b", str(raw_title or ""), re.I))


def number_matches(issue_number, raw_title):
    n = issue_int(issue_number)
    if n is None:
        return False
    text = str(raw_title or "")
    patterns = [
        rf"(^|[^0-9])0*{n}([^0-9]|$)",
        rf"\bissue[\W_]+0*{n}([^0-9]|$)",
        rf"\b#\s*0*{n}([^0-9]|$)",
        rf"\bv0*{n}([^0-9]|$)",
        rf"\bvol(?:ume)?\.?[\W_]+0*{n}([^0-9]|$)",
        rf"\bchapter[\W_]+0*{n}([^0-9]|$)",
        rf"\bch\.?[\W_]+0*{n}([^0-9]|$)",
    ]
    return any(re.search(pattern, text, re.I) for pattern in patterns)


def issue_number_tokens(issue_number):
    n = issue_int(issue_number)
    if n is None:
        return set()
    return {str(n), f"{n:02d}", f"{n:03d}", f"{n:04d}"}


def strict_title_issue_prefix(series, aliases, issue_number, raw_title):
    raw_tokens = re.findall(r"[a-z0-9]+", str(raw_title or "").lower())
    wanted_numbers = issue_number_tokens(issue_number)
    if not raw_tokens or not wanted_numbers:
        return None
    for title in [series, *aliases]:
        title_tokens = re.findall(r"[a-z0-9]+", str(title or "").lower())
        if not title_tokens:
            continue
        starts = [0]
        if raw_tokens[:1] == ["the"] and title_tokens[:1] != ["the"]:
            starts.append(1)
        for start in starts:
            end = start + len(title_tokens)
            if raw_tokens[start:end] != title_tokens:
                continue
            if end < len(raw_tokens) and raw_tokens[end] in wanted_numbers:
                return title
            if end < len(raw_tokens) and re.fullmatch(rf"v0*{issue_int(issue_number)}", raw_tokens[end] or ""):
                return title
            if end + 1 < len(raw_tokens) and raw_tokens[end] in {"issue", "vol", "volume", "chapter", "ch"} and raw_tokens[end + 1] in wanted_numbers:
                return title
    return None


def title_matches(series, aliases, raw_title):
    raw = str(raw_title or "")
    for title in [series, *aliases]:
        pattern = title_words_pattern(title)
        if pattern and re.search(rf"(^|[^a-z0-9]){pattern}([^a-z0-9]|$)", raw, re.I):
            return title
    return None


def result_key(result):
    return slug_key(result.get("indexer"), result.get("protocol"), result.get("title"))


def category_ids(result):
    ids = set()
    for category in result.get("categories") or []:
        if isinstance(category, dict):
            cid = category.get("id")
            if cid is not None:
                ids.add(int(cid))
            for sub in category.get("subCategories") or []:
                if isinstance(sub, dict) and sub.get("id") is not None:
                    ids.add(int(sub["id"]))
    return ids


def score_release_title(series, issue_number, aliases, raw_title, categories=None):
    matched_title = title_matches(series, aliases, raw_title)
    strict_title = strict_title_issue_prefix(series, aliases, issue_number, raw_title)
    has_number = number_matches(issue_number, raw_title)
    pack = is_pack(raw_title)
    reasons = []
    score = 0

    if matched_title:
        score += 35
        reasons.append(f"title/alias match: {matched_title}")
        if strict_title:
            score += 20
            reasons.append("title directly followed by issue/volume")
        else:
            score -= 25
            reasons.append("title match is not a strict issue prefix")
    else:
        reasons.append("series title/alias not exact")
    if has_number:
        score += 30
        reasons.append("issue/volume number match")
    else:
        reasons.append("issue/volume number unclear")
    if categories and any("comic" in str(cat).lower() or "manga" in str(cat).lower() for cat in categories):
        score += 5
        reasons.append("feed comic/manga category")
    if re.search(r"\b(cbr|cbz|digital|web|webrip)\b", str(raw_title or ""), re.I):
        score += 5
    if pack:
        score -= 45
        reasons.append("pack/range blocked for unattended grab")
    if non_english_hint(raw_title):
        score -= 35
        reasons.append("non-English language hint")
    return {
        "score": max(0, min(100, score)),
        "matched_title": matched_title,
        "strict_title": strict_title,
        "has_number": has_number,
        "pack": pack,
        "reasons": reasons,
    }


def score_result(series, issue_number, aliases, result, bad_matches):
    raw = result.get("title") or ""
    base = score_release_title(series, issue_number, aliases, raw, [])
    protocol = result.get("protocol")
    seeders = result.get("seeders")
    cats = category_ids(result)
    reasons = list(base["reasons"])
    score = base["score"]

    if 7030 in cats:
        score += 5
        reasons.append("Prowlarr comic category")
    if 7000 in cats:
        score += 5
        reasons.append("Prowlarr manga/literature category")
    if protocol == "usenet":
        score += 10
        reasons.append("usenet availability")
    elif protocol == "torrent":
        if seeders and seeders >= 1:
            score += 10
            reasons.append(f"{seeders} seeders")
        else:
            score -= 35
            reasons.append("zero-seed torrent")
    if result_key(result) in bad_matches:
        score -= 100
        reasons.append("known bad match")
    return {
        "score": max(0, min(100, score)),
        "matched_title": base["matched_title"],
        "strict_title": base["strict_title"],
        "has_number": base["has_number"],
        "pack": base["pack"],
        "reasons": reasons,
    }


def safe_candidate(result):
    return {
        "title": result.get("title"),
        "indexer": result.get("indexer"),
        "indexerId": result.get("indexerId"),
        "protocol": result.get("protocol"),
        "seeders": result.get("seeders"),
        "leechers": result.get("leechers"),
        "size": result.get("size"),
        "publishDate": result.get("publishDate"),
        "categories": sorted(category_ids(result)),
    }


def raw_review_candidate(result):
    data = safe_candidate(result)
    data["downloadUrl"] = result.get("downloadUrl")
    return data


def safe_rss_item(item):
    return {
        "title": item.get("title"),
        "link": item.get("link"),
        "published": item.get("published"),
        "categories": item.get("categories") or [],
        "id": item.get("id"),
    }


def quality_rules_for(missing_mod):
    if hasattr(missing_mod, "quality_language_rules"):
        return missing_mod.quality_language_rules(refresh=True)
    return {}


def quality_rule_summary(missing_mod, rules):
    if hasattr(missing_mod, "quality_rule_summary"):
        return missing_mod.quality_rule_summary(rules)
    return {}


def quality_block_message(missing_mod, blocker, english=None):
    if hasattr(missing_mod, "quality_block_message"):
        return missing_mod.quality_block_message(blocker, english=english)
    text = str(blocker or "").strip()
    if text.startswith("blocked_release_term:"):
        return "blocked release term: " + text.split(":", 1)[1].strip()
    return text or "blocked by quality/language settings"


def quality_block_status(missing_mod, blocker):
    if hasattr(missing_mod, "quality_block_attempt_status"):
        return missing_mod.quality_block_attempt_status(blocker)
    text = str(blocker or "").lower()
    return "language_blocked" if "language" in text or "english" in text or "release_term" in text else "quality_blocked"


def configured_blocked_release_reason(acquire, payload, rules):
    if not hasattr(acquire, "blocked_release_term_reason"):
        return ""
    return acquire.blocked_release_term_reason(payload, (rules or {}).get("blocked_release_terms") or [])


def source_item_quality_blocker(acquire, item, rules):
    payload = {
        "title": item.get("title"),
        "indexer": item.get("source") or item.get("link") or "rss",
        "categories": item.get("categories") or [],
    }
    blocked = configured_blocked_release_reason(acquire, payload, rules)
    return blocked.replace("blocked release term", "blocked_release_term", 1) if blocked else ""


def prowlarr_result_quality_blocker(missing_mod, acquire, result, rules):
    english = acquire.classify_english_result(result) if hasattr(acquire, "classify_english_result") else None
    blocker = missing_mod.quality_rule_block_reason(result, rules, english=english) if hasattr(missing_mod, "quality_rule_block_reason") else ""
    return blocker, english


def record_quality_block_attempt(missing_mod, row, source, query, result, blocker, english, rules, dry_run):
    if not hasattr(missing_mod, "record_inkdrop_queue_attempt"):
        return None
    return missing_mod.record_inkdrop_queue_attempt(
        row,
        quality_block_status(missing_mod, blocker),
        quality_block_message(missing_mod, blocker, english=english),
        query=query,
        candidate=result,
        dry_run=dry_run,
        source=source,
        extra={
            "quality_blocker": blocker,
            "english_confidence": english,
            "quality_language_rules": quality_rule_summary(missing_mod, rules),
        },
    )


def query_variants(missing, series_aliases):
    missing_mod = load_missing()
    queries = list(missing_mod.query_variants(missing["title"], missing["issue_number"], year=missing.get("year")))
    for alias in series_aliases:
        for query in missing_mod.query_variants(alias, missing["issue_number"], year=missing.get("year")):
            if normalize(query) not in {normalize(q) for q in queries}:
                queries.append(query)
    return queries


def send(acquire, chosen, query, dry_run, target=None):
    title = chosen.get("title") or "unknown"
    if dry_run:
        return {"dry_run": True, "category": "comics"}
    url = chosen.get("downloadUrl")
    if chosen.get("protocol") == "torrent":
        outcome = acquire.qbit_add(url, title, "comics", dry_run=False)
    elif chosen.get("protocol") == "usenet":
        outcome = acquire.sab_add(url, title, dry_run=False)
    else:
        raise RuntimeError(f"unsupported protocol: {chosen.get('protocol')}")
    acquire.record_pending_import(query, "comics", chosen, outcome if isinstance(outcome, dict) else {}, target=target)
    return outcome


def pack_auto_approve_action(
    missing_mod,
    acquire,
    row,
    query,
    result,
    source,
    source_item_key,
    source_item,
    confidence_score,
    row_context,
    cache,
    dry_run,
):
    pack_item = missing_mod.make_pack_candidate(acquire, row, query, result, source)
    if not pack_item:
        return None
    pack_item.update(row_context or {})
    auto_result = missing_mod.auto_approve_pack_candidate(acquire, pack_item, cache, dry_run)
    auto_status = str(auto_result.get("status") or "")
    if auto_status == "pack_already_handled":
        return {
            "skip": True,
            "reason": "pack_already_in_flight_or_finished",
            "pack_item": pack_item,
            "auto_result": auto_result,
        }
    if auto_status == "blocked_active_pack":
        return {
            "skip": True,
            "reason": "pack_auto_waiting_active_pack",
            "pack_item": pack_item,
            "auto_result": auto_result,
        }
    if auto_result.get("status") not in {"pack_auto_approved", "dry_run"}:
        return {
            "blocked": True,
            "reason": auto_result.get("reason") or auto_result.get("status") or "pack_not_auto_approved",
            "pack_item": pack_item,
            "auto_result": auto_result,
        }
    candidate = pack_item.get("candidate") or {}
    action = {
        "series": row.get("title"),
        "issue": row.get("issue_number"),
        "query": query,
        "title": candidate.get("title"),
        "indexer": candidate.get("indexer"),
        "protocol": candidate.get("protocol"),
        "seeders": candidate.get("seeders"),
        source_item_key: source_item,
        "candidate": safe_candidate(result),
        "confidence_score": confidence_score,
        "pack_auto_approved": True,
        "pack_review_id": auto_result.get("review_id"),
        "pack_info": pack_item.get("pack_info"),
        "pack_match": pack_item.get("pack_match"),
        "pack_decision": pack_item.get("pack_decision"),
        "outcome": auto_result,
        **(row_context or {}),
    }
    return {"action": action, "pack_item": pack_item, "auto_result": auto_result}


def fetch_feed(cache, args):
    feed_state = cache.setdefault("feed", {})
    now = time.time()
    if not args.force_feed and feed_state.get("backoff_until", 0) > now:
        return None, "backoff"

    if args.feed_file:
        xml = Path(args.feed_file).read_text(encoding="utf-8")
        return xml, "file"

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.5",
    }
    if not args.force_feed and feed_state.get("etag"):
        headers["If-None-Match"] = feed_state["etag"]
    if not args.force_feed and feed_state.get("last_modified"):
        headers["If-Modified-Since"] = feed_state["last_modified"]
    try:
        response = requests.get(FEED_URL, headers=headers, timeout=30)
        if response.status_code == 304:
            feed_state["last_status"] = 304
            feed_state["last_poll"] = now
            feed_state["failure_count"] = 0
            return "", "not_modified"
        response.raise_for_status()
    except Exception as exc:
        failures = int(feed_state.get("failure_count") or 0) + 1
        backoff = min(MAX_BACKOFF_SECONDS, 900 * failures)
        feed_state.update({
            "failure_count": failures,
            "backoff_until": now + backoff,
            "last_error": redact_text(exc),
            "last_poll": now,
        })
        raise

    feed_state.update({
        "etag": response.headers.get("ETag") or feed_state.get("etag"),
        "last_modified": response.headers.get("Last-Modified") or feed_state.get("last_modified"),
        "last_status": response.status_code,
        "last_poll": now,
        "last_error": None,
        "failure_count": 0,
        "backoff_until": 0,
    })
    return response.text, "fetched"


def xml_text(node, name):
    found = node.find(name)
    return (found.text or "").strip() if found is not None and found.text else ""


def parse_feed(xml):
    if not xml:
        return []
    root = ET.fromstring(xml)
    items = []
    for item in root.findall(".//channel/item"):
        title = xml_text(item, "title")
        link = xml_text(item, "link")
        guid = xml_text(item, "guid")
        published = xml_text(item, "pubDate")
        categories = [cat.text.strip() for cat in item.findall("category") if cat.text and cat.text.strip()]
        key = guid or link or stable_hash(title, published)
        parsed_date = None
        if published:
            try:
                parsed_date = email.utils.parsedate_to_datetime(published).isoformat()
            except (TypeError, ValueError):
                parsed_date = published
        items.append({
            "id": stable_hash(key),
            "title": title,
            "link": link,
            "published": parsed_date or published,
            "categories": categories,
        })
    return items


def best_rss_match(item, rows, aliases):
    best = None
    title = item.get("title") or ""
    for row in rows:
        series = row["title"]
        series_aliases = aliases.get(series, [])
        score = score_release_title(series, row["issue_number"], series_aliases, title, item.get("categories"))
        if not best or score["score"] > best["score"]["score"]:
            best = {"row": row, "score": score}
    return best


def best_prowlarr_result(
    acquire,
    row,
    aliases,
    bad_matches,
    limit,
    cache,
    now,
    *,
    missing_mod=None,
    quality_rules=None,
    source="rss",
    dry_run=False,
):
    no_result_cutoff = now - NO_RESULT_COOLDOWN_HOURS * 3600
    series_aliases = aliases.get(row["title"], [])
    missing_mod = missing_mod or load_missing()
    quality_rules = quality_rules if quality_rules is not None else quality_rules_for(missing_mod)
    best = None
    for query in query_variants(row, series_aliases):
        cache_key = slug_key(row["title"], row["issue_number"], query)
        if cache["no_result"].get(cache_key, 0) >= no_result_cutoff:
            continue
        results = acquire.prowlarr_search(query, "comics", limit=limit)
        if not results:
            cache["no_result"][cache_key] = now
            continue
        if hasattr(missing_mod, "filter_known_bad_results"):
            results, known_bad_samples = missing_mod.filter_known_bad_results(
                cache,
                row,
                row["title"],
                row["issue_number"],
                results,
                query=query,
                dry_run=dry_run,
                source=source,
            )
            if known_bad_samples:
                log(
                    f"{source}_known_bad_candidates_skipped",
                    {
                        "series": row["title"],
                        "issue": row["issue_number"],
                        "query": query,
                        "skipped": len(known_bad_samples),
                        "samples": known_bad_samples[:3],
                    },
                )
            if not results:
                continue
        for result in results:
            blocker, english = prowlarr_result_quality_blocker(missing_mod, acquire, result, quality_rules)
            if blocker:
                record_quality_block_attempt(missing_mod, row, source, query, result, blocker, english, quality_rules, dry_run)
                continue
            score = score_result(row["title"], row["issue_number"], series_aliases, result, bad_matches)
            candidate = {"query": query, "result": result, "score": score}
            if not best or score["score"] > best["score"]["score"]:
                best = candidate
    return best


def discover_rss(args):
    now = time.time()
    cache = load_cache()
    errors = []

    summary = {
        "mode": "dry_run" if args.dry_run else "live",
        "source": "getcomics-rss-discovery",
        "feed_url": FEED_URL,
        "started_at": now,
        "last_poll": None,
        "feed_status": None,
        "status": "OK",
        "series_count": 0,
        "missing_targets": 0,
        "feed_items": 0,
        "candidates_found": 0,
        "auto_grabbed": 0,
        "sent_to_review": 0,
        "skipped": 0,
        "failed": 0,
        "backoff_until": cache.get("feed", {}).get("backoff_until") or None,
        "actions": [],
        "reviews": [],
        "skips": [],
        "errors": errors,
    }
    if not RSS_PROVIDER_ENABLED:
        summary.update({
            "status": "DISABLED",
            "skipped": 1,
            "skips": [{"reason": "provider_disabled"}],
            "finished_at": time.time(),
        })
        summary["duration_seconds"] = round(summary["finished_at"] - summary["started_at"], 2)
        write_json(STATUS_FILE, summary)
        log("summary", {k: summary[k] for k in ("source", "status", "skipped", "duration_seconds")})
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0

    acquire = load_acquire()
    missing_mod = load_missing()
    quality_rules = quality_rules_for(missing_mod)
    summary["quality_language_rules"] = quality_rule_summary(missing_mod, quality_rules)
    aliases = load_aliases()
    bad_matches = load_bad_matches()
    actions = load_actions()
    ignored = set(actions.get("ignored") or [])
    bad_review_ids = set(actions.get("bad") or [])
    processed = cache.setdefault("processed", {})
    auto_by_series = {}

    try:
        xml, feed_status = fetch_feed(cache, args)
        summary["feed_status"] = feed_status
    except Exception as exc:
        summary["status"] = "WARN"
        summary["failed"] += 1
        summary["errors"].append({"stage": "feed_fetch", "error": redact_text(exc)})
        summary["backoff_until"] = cache.get("feed", {}).get("backoff_until") or None
        summary["last_poll"] = cache.get("feed", {}).get("last_poll")
        summary["finished_at"] = time.time()
        summary["duration_seconds"] = round(summary["finished_at"] - summary["started_at"], 2)
        save_cache(cache)
        write_json(STATUS_FILE, summary)
        log("summary", {k: summary[k] for k in ("source", "status", "failed", "duration_seconds")})
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 1

    summary["last_poll"] = cache.get("feed", {}).get("last_poll") or now
    if feed_status == "backoff":
        summary["status"] = "WATCH"
        summary["skipped"] += 1
        summary["skips"].append({"reason": "feed_backoff_active"})
    if feed_status == "not_modified":
        items = []
    else:
        items = parse_feed(xml)
    summary["feed_items"] = len(items)

    series_names = tuple(args.series) if args.series else missing_mod.monitored_series_names()
    _, raw_pending = missing_mod.pending_entries()
    rows = missing_mod.missing_issues(series_names, fresh_days=args.fresh_days)
    if args.only:
        allowed = {value.lower() for value in args.only}
        rows = [row for row in rows if row["title"].lower() in allowed]
    summary["series_count"] = len(series_names)
    summary["missing_targets"] = len(rows)

    for item in items:
        item_key = item["id"]
        if processed.get(item_key) and not args.reprocess_seen:
            summary["skipped"] += 1
            summary["skips"].append({"rss_title": item.get("title"), "reason": "already_seen"})
            continue
        match = best_rss_match(item, rows, aliases)
        if not match:
            summary["skipped"] += 1
            summary["skips"].append({"rss_title": item.get("title"), "reason": "low_signal"})
            if not args.dry_run:
                processed[item_key] = {"ts": now, "decision": "low_signal"}
            continue

        row = match["row"]
        series = row["title"]
        issue = row["issue_number"]
        row_context = missing_mod.row_output_context(row)
        rss_score = match["score"]
        rss_public = safe_rss_item(item)
        source_blocker = source_item_quality_blocker(acquire, item, quality_rules)
        if source_blocker:
            reason = quality_block_message(missing_mod, source_blocker)
            record_quality_block_attempt(
                missing_mod,
                row,
                "rss",
                item.get("title"),
                {"title": item.get("title"), "indexer": "rss", "categories": item.get("categories") or []},
                source_blocker,
                None,
                quality_rules,
                args.dry_run,
            )
            summary["skipped"] += 1
            summary["skips"].append({
                "series": series,
                "issue": issue,
                "rss_title": item.get("title"),
                "reason": quality_block_status(missing_mod, source_blocker),
                "detail": reason,
                **row_context,
            })
            if not args.dry_run:
                processed[item_key] = {"ts": now, "decision": quality_block_status(missing_mod, source_blocker), "series": series, "issue": issue}
            continue
        if rss_score["score"] < 55:
            if rss_score["pack"] and rss_score["matched_title"] and rss_score["has_number"]:
                payload = {
                    "series": series,
                    "issue": issue,
                    "rss_item": rss_public,
                    "confidence_score": rss_score["score"],
                    "why": [*rss_score["reasons"], "broad pack/range requires explicit review"],
                    **row_context,
                }
                summary["candidates_found"] += 1
                summary["sent_to_review"] += 1
                summary["reviews"].append({**payload, "reason": "rss_pack_requires_review"})
                if not args.dry_run:
                    review("rss_pack_requires_review", payload)
                    processed[item_key] = {"ts": now, "decision": "review", "series": series, "issue": issue}
                continue
            summary["skipped"] += 1
            summary["skips"].append({"rss_title": item.get("title"), "reason": "low_signal", **row_context})
            if not args.dry_run:
                processed[item_key] = {"ts": now, "decision": "low_signal"}
            continue
        summary["candidates_found"] += 1

        if auto_by_series.get(series, 0) >= args.max_per_series:
            summary["skipped"] += 1
            summary["skips"].append({"series": series, "issue": issue, "rss_title": item.get("title"), "reason": "per_series_limit", **row_context})
            continue
        if summary["auto_grabbed"] >= args.max_auto:
            summary["skipped"] += 1
            summary["skips"].append({"series": series, "issue": issue, "rss_title": item.get("title"), "reason": "auto_limit", **row_context})
            continue
        if missing_mod.issue_has_pending(series, issue, raw_pending):
            summary["skipped"] += 1
            summary["skips"].append({"series": series, "issue": issue, "rss_title": item.get("title"), "reason": "already_pending", **row_context})
            if not args.dry_run:
                processed[item_key] = {"ts": now, "decision": "already_pending", "series": series, "issue": issue}
            continue
        if not missing_mod.folder_is_safe(row.get("folder")):
            payload = {
                "series": series,
                "issue": issue,
                "rss_item": rss_public,
                "confidence_score": rss_score["score"],
                "why": rss_score["reasons"],
                "folder": row.get("folder"),
                **row_context,
            }
            summary["sent_to_review"] += 1
            summary["reviews"].append({**payload, "reason": "rss_unsafe_target_folder"})
            if not args.dry_run:
                review("rss_unsafe_target_folder", payload)
                processed[item_key] = {"ts": now, "decision": "review", "series": series, "issue": issue}
            continue

        try:
            prowlarr = best_prowlarr_result(
                acquire,
                row,
                aliases,
                bad_matches,
                args.limit,
                cache,
                now,
                missing_mod=missing_mod,
                quality_rules=quality_rules,
                source="rss",
                dry_run=args.dry_run,
            )
        except Exception as exc:
            summary["failed"] += 1
            payload = {
                "series": series,
                "issue": issue,
                "rss_item": rss_public,
                "confidence_score": rss_score["score"],
                "why": rss_score["reasons"],
                "error": redact_text(exc),
                **row_context,
            }
            summary["errors"].append(payload)
            log("prowlarr_search_error", payload)
            continue

        if not prowlarr:
            payload = {
                "series": series,
                "issue": issue,
                "rss_item": rss_public,
                "confidence_score": rss_score["score"],
                "query": (query_variants(row, aliases.get(series, [])) or [series])[0],
                "why": [*rss_score["reasons"], "RSS matched a missing item but Prowlarr had no usable result"],
                **row_context,
            }
            reason = "rss_high_confidence_no_prowlarr_result" if rss_score["score"] >= 85 else "rss_medium_confidence"
            summary["sent_to_review"] += 1
            summary["reviews"].append({**payload, "reason": reason})
            if not args.dry_run:
                review(reason, payload)
                processed[item_key] = {"ts": now, "decision": "review", "series": series, "issue": issue}
            continue

        result = prowlarr["result"]
        result_score = prowlarr["score"]
        payload = {
            "series": series,
            "issue": issue,
            "query": prowlarr["query"],
            "rss_item": rss_public,
            "confidence_score": min(100, round((rss_score["score"] * 0.45) + (result_score["score"] * 0.55))),
            "rss_confidence_score": rss_score["score"],
            "prowlarr_confidence_score": result_score["score"],
            "source_indexer": result.get("indexer"),
            "matched_title": result_score["matched_title"],
            "candidate": raw_review_candidate(result),
            "why": [f"RSS: {reason}" for reason in rss_score["reasons"]] + [f"Prowlarr: {reason}" for reason in result_score["reasons"]],
            **row_context,
        }
        if rss_score["pack"] or result_score["pack"]:
            try:
                pack_auto = pack_auto_approve_action(
                    missing_mod,
                    acquire,
                    row,
                    prowlarr["query"],
                    result,
                    "rss_discovery_pack",
                    "rss_item",
                    rss_public,
                    payload["confidence_score"],
                    row_context,
                    cache,
                    args.dry_run,
                )
            except Exception as exc:
                payload["pack_auto_error"] = redact_text(exc)
                summary["failed"] += 1
                summary["errors"].append({**payload, "candidate": safe_candidate(result)})
                log("rss_pack_auto_approve_error", payload)
                pack_auto = None
            if pack_auto and pack_auto.get("action"):
                action = pack_auto["action"]
                summary["auto_grabbed"] += 1
                auto_by_series[series] = auto_by_series.get(series, 0) + 1
                summary["actions"].append(action)
                log("rss_pack_auto_approved", action)
                if not args.dry_run:
                    processed[item_key] = {"ts": now, "decision": "pack_auto_approved", "series": series, "issue": issue}
                continue
            if pack_auto and pack_auto.get("skip"):
                skip = {
                    "series": series,
                    "issue": issue,
                    "rss_title": item.get("title"),
                    "reason": pack_auto.get("reason") or "pack_auto_waiting",
                    "pack_auto_result": pack_auto.get("auto_result"),
                    **row_context,
                }
                summary["skipped"] += 1
                summary["skips"].append(skip)
                log("rss_pack_auto_skip", skip)
                if not args.dry_run and skip["reason"] == "pack_already_in_flight_or_finished":
                    processed[item_key] = {"ts": now, "decision": skip["reason"], "series": series, "issue": issue}
                continue
            if pack_auto and pack_auto.get("blocked"):
                payload["pack_auto_blocked_reason"] = pack_auto.get("reason")
                payload["pack_auto_result"] = pack_auto.get("auto_result")
        # Compute the review reason up front and derive `rid` from it so the
        # already-rejected gate below checks the exact id a prior review would
        # have been stored under -- not a hardcoded "rss_medium_confidence"
        # that only matches one of the three possible reasons. Using the wrong
        # id let a candidate the user had marked bad/ignored under
        # rss_high_confidence_manual_review or rss_pack_requires_review sail
        # through this gate and get auto-grabbed on a later poll.
        reason = "rss_medium_confidence"
        if rss_score["pack"] or result_score["pack"]:
            reason = "rss_pack_requires_review"
        elif payload["confidence_score"] >= 85:
            reason = "rss_high_confidence_manual_review"
        rid = review_id(reason, series, issue, result.get("title"), item.get("title"))
        is_high = (
            payload["confidence_score"] >= 90
            and rss_score["strict_title"]
            and result_score["strict_title"]
            and not rss_score["pack"]
            and not result_score["pack"]
            and rid not in ignored
            and rid not in bad_review_ids
        )
        if is_high:
            try:
                outcome = send(acquire, result, prowlarr["query"], args.dry_run, target=row)
                action = {
                    "series": series,
                    "issue": issue,
                    "query": prowlarr["query"],
                    "rss_item": rss_public,
                    "candidate": safe_candidate(result),
                    "confidence_score": payload["confidence_score"],
                    "outcome": outcome,
                    **row_context,
                }
                summary["auto_grabbed"] += 1
                auto_by_series[series] = auto_by_series.get(series, 0) + 1
                summary["actions"].append(action)
                log("rss_auto_grab", action)
                if not args.dry_run:
                    processed[item_key] = {"ts": now, "decision": "auto_grabbed", "series": series, "issue": issue}
                continue
            except Exception as exc:
                payload["error"] = redact_text(exc)
                summary["failed"] += 1
                summary["errors"].append({**payload, "candidate": safe_candidate(result)})
                log("rss_auto_grab_error", payload)

        summary["sent_to_review"] += 1
        summary["reviews"].append({**payload, "candidate": safe_candidate(result), "reason": reason, "review_id": rid})
        if not args.dry_run:
            review(reason, payload)
            processed[item_key] = {"ts": now, "decision": "review", "series": series, "issue": issue}

    summary["finished_at"] = time.time()
    summary["duration_seconds"] = round(summary["finished_at"] - summary["started_at"], 2)
    if summary["failed"] and summary["status"] == "OK":
        summary["status"] = "WATCH"
    if not args.dry_run:
        save_cache(cache)
    safe_status = json.loads(json.dumps(summary))
    for review_row in safe_status.get("reviews", []):
        if isinstance(review_row.get("candidate"), dict):
            review_row["candidate"].pop("downloadUrl", None)
    for error_row in safe_status.get("errors", []):
        if isinstance(error_row, dict) and isinstance(error_row.get("candidate"), dict):
            error_row["candidate"].pop("downloadUrl", None)
        if isinstance(error_row, dict) and error_row.get("error"):
            error_row["error"] = redact_text(error_row["error"])
    write_json(STATUS_FILE, safe_status)
    log("summary", {
        "mode": summary["mode"],
        "source": summary["source"],
        "feed_status": summary["feed_status"],
        "feed_items": summary["feed_items"],
        "candidates_found": summary["candidates_found"],
        "auto_grabbed": summary["auto_grabbed"],
        "sent_to_review": summary["sent_to_review"],
        "skipped": summary["skipped"],
        "failed": summary["failed"],
        "duration_seconds": summary["duration_seconds"],
    })
    print(json.dumps(safe_status, indent=2, sort_keys=True))
    return 0 if not summary["failed"] else 1


def main():
    apply_rss_provider_settings()
    parser = argparse.ArgumentParser(description="Respectful RSS discovery for InkDrop")
    parser.add_argument("--series", action="append", default=[], help="limit run to InkDrop's watched series set")
    parser.add_argument("--only", action="append", default=[], help="filter processed series by exact title")
    parser.add_argument("--fresh-days", type=float)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--max-auto", type=int, default=DEFAULT_MAX_AUTO)
    parser.add_argument("--max-per-series", type=int, default=DEFAULT_MAX_PER_SERIES)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force-feed", action="store_true", help="ignore ETag/Last-Modified for this run")
    parser.add_argument("--reprocess-seen", action="store_true", help="re-evaluate cached feed items")
    parser.add_argument("--feed-file", help="test with a local RSS XML file instead of the live feed")
    args = parser.parse_args()
    raise SystemExit(discover_rss(args))


if __name__ == "__main__":
    main()
