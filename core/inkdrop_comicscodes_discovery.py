#!/usr/bin/env python3
"""Guarded ComicsCodes discovery for InkDrop.

ComicsCodes is used only as a shallow candidate source. InkDrop-owned wanted
rows are the source of truth, and Prowlarr remains the guarded acquisition
path. This worker does not bypass site protections and does not scrape deep
download-host pages.
"""
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))


import argparse
import html.parser
import importlib.util
import json
import os
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

from core import inkdrop_runtime_config


STATE_DIR = inkdrop_runtime_config.state_dir()
LOG_DIR = inkdrop_runtime_config.log_dir()
RSS_WORKER_PATH = Path(os.environ.get("INKDROP_RSS_DISCOVERY_SCRIPT") or Path(__file__).resolve().with_name("inkdrop_rss_discovery.py"))
STATUS_FILE = STATE_DIR / "comicscodes-discovery-status.json"
LOG_FILE = LOG_DIR / "comicscodes-discovery.log"
CACHE_FILE = STATE_DIR / "comicscodes-discovery-cache.json"

SOURCE_URLS = [
    "https://comics.codes/feed/",
    "https://comics.codes/",
]
LIST_URLS = [
    "https://comics.codes/all-comics-list/",
    "https://comics.codes/all-manga-list/",
]
USER_AGENT = "InkDrop-ComicsCodes-Discovery/1.0 (+guarded slow candidate discovery)"
DEFAULT_LIMIT = 12
DEFAULT_MAX_AUTO = 5
DEFAULT_MAX_PER_SERIES = 1
MAX_BACKOFF_SECONDS = 12 * 3600
COMICSCODES_PROVIDER_ENABLED = True


def load_rss_worker():
    spec = importlib.util.spec_from_file_location("inkdrop_rss_discovery", RSS_WORKER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rss = load_rss_worker()


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


def list_setting(settings, key, default):
    value = settings.get(key) if isinstance(settings, dict) else None
    if isinstance(value, list):
        out = [str(item).strip() for item in value if str(item).strip()]
    elif isinstance(value, str):
        out = [part.strip() for part in re.split(r"[\n,]+", value) if part.strip()]
    else:
        out = []
    return out or list(default)


def apply_comicscodes_provider_settings():
    global SOURCE_URLS, LIST_URLS, USER_AGENT, DEFAULT_LIMIT, DEFAULT_MAX_AUTO, DEFAULT_MAX_PER_SERIES
    global MAX_BACKOFF_SECONDS, COMICSCODES_PROVIDER_ENABLED
    config = rss.provider_config("comicscodes")
    settings = config.get("settings") if isinstance(config.get("settings"), dict) else {}
    COMICSCODES_PROVIDER_ENABLED = bool(config.get("enabled", True))
    SOURCE_URLS = list_setting(settings, "source_urls", SOURCE_URLS)
    LIST_URLS = list_setting(settings, "list_urls", LIST_URLS)
    user_agent = str(settings.get("user_agent") or USER_AGENT).strip()
    if user_agent:
        USER_AGENT = user_agent
    DEFAULT_LIMIT = rss.int_setting(settings, "default_limit", DEFAULT_LIMIT, 1, 100)
    DEFAULT_MAX_AUTO = rss.int_setting(settings, "max_auto", DEFAULT_MAX_AUTO, 0, 50)
    DEFAULT_MAX_PER_SERIES = rss.int_setting(settings, "max_per_series", DEFAULT_MAX_PER_SERIES, 0, 20)
    MAX_BACKOFF_SECONDS = int(rss.float_setting(settings, "max_backoff_hours", MAX_BACKOFF_SECONDS / 3600, 0.25, 24 * 30) * 3600)
    return {
        "enabled": COMICSCODES_PROVIDER_ENABLED,
        "source": config.get("source") if config else "defaults",
        "source_urls": SOURCE_URLS,
        "list_urls": LIST_URLS,
        "default_limit": DEFAULT_LIMIT,
        "max_auto": DEFAULT_MAX_AUTO,
        "max_per_series": DEFAULT_MAX_PER_SERIES,
    }


def log(event, payload):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    safe = {"ts": time.time(), "event": event}
    for key, value in dict(payload).items():
        if "url" in key.lower() and key not in {"source_url", "candidate_url"}:
            safe[key] = "<redacted>"
        elif isinstance(value, str):
            safe[key] = rss.redact_text(value)
        else:
            safe[key] = value
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(safe, sort_keys=True) + "\n")


def load_cache():
    data = read_json(CACHE_FILE, {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("sources", {})
    data.setdefault("processed", {})
    data.setdefault("no_result", {})
    data.setdefault("bad_result", {})
    return data


def source_key(url):
    return rss.stable_hash(url)


def blocked_by_challenge(response):
    if response.status_code in {401, 403, 429, 503}:
        mitigated = response.headers.get("Cf-Mitigated", "")
        server = response.headers.get("Server", "")
        text = response.text[:1000].lower() if response.text else ""
        return (
            "challenge" in mitigated.lower()
            or "cloudflare" in server.lower() and "challenge" in text
            or "challenges.cloudflare.com" in text
        )
    return False


def fetch_url(url, cache, args):
    sources = cache.setdefault("sources", {})
    key = source_key(url)
    state = sources.setdefault(key, {"url": url})
    now = time.time()
    if not args.force and state.get("backoff_until", 0) > now:
        return None, "backoff", state

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/xml;q=0.9, text/html;q=0.8, */*;q=0.3",
    }
    if not args.force and state.get("etag"):
        headers["If-None-Match"] = state["etag"]
    if not args.force and state.get("last_modified"):
        headers["If-Modified-Since"] = state["last_modified"]

    try:
        response = requests.get(url, headers=headers, timeout=25)
    except Exception as exc:
        failures = int(state.get("failure_count") or 0) + 1
        backoff = min(MAX_BACKOFF_SECONDS, 1800 * failures)
        state.update({
            "last_poll": now,
            "failure_count": failures,
            "last_error": rss.redact_text(exc),
            "backoff_until": now + backoff,
        })
        return None, "error", state

    state["last_poll"] = now
    state["last_status"] = response.status_code
    if response.status_code == 304:
        state["failure_count"] = 0
        state["last_error"] = None
        return "", "not_modified", state
    if blocked_by_challenge(response):
        failures = int(state.get("failure_count") or 0) + 1
        backoff = min(MAX_BACKOFF_SECONDS, 3600 * failures)
        state.update({
            "failure_count": failures,
            "last_error": "cloudflare_or_host_challenge",
            "backoff_until": now + backoff,
        })
        return None, "blocked_by_challenge", state
    if response.status_code >= 400:
        failures = int(state.get("failure_count") or 0) + 1
        backoff = min(MAX_BACKOFF_SECONDS, 1800 * failures)
        state.update({
            "failure_count": failures,
            "last_error": f"http_{response.status_code}",
            "backoff_until": now + backoff,
        })
        return None, "error", state

    state.update({
        "etag": response.headers.get("ETag") or state.get("etag"),
        "last_modified": response.headers.get("Last-Modified") or state.get("last_modified"),
        "failure_count": 0,
        "last_error": None,
        "backoff_until": 0,
        "content_type": response.headers.get("Content-Type"),
    })
    return response.text, "fetched", state


class LinkExtractor(html.parser.HTMLParser):
    def __init__(self, base_url):
        super().__init__()
        self.base_url = base_url
        self.links = []
        self._href = None
        self._text = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return
        attrs = dict(attrs)
        href = attrs.get("href")
        if not href:
            return
        self._href = urllib.parse.urljoin(self.base_url, href)
        self._text = []

    def handle_data(self, data):
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._href:
            title = re.sub(r"\s+", " ", "".join(self._text)).strip()
            self.links.append({"title": title, "link": self._href})
            self._href = None
            self._text = []


def parse_xml_items(text, source_url):
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    items = []
    for item in root.findall(".//channel/item"):
        title = rss.xml_text(item, "title")
        link = rss.xml_text(item, "link")
        guid = rss.xml_text(item, "guid")
        published = rss.xml_text(item, "pubDate")
        categories = [cat.text.strip() for cat in item.findall("category") if cat.text and cat.text.strip()]
        key = guid or link or rss.stable_hash(source_url, title, published)
        items.append({
            "id": rss.stable_hash("comicscodes", key),
            "title": title,
            "link": link,
            "published": published,
            "categories": categories,
            "source_url": source_url,
        })
    return items


def parse_html_items(text, source_url):
    parser = LinkExtractor(source_url)
    parser.feed(text or "")
    seen = set()
    items = []
    for link in parser.links:
        href = link.get("link") or ""
        title = link.get("title") or ""
        parsed = urllib.parse.urlparse(href)
        if parsed.netloc and parsed.netloc != "comics.codes":
            continue
        if not title or len(title) < 3:
            continue
        if re.search(r"\b(login|privacy|contact|dmca|advertis|request|comment|page \d+)\b", title, re.I):
            continue
        if re.search(r"/(tag|author|privacy|contact|dmca|wp-|feed|comments)/", parsed.path, re.I):
            continue
        key = rss.stable_hash(source_url, href, title)
        if key in seen:
            continue
        seen.add(key)
        items.append({
            "id": rss.stable_hash("comicscodes", key),
            "title": title,
            "link": href,
            "published": None,
            "categories": ["comicscodes"],
            "source_url": source_url,
        })
    return items


def parse_items(text, source_url):
    if not text:
        return []
    if "<rss" in text[:500].lower() or "<feed" in text[:500].lower():
        return parse_xml_items(text, source_url)
    return parse_html_items(text, source_url)


def source_item(item):
    return {
        "title": item.get("title"),
        "link": item.get("link"),
        "published": item.get("published"),
        "categories": item.get("categories") or [],
        "id": item.get("id"),
        "source": "ComicsCodes",
    }


def review(reason, payload):
    record = {
        "ts": time.time(),
        "reason": reason,
        "source": "comicscodes_discovery",
        "review_id": rss.review_id(reason, payload.get("series"), payload.get("issue"), (payload.get("candidate") or {}).get("title"), (payload.get("source_item") or {}).get("title")),
        **payload,
    }
    if str(reason or "") != "comicscodes_unsafe_target_folder" and not rss.truthy_env(rss.MANUAL_REVIEW_PERSIST_ENV):
        return record
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with rss.REVIEW_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    return record


def best_source_match(item, rows, aliases):
    best = None
    title = item.get("title") or ""
    categories = item.get("categories") or []
    for row in rows:
        series = row["title"]
        series_aliases = aliases.get(series, [])
        score = rss.score_release_title(series, row["issue_number"], series_aliases, title, categories)
        if not best or score["score"] > best["score"]["score"]:
            best = {"row": row, "score": score}
    return best


def discover(args):
    now = time.time()
    source_urls = list(args.source_url or [])
    if not source_urls:
        source_urls = SOURCE_URLS + (LIST_URLS if args.include_lists else [])

    summary = {
        "mode": "dry_run" if args.dry_run else "live",
        "source": "comicscodes-discovery",
        "source_urls": source_urls,
        "started_at": now,
        "status": "OK",
        "series_count": 0,
        "missing_targets": 0,
        "source_items": 0,
        "candidates_found": 0,
        "auto_grabbed": 0,
        "sent_to_review": 0,
        "skipped": 0,
        "failed": 0,
        "blocked_sources": 0,
        "backoff_sources": 0,
        "actions": [],
        "reviews": [],
        "skips": [],
        "errors": [],
        "sources": [],
    }
    if not COMICSCODES_PROVIDER_ENABLED:
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

    acquire = rss.load_acquire()
    missing_mod = rss.load_missing()
    quality_rules = rss.quality_rules_for(missing_mod)
    summary["quality_language_rules"] = rss.quality_rule_summary(missing_mod, quality_rules)
    aliases = rss.load_aliases()
    bad_matches = rss.load_bad_matches()
    actions = rss.load_actions()
    ignored = set(actions.get("ignored") or [])
    bad_review_ids = set(actions.get("bad") or [])
    cache = load_cache()
    processed = cache.setdefault("processed", {})
    auto_by_series = {}

    if args.source_file:
        text = Path(args.source_file).read_text(encoding="utf-8")
        items = parse_items(text, "file://" + str(Path(args.source_file)))
        summary["sources"].append({"url": "source_file", "status": "file", "items": len(items)})
    else:
        items = []
        for url in source_urls:
            text, status, state = fetch_url(url, cache, args)
            source_items = parse_items(text, url) if text else []
            items.extend(source_items)
            row = {
                "url": url,
                "status": status,
                "http_status": state.get("last_status"),
                "items": len(source_items),
                "backoff_until": state.get("backoff_until") or None,
                "last_error": state.get("last_error"),
            }
            summary["sources"].append(row)
            if status == "blocked_by_challenge":
                summary["blocked_sources"] += 1
            if status == "backoff":
                summary["backoff_sources"] += 1
            if status == "error":
                summary["failed"] += 1
                summary["errors"].append(row)

    # De-duplicate source items by normalized title/link.
    deduped = []
    seen_items = set()
    for item in items:
        key = rss.slug_key(item.get("title"), item.get("link"))
        if key and key not in seen_items:
            seen_items.add(key)
            deduped.append(item)
    items = deduped[: max(1, args.source_item_limit)]
    summary["source_items"] = len(items)

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
            summary["skips"].append({"title": item.get("title"), "reason": "already_seen"})
            continue
        match = best_source_match(item, rows, aliases)
        if not match:
            summary["skipped"] += 1
            summary["skips"].append({"title": item.get("title"), "reason": "no_matching_missing_item"})
            if not args.dry_run:
                processed[item_key] = {"ts": now, "decision": "no_matching_missing_item"}
            continue

        row = match["row"]
        series = row["title"]
        issue = row["issue_number"]
        row_context = missing_mod.row_output_context(row)
        source_score = match["score"]
        public_item = source_item(item)
        source_blocker = rss.source_item_quality_blocker(acquire, item, quality_rules)
        if source_blocker:
            reason = rss.quality_block_message(missing_mod, source_blocker)
            rss.record_quality_block_attempt(
                missing_mod,
                row,
                "comicscodes",
                item.get("title"),
                {"title": item.get("title"), "indexer": "ComicsCodes", "categories": item.get("categories") or []},
                source_blocker,
                None,
                quality_rules,
                args.dry_run,
            )
            summary["skipped"] += 1
            summary["skips"].append({
                "series": series,
                "issue": issue,
                "title": item.get("title"),
                "reason": rss.quality_block_status(missing_mod, source_blocker),
                "detail": reason,
                **row_context,
            })
            if not args.dry_run:
                processed[item_key] = {"ts": now, "decision": rss.quality_block_status(missing_mod, source_blocker), "series": series, "issue": issue}
            continue
        if source_score["score"] < 55:
            if source_score["pack"] and source_score["matched_title"] and source_score["has_number"]:
                payload = {
                    "series": series,
                    "issue": issue,
                    "source_item": public_item,
                    "confidence_score": source_score["score"],
                    "why": [*source_score["reasons"], "ComicsCodes broad pack/range requires explicit review"],
                    **row_context,
                }
                summary["candidates_found"] += 1
                summary["sent_to_review"] += 1
                summary["reviews"].append({**payload, "reason": "comicscodes_pack_requires_review"})
                if not args.dry_run:
                    review("comicscodes_pack_requires_review", payload)
                    processed[item_key] = {"ts": now, "decision": "review", "series": series, "issue": issue}
                continue
            summary["skipped"] += 1
            summary["skips"].append({"title": item.get("title"), "reason": "low_signal", **row_context})
            if not args.dry_run:
                processed[item_key] = {"ts": now, "decision": "low_signal"}
            continue
        summary["candidates_found"] += 1

        if auto_by_series.get(series, 0) >= args.max_per_series:
            summary["skipped"] += 1
            summary["skips"].append({"series": series, "issue": issue, "title": item.get("title"), "reason": "per_series_limit", **row_context})
            continue
        if summary["auto_grabbed"] >= args.max_auto:
            summary["skipped"] += 1
            summary["skips"].append({"series": series, "issue": issue, "title": item.get("title"), "reason": "auto_limit", **row_context})
            continue
        if missing_mod.issue_has_pending(series, issue, raw_pending):
            summary["skipped"] += 1
            summary["skips"].append({"series": series, "issue": issue, "title": item.get("title"), "reason": "already_pending", **row_context})
            if not args.dry_run:
                processed[item_key] = {"ts": now, "decision": "already_pending", "series": series, "issue": issue}
            continue
        if not missing_mod.folder_is_safe(row.get("folder")):
            payload = {
                "series": series,
                "issue": issue,
                "source_item": public_item,
                "confidence_score": source_score["score"],
                "why": source_score["reasons"],
                "folder": row.get("folder"),
                **row_context,
            }
            summary["sent_to_review"] += 1
            summary["reviews"].append({**payload, "reason": "comicscodes_unsafe_target_folder"})
            if not args.dry_run:
                review("comicscodes_unsafe_target_folder", payload)
                processed[item_key] = {"ts": now, "decision": "review", "series": series, "issue": issue}
            continue

        try:
            prowlarr = rss.best_prowlarr_result(
                acquire,
                row,
                aliases,
                bad_matches,
                args.limit,
                cache,
                now,
                missing_mod=missing_mod,
                quality_rules=quality_rules,
                source="comicscodes",
                dry_run=args.dry_run,
            )
        except Exception as exc:
            summary["failed"] += 1
            payload = {
                "series": series,
                "issue": issue,
                "source_item": public_item,
                "confidence_score": source_score["score"],
                "why": source_score["reasons"],
                "error": rss.redact_text(exc),
                **row_context,
            }
            summary["errors"].append(payload)
            log("prowlarr_search_error", payload)
            continue

        if not prowlarr:
            payload = {
                "series": series,
                "issue": issue,
                "source_item": public_item,
                "confidence_score": source_score["score"],
                "query": (rss.query_variants(row, aliases.get(series, [])) or [series])[0],
                "why": [*source_score["reasons"], "ComicsCodes matched a missing item but Prowlarr had no usable result"],
                **row_context,
            }
            reason = "comicscodes_high_confidence_no_prowlarr_result" if source_score["score"] >= 85 else "comicscodes_medium_confidence"
            summary["sent_to_review"] += 1
            summary["reviews"].append({**payload, "reason": reason})
            if not args.dry_run:
                review(reason, payload)
                processed[item_key] = {"ts": now, "decision": "review", "series": series, "issue": issue}
            continue

        result = prowlarr["result"]
        result_score = prowlarr["score"]
        reason_seed = "comicscodes_medium_confidence"
        rid = rss.review_id(reason_seed, series, issue, result.get("title"), item.get("title"))
        payload = {
            "series": series,
            "issue": issue,
            "query": prowlarr["query"],
            "source_item": public_item,
            "confidence_score": min(100, round((source_score["score"] * 0.45) + (result_score["score"] * 0.55))),
            "source_confidence_score": source_score["score"],
            "prowlarr_confidence_score": result_score["score"],
            "source_indexer": result.get("indexer"),
            "matched_title": result_score["matched_title"],
            "candidate": rss.raw_review_candidate(result),
            "why": [f"ComicsCodes: {reason}" for reason in source_score["reasons"]] + [f"Prowlarr: {reason}" for reason in result_score["reasons"]],
            **row_context,
        }
        if source_score["pack"] or result_score["pack"]:
            try:
                pack_auto = rss.pack_auto_approve_action(
                    missing_mod,
                    acquire,
                    row,
                    prowlarr["query"],
                    result,
                    "comicscodes_discovery_pack",
                    "source_item",
                    public_item,
                    payload["confidence_score"],
                    row_context,
                    cache,
                    args.dry_run,
                )
            except Exception as exc:
                payload["pack_auto_error"] = rss.redact_text(exc)
                summary["failed"] += 1
                summary["errors"].append({**payload, "candidate": rss.safe_candidate(result)})
                log("comicscodes_pack_auto_approve_error", payload)
                pack_auto = None
            if pack_auto and pack_auto.get("action"):
                action = pack_auto["action"]
                summary["auto_grabbed"] += 1
                auto_by_series[series] = auto_by_series.get(series, 0) + 1
                summary["actions"].append(action)
                log("comicscodes_pack_auto_approved", action)
                if not args.dry_run:
                    processed[item_key] = {"ts": now, "decision": "pack_auto_approved", "series": series, "issue": issue}
                continue
            if pack_auto and pack_auto.get("skip"):
                skip = {
                    "series": series,
                    "issue": issue,
                    "title": item.get("title"),
                    "reason": pack_auto.get("reason") or "pack_auto_waiting",
                    "pack_auto_result": pack_auto.get("auto_result"),
                    **row_context,
                }
                summary["skipped"] += 1
                summary["skips"].append(skip)
                log("comicscodes_pack_auto_skip", skip)
                if not args.dry_run and skip["reason"] == "pack_already_in_flight_or_finished":
                    processed[item_key] = {"ts": now, "decision": skip["reason"], "series": series, "issue": issue}
                continue
            if pack_auto and pack_auto.get("blocked"):
                payload["pack_auto_blocked_reason"] = pack_auto.get("reason")
                payload["pack_auto_result"] = pack_auto.get("auto_result")
        is_high = (
            payload["confidence_score"] >= 90
            and source_score["strict_title"]
            and result_score["strict_title"]
            and not source_score["pack"]
            and not result_score["pack"]
            and rid not in ignored
            and rid not in bad_review_ids
        )
        if is_high:
            try:
                outcome = rss.send(acquire, result, prowlarr["query"], args.dry_run)
                action = {
                    "series": series,
                    "issue": issue,
                    "query": prowlarr["query"],
                    "source_item": public_item,
                    "candidate": rss.safe_candidate(result),
                    "confidence_score": payload["confidence_score"],
                    "outcome": outcome,
                    **row_context,
                }
                summary["auto_grabbed"] += 1
                auto_by_series[series] = auto_by_series.get(series, 0) + 1
                summary["actions"].append(action)
                log("comicscodes_auto_grab", action)
                if not args.dry_run:
                    processed[item_key] = {"ts": now, "decision": "auto_grabbed", "series": series, "issue": issue}
                continue
            except Exception as exc:
                payload["error"] = rss.redact_text(exc)
                summary["failed"] += 1
                summary["errors"].append({**payload, "candidate": rss.safe_candidate(result)})
                log("comicscodes_auto_grab_error", payload)

        reason = "comicscodes_medium_confidence"
        if source_score["pack"] or result_score["pack"]:
            reason = "comicscodes_pack_requires_review"
        elif payload["confidence_score"] >= 85:
            reason = "comicscodes_high_confidence_manual_review"
        summary["sent_to_review"] += 1
        summary["reviews"].append({**payload, "candidate": rss.safe_candidate(result), "reason": reason, "review_id": rss.review_id(reason, series, issue, result.get("title"), item.get("title"))})
        if not args.dry_run:
            review(reason, payload)
            processed[item_key] = {"ts": now, "decision": "review", "series": series, "issue": issue}

    summary["finished_at"] = time.time()
    summary["duration_seconds"] = round(summary["finished_at"] - summary["started_at"], 2)
    if (summary["blocked_sources"] or summary["backoff_sources"]) and not summary["source_items"]:
        summary["status"] = "WATCH"
    if summary["failed"] and summary["status"] == "OK":
        summary["status"] = "WATCH"
    if not args.dry_run:
        write_json(CACHE_FILE, cache)
    safe_status = json.loads(json.dumps(summary))
    for review_row in safe_status.get("reviews", []):
        if isinstance(review_row.get("candidate"), dict):
            review_row["candidate"].pop("downloadUrl", None)
    for error_row in safe_status.get("errors", []):
        if isinstance(error_row, dict) and isinstance(error_row.get("candidate"), dict):
            error_row["candidate"].pop("downloadUrl", None)
    write_json(STATUS_FILE, safe_status)
    log("summary", {
        "mode": summary["mode"],
        "source": summary["source"],
        "source_items": summary["source_items"],
        "candidates_found": summary["candidates_found"],
        "auto_grabbed": summary["auto_grabbed"],
        "sent_to_review": summary["sent_to_review"],
        "blocked_sources": summary["blocked_sources"],
        "failed": summary["failed"],
        "duration_seconds": summary["duration_seconds"],
    })
    print(json.dumps(safe_status, indent=2, sort_keys=True))
    return 0 if not summary["failed"] else 1


def main():
    apply_comicscodes_provider_settings()
    parser = argparse.ArgumentParser(description="Guarded ComicsCodes discovery for InkDrop")
    parser.add_argument("--series", action="append", default=[], help="limit run to InkDrop's watched series set")
    parser.add_argument("--only", action="append", default=[], help="filter processed series by exact title")
    parser.add_argument("--fresh-days", type=float)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--max-auto", type=int, default=DEFAULT_MAX_AUTO)
    parser.add_argument("--max-per-series", type=int, default=DEFAULT_MAX_PER_SERIES)
    parser.add_argument("--source-item-limit", type=int, default=80)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="ignore cache/backoff for this run")
    parser.add_argument("--reprocess-seen", action="store_true", help="re-evaluate cached source items")
    parser.add_argument("--include-lists", action="store_true", help="include All Comics/Manga list pages")
    parser.add_argument("--source-url", action="append", default=[], help="override source URL; may be repeated")
    parser.add_argument("--source-file", help="test with a local HTML/RSS source file instead of live ComicsCodes")
    args = parser.parse_args()
    raise SystemExit(discover(args))


if __name__ == "__main__":
    main()
