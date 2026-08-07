"""Provider-agnostic contracts for InkDrop manual and automatic search.

This module does not perform network I/O. Provider adapters keep ownership of
discovery and acquisition; this layer gives their results one bounded, private
representation that can be consumed by Manual Search and later by automation.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import PurePath
from typing import Any, Iterable

from core import inkdrop_candidate_matching
from core import inkdrop_manga_unit_policy
from core import inkdrop_source_providers as source_providers
from core import inkdrop_sources


CONTRACT_VERSION = 1
PROTOCOLS = {"torrent", "usenet", "soulseek", "direct", "page_source", "local"}
UNIT_TYPES = {"issue", "chapter", "volume", "episode", "collected_edition"}
PACK_TYPES = {
    "single_issue_chapter",
    "issue_range",
    "volume_pack",
    "complete_series_pack",
    "weekly_pack",
    "publisher_dump",
    "omnibus_collected_edition",
    "unknown_archive",
}
CONFIDENCE_TIERS = {"exact", "high", "medium", "low", "rejected", "unknown"}
ACQUISITION_CAPABILITIES = {"automatic", "assisted", "manual", "unavailable"}

_SPACE_RE = re.compile(r"\s+")
_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|apikey|passkey|password|passwd|token|cookie|authorization|session)"
)
_URL_RE = re.compile(r"(?i)\b(?:https?|ftp|file)://[^\s<>\"']+")
_IP_RE = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
_PATH_RE = re.compile(r"(?i)(?:\b[a-z]:\\[^\s,;]+|(?<![:\w])\/(?:[^/\s]+/)+[^\s,;]+)")
_YEAR_RE = re.compile(r"(?<!\d)((?:18|19|20)\d{2})(?!\d)")
_RANGE_RE = re.compile(
    r"(?i)\b(?:issues?|chapters?|ch\.?|volumes?|vols?\.?|v)?\s*#?0*(\d+(?:\.\d+)?)\s*[-–]\s*#?0*(\d+(?:\.\d+)?)\b"
)
_ISSUE_RE = re.compile(r"(?i)(?:\bissue\s*|(?<!\w)#)0*(\d+(?:\.\d+)?)\b")
_CHAPTER_RE = re.compile(r"(?i)\b(?:chapter|ch)\.?\s*0*(\d+(?:\.\d+)?)\b")
_VOLUME_RE = re.compile(r"(?i)\b(?:volume|vol|v)\.?\s*0*(\d+(?:\.\d+)?)\b")
_EPISODE_RE = re.compile(r"(?i)\b(?:episode|ep)\.?\s*0*(\d+(?:\.\d+)?)\b")
_TRUSTED_VOLUME_TITLE_RE = re.compile(
    r"(?i)^(?:band|tome|tomo|vol(?:ume)?|book|hc|tpb)\.?\s*#?0*(\d+(?:\.\d+)?)$"
)
_TRUSTED_VOLUME_TITLE_SUFFIX_RE = re.compile(
    r"(?i)^#?0*(\d+(?:\.\d+)?)\s*(?:band|tome|tomo|vol(?:ume)?|book|hc|tpb)$"
)
_TRUSTED_UNNUMBERED_VOLUME_TITLE_RE = re.compile(r"(?i)^(?:hc|tpb)$")
_TRUSTED_NUMBER_WORDS = {
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
_TRUSTED_NUMBER_WORD_TOKEN = "|".join(_TRUSTED_NUMBER_WORDS)
_TRUSTED_VOLUME_WORD_TITLE_RE = re.compile(
    rf"(?i)^(?:band|tome|tomo|vol(?:ume)?|book|hc|tpb)\.?\s*({_TRUSTED_NUMBER_WORD_TOKEN})$"
)
_TRUSTED_VOLUME_WORD_TITLE_SUFFIX_RE = re.compile(
    rf"(?i)^({_TRUSTED_NUMBER_WORD_TOKEN})\s*(?:band|tome|tomo|vol(?:ume)?|book|hc|tpb)$"
)
_ARCHIVE_EXTENSIONS = {".cbz", ".cbr", ".zip", ".rar", ".7z", ".tar", ".gz"}

REJECTION_EXPLANATIONS = {
    "candidate_not_safe": "The existing provider safety verdict did not accept this candidate.",
    "language_rejected": "The candidate language is outside the series or source language policy.",
    "title_mismatch": "The release title does not safely match the requested work.",
    "unit_mismatch": "The release unit does not safely match the requested issue, chapter, or volume.",
    "provider_unavailable": "The provider or child source was unavailable for this search.",
    "provider_timeout": "The provider call timed out; this is not a zero-result response.",
    "manual_interaction_required": "The source identified a release but cannot complete acquisition automatically.",
    "unsupported_protocol": "No supported acquisition protocol was preserved for this candidate.",
    "unsafe_locator": "The candidate locator did not pass the existing URL or path safety gate.",
    "duplicate_candidate": "Another result normalized to the same stable candidate identity.",
    "unknown_match_confidence": "The provider did not supply enough evidence for automatic acceptance.",
}


def _text(value: Any) -> str:
    return _SPACE_RE.sub(" ", str(value or "").strip())


def _first(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def _list(value: Any) -> list[str]:
    if isinstance(value, str):
        values = re.split(r"[|;\n]", value)
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    elif value in (None, ""):
        values = []
    else:
        values = [value]
    out: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = _text(item)
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            out.append(text)
    return out


def _int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        parsed = float(value) if value not in (None, "") else None
        return parsed if parsed is None or math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def _hash(*parts: Any) -> str:
    payload = "\x1f".join(_text(part) for part in parts)
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()


def _timestamp(value: Any = None) -> str:
    if value:
        text = _text(value)
        if text:
            return text
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _redacted_text(value: Any, limit: int = 500) -> str:
    text = _URL_RE.sub("<redacted-url>", _text(value))
    text = _IP_RE.sub("<redacted-ip>", text)
    text = _PATH_RE.sub("<redacted-path>", text)
    text = re.sub(
        r"(?i)(api[_-]?key|apikey|passkey|password|passwd|token|cookie|authorization|session)\s*[:=]\s*[^\s,;&]+",
        r"\1=<redacted>",
        text,
    )
    return text[: max(0, int(limit or 0))]


def redacted_text(value: Any, limit: int = 500) -> str:
    """Public redactor for persisted/API-visible Manual Search diagnostics."""

    return _redacted_text(value, limit=limit)


def safe_health_snapshot(value: dict[str, Any] | None) -> dict[str, Any]:
    """Strict scalar allowlist for provider health persistence and APIs."""

    value = value if isinstance(value, dict) else {}
    allowed = (
        "status", "healthy", "last_successful_search", "last_successful_fetch",
        "last_successful_chapter_fetch", "last_successful_page_fetch", "current_error",
        "cooldown_until", "configured", "enabled", "version", "update_available",
        "webview_required", "page_download_capable",
    )
    out: dict[str, Any] = {}
    for key in allowed:
        item = value.get(key)
        if item in (None, "") or isinstance(item, (dict, list, tuple, set)):
            continue
        out[key] = item if isinstance(item, (bool, int, float)) else _redacted_text(item, 160)
    return out


def safe_public_structure(value: Any, *, depth: int = 0) -> Any:
    """Bound a small structured evidence value while removing locators/secrets."""

    if depth >= 4:
        return "<bounded>"
    if isinstance(value, dict):
        out = {}
        for raw_key in list(value)[:40]:
            key = _text(raw_key)[:80]
            if _SECRET_RE.search(key) or re.search(r"(?i)(?:url|uri|path|headers?|request|locator)", key):
                continue
            out[key] = safe_public_structure(value.get(raw_key), depth=depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        return [safe_public_structure(item, depth=depth + 1) for item in list(value)[:40]]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return _redacted_text(value, 240)


def _normalized_unit_type(value: Any, media_type: str = "") -> str:
    key = _text(value).lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "collected": "collected_edition",
        "collection": "collected_edition",
        "omnibus": "collected_edition",
        "book": "volume" if media_type in {"manga", "manhwa", "manhua"} else "issue",
        "manga_volume": "volume",
        "book_volume": "volume",
    }
    key = aliases.get(key, key)
    return key if key in UNIT_TYPES else ("chapter" if media_type == "webtoon" else "issue")


def _unit_number_key(value: Any) -> str:
    text = _text(value).lower()
    if text in _TRUSTED_NUMBER_WORDS:
        return str(_TRUSTED_NUMBER_WORDS[text])
    match = re.fullmatch(r"0*(\d+)(?:\.(\d+))?", text)
    if not match:
        return ""
    whole = str(int(match.group(1) or "0"))
    decimal = (match.group(2) or "").rstrip("0")
    return f"{whole}.{decimal}" if decimal else whole


MANGA_VOLUME_METADATA_PROVIDERS = {"comicvine", "kapowarr"}
MANGA_CHAPTER_METADATA_PROVIDERS = {"mangadex", "suwayomi", "tachiyomi"}


def trusted_target_unit_identity(value: dict[str, Any] | None) -> dict[str, str]:
    """Infer a target unit only from explicit or trusted metadata provenance.

    Release titles are deliberately excluded. This contract is for target
    identity supplied by the metadata-owned issue row, so an arbitrary result
    cannot promote itself from an issue into a collected volume.
    """

    value = value if isinstance(value, dict) else {}
    explicit = _text(_first(value.get("unit_type"), value.get("unitType"))).lower()
    metadata_trusted = value.get("target_unit_metadata_trusted") is True
    media_type = _text(_first(value.get("media_type"), value.get("mediaType"))).lower()
    unit_number = _unit_number_key(
        _first(value.get("unit_number"), value.get("issue_number"), value.get("normalized_number"))
    )
    issue_provider = _text(
        _first(value.get("issue_metadata_provider"), value.get("issueMetadataProvider"))
    ).lower()
    series_provider = _text(
        _first(
            value.get("series_metadata_provider"),
            value.get("seriesMetadataProvider"),
            value.get("metadata_provider"),
            value.get("metadataProvider"),
        )
    ).lower()
    provider_bound_identity = metadata_trusted and bool(issue_provider or series_provider)
    if explicit and not provider_bound_identity:
        return {
            "unit_type": _normalized_unit_type(explicit, media_type),
            "source": "explicit_unit_type",
            "volume_number": "",
        }
    if not metadata_trusted:
        return {"unit_type": "", "source": "", "volume_number": ""}
    if media_type in {"manga", "manhwa", "manhua", "webtoon"} and unit_number:
        # A chapter-native issue provider outranks the parent series provider.
        # This is important for MangaDex chapters attached to a ComicVine-backed
        # series: they must remain chapters and may never relabel themselves as
        # collected volumes.
        if issue_provider in MANGA_CHAPTER_METADATA_PROVIDERS:
            return {
                "unit_type": "chapter",
                "source": "trusted_chapter_metadata_provider",
                "volume_number": "",
            }
        # A user-set per-series override (Settings > Series, stored in
        # series.raw_json.manga_unit_model_override) outranks the blanket
        # ComicVine/Kapowarr-is-volume-managed assumption below. That
        # assumption is right for most manga series backed by those
        # providers, but not all of them (the Climber bug: a series that's
        # actually chapter-native but happens to be ComicVine-backed got
        # forced into "volume" and matched the wrong unit type every time).
        # Series with no override keep exactly the prior default behavior.
        series_override = str(value.get("series_manga_unit_override") or "").strip()
        if series_override:
            if inkdrop_manga_unit_policy.manga_unit_model_prefers_chapter(series_override):
                return {
                    "unit_type": "chapter",
                    "source": "series_manga_unit_override",
                    "volume_number": "",
                }
            if inkdrop_manga_unit_policy.manga_unit_model_prefers_volume(series_override):
                return {
                    "unit_type": "volume",
                    "source": "series_manga_unit_override",
                    "volume_number": unit_number,
                }
        # ComicVine/Kapowarr issue rows for a manga publication are the
        # volume-managed release units.  Their issue titles are often subtitles
        # (for example, "Founding"), so requiring the word "Volume" loses the
        # authoritative unit identity and causes exact vNN releases to be
        # rejected.  Provider provenance + manga classification + a canonical
        # issue number is the trusted boundary; result titles cannot trigger it.
        if (issue_provider or series_provider) in MANGA_VOLUME_METADATA_PROVIDERS:
            return {
                "unit_type": "volume",
                "source": "trusted_manga_volume_metadata_provider",
                "volume_number": unit_number,
            }
    # Provider-bound durable rows may not be relabeled by raw volume/chapter
    # aliases.  Provider-less trusted metadata may still use explicit volume
    # evidence for compatibility with manually curated records.
    trusted_volume = "" if provider_bound_identity else _unit_number_key(_first(value.get("volume_number"), value.get("volume")))
    if unit_number and trusted_volume and unit_number == trusted_volume:
        return {
            "unit_type": "volume",
            "source": "trusted_volume_number",
            "volume_number": trusted_volume,
        }
    title = _text(_first(value.get("trusted_unit_title"), value.get("metadata_unit_title")))
    match = (
        _TRUSTED_VOLUME_TITLE_RE.fullmatch(title)
        or _TRUSTED_VOLUME_TITLE_SUFFIX_RE.fullmatch(title)
        or _TRUSTED_VOLUME_WORD_TITLE_RE.fullmatch(title)
        or _TRUSTED_VOLUME_WORD_TITLE_SUFFIX_RE.fullmatch(title)
    )
    title_number = _unit_number_key(match.group(1)) if match else ""
    if unit_number and title_number == unit_number:
        return {
            "unit_type": "volume",
            "source": "trusted_volume_title",
            "volume_number": unit_number,
        }
    if unit_number and _TRUSTED_UNNUMBERED_VOLUME_TITLE_RE.fullmatch(title):
        return {
            "unit_type": "volume",
            "source": "trusted_volume_format_title",
            "volume_number": unit_number,
        }
    return {"unit_type": "", "source": "", "volume_number": ""}


def collected_title_aliases(value: Any) -> list[str]:
    """Derive narrow aliases when an edition qualifier precedes a colon.

    The colon is required evidence that the left side is a qualified work
    identity rather than ordinary title prose.  At least one meaningful token
    must remain on the left and the subtitle must remain distinctive, so this
    never degrades to a broad franchise-only search.
    """

    return inkdrop_sources.collected_title_aliases(value)


def structured_search_input(value: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize series/wanted metadata before any provider builds a query."""

    value = value if isinstance(value, dict) else {}
    media_type = _text(_first(value.get("media_type"), value.get("mediaType"))).lower()
    canonical = _text(
        _first(
            value.get("canonical_work_title"),
            value.get("series_title"),
            value.get("series"),
            value.get("manga_title"),
            value.get("title"),
        )
    )
    publication = _text(
        _first(value.get("publication_title"), value.get("edition_title"), value.get("publication"))
    )
    unit_number = _text(
        _first(
            value.get("unit_number"),
            value.get("issue_number"),
            value.get("chapter_number"),
            value.get("episode_number"),
            value.get("normalized_number"),
        )
    )
    volume_number = _text(_first(value.get("volume_number"), value.get("volume")))
    target_identity = trusted_target_unit_identity(value)
    unit_type = _normalized_unit_type(target_identity.get("unit_type"), media_type)
    if unit_type == "volume" and not volume_number:
        volume_number = target_identity.get("volume_number") or unit_number
    aliases = _list(_first(value.get("aliases"), value.get("query_aliases"), value.get("series_query_aliases")))
    aliases.extend(collected_title_aliases(canonical))
    aliases.extend(inkdrop_sources.contributor_title_aliases(canonical))
    aliases.extend(inkdrop_sources.contributor_title_aliases(publication))
    aliases = [alias for alias in aliases if alias.casefold() not in {canonical.casefold(), publication.casefold()}]
    return {
        "search_input_contract_version": CONTRACT_VERSION,
        "canonical_work_title": canonical,
        "publication_title": publication,
        "edition_id": _text(_first(value.get("edition_id"), value.get("publication_id"))),
        "edition_marker": _text(_first(value.get("edition_marker"), value.get("edition_type"))).lower(),
        "aliases": aliases[:12],
        "creators": _list(_first(value.get("creators"), value.get("creator"), value.get("authors")))[:12],
        "publisher": _text(_first(value.get("publisher"), value.get("series_publisher"), value.get("watch_publisher"))),
        "publication_year": _int(_first(value.get("publication_year"), value.get("year"), value.get("start_year"))),
        "language": _text(_first(value.get("language"), value.get("preferred_language"), "en")).lower(),
        "media_type": media_type,
        "unit_type": unit_type,
        "unit_type_source": target_identity.get("source") or "media_default",
        "unit_number": unit_number,
        "volume_number": volume_number,
        "trusted_unit_title": _text(value.get("trusted_unit_title")),
        "preferred_format": _text(_first(value.get("preferred_format"), value.get("format"))).lower(),
        "pack_allowed": bool(value.get("pack_allowed", value.get("allow_pack", False))),
        "media_type_source_profile": _text(
            _first(value.get("media_type_source_profile"), value.get("source_profile"))
        ),
        "series_source_override": _list(
            _first(value.get("series_source_override"), value.get("source_override"))
        )[:12],
        # These fields are produced only by the durable ComicVine singleton
        # proof used by automatic acquisition. Preserving them here lets
        # Manual Search use the same exact-title collected-book boundary.
        "canonical_issue_count": _int(value.get("canonical_issue_count")) or 0,
        "metadata_issue_count": _int(value.get("metadata_issue_count")) or 0,
        "singleton_metadata_trusted": value.get("singleton_metadata_trusted") is True,
        "singleton_metadata_fresh": value.get("singleton_metadata_fresh") is True,
        "singleton_issue_metadata_trusted": value.get("singleton_issue_metadata_trusted") is True,
        "singleton_issue_proof": value.get("singleton_issue_proof") is True,
        "singleton_issue_proof_source": _text(value.get("singleton_issue_proof_source")),
        "collected_singleton_wanted_count": _int(value.get("collected_singleton_wanted_count")) or 0,
        "collected_singleton_markers": _list(value.get("collected_singleton_markers"))[:12],
        "collected_singleton_title_aliases": _list(value.get("collected_singleton_title_aliases"))[:12],
        "collected_singleton_proof": value.get("collected_singleton_proof") is True,
        "collected_singleton_proof_source": _text(value.get("collected_singleton_proof_source")),
    }


def build_query_variants(
    search_input: dict[str, Any] | None,
    *,
    provider_id: str = "",
    max_queries: int = 6,
) -> list[dict[str, Any]]:
    """Build bounded, attributed queries from structured metadata."""

    context = structured_search_input(search_input)
    max_queries = max(1, min(int(max_queries or 1), 12))
    unit_type = context["unit_type"]
    unit_number = context["unit_number"]
    volume_number = context["volume_number"]
    year = context["publication_year"]
    stems: list[tuple[str, str]] = []
    if context["publication_title"]:
        stems.append((context["publication_title"], "publication"))
    if context["canonical_work_title"]:
        stems.append((context["canonical_work_title"], "canonical"))
    stems.extend((alias, "alias") for alias in context["aliases"])

    variants: list[dict[str, Any]] = []
    seen: set[str] = set()
    provider_key = _text(provider_id).lower()
    fallback_candidates = context["aliases"]
    discovery_fallback = min(
        (stem for stem in fallback_candidates if _text(stem)),
        key=lambda stem: (len(_text(stem).split()), len(_text(stem))),
        default="",
    ) if provider_key in {"prowlarr", "slskd"} and unit_number and max_queries >= 3 else ""
    fallback_added = False

    def add(query: str, kind: str, stem: str) -> None:
        query = _text(query)
        key = query.casefold()
        if query and key not in seen and len(variants) < max_queries:
            seen.add(key)
            variants.append(
                {
                    "query": query,
                    "query_kind": kind,
                    "source_title": stem,
                    "provider_id": _text(provider_id).lower(),
                    "ordinal": len(variants),
                }
            )

    if provider_key == "slskd":
        if discovery_fallback:
            add(discovery_fallback, "discovery_series_fallback", discovery_fallback)
            fallback_added = True
        elif context["canonical_work_title"]:
            discovery_title = context["canonical_work_title"]
            is_comic = context["media_type"] == "comic"
            media_label = (
                "manga"
                if context["media_type"] in {"manga", "manhwa", "manhua", "webtoon"}
                else "comics" if is_comic else ""
            )
            # Comic scene releases commonly carry a "Comics" tag in their own
            # title/folder name, so the media-suffixed query leads for comics
            # (proven live on SLSKD). Manga releases rarely carry "Manga" in
            # their own name, so the plain series title leads there instead,
            # with the media-suffixed form kept as a fallback query.
            if media_label and is_comic:
                add(f"{discovery_title} {media_label}", "canonical_media_discovery", discovery_title)
            add(discovery_title, "canonical_series_discovery", discovery_title)
            if media_label and not is_comic:
                add(f"{discovery_title} {media_label}", "canonical_media_discovery", discovery_title)

    # Lead with unit-bearing queries across the useful title identities. This
    # keeps a verbose publication title from consuming the entire query budget.
    for stem, stem_kind in stems:
        if volume_number:
            add(f"{stem} Vol {volume_number}", f"{stem_kind}_volume", stem)
        if unit_number:
            marker = {"chapter": "Chapter", "episode": "Episode", "volume": "Vol"}.get(unit_type, "")
            add(f"{stem} {marker} {unit_number}" if marker else f"{stem} {unit_number}", f"{stem_kind}_unit", stem)
        if discovery_fallback and not fallback_added and variants:
            add(discovery_fallback, "discovery_series_fallback", discovery_fallback)
            fallback_added = True

    # Creator-prefixed queries are valuable for ambiguous western titles, but
    # remain bounded to the first two creators and the canonical work title.
    canonical = context["canonical_work_title"]
    for creator in context["creators"][:2]:
        if not canonical or creator.casefold() in canonical.casefold():
            continue
        if unit_number:
            marker = {"chapter": "Chapter", "episode": "Episode", "volume": "Vol"}.get(unit_type, "")
            suffix = f"{marker} {unit_number}" if marker else unit_number
            add(f"{creator} {canonical} {suffix}", "creator_unit", canonical)
        else:
            add(f"{creator} {canonical}", "creator_series", canonical)

    for stem, stem_kind in stems:
        if year:
            add(f"{stem} {year}", f"{stem_kind}_year", stem)
        add(stem, f"{stem_kind}_series", stem)
        if len(variants) >= max_queries:
            break
    return variants


def classify_pack(title: Any, *, extension: Any = "", raw: dict[str, Any] | None = None) -> dict[str, Any]:
    text = _text(title)
    lowered = text.lower()
    raw = raw if isinstance(raw, dict) else {}
    pack_type = "single_issue_chapter"
    range_match = _RANGE_RE.search(text)
    likely_members = _int(_first(raw.get("estimated_pack_members"), raw.get("pack_member_count")))
    if re.search(r"\bweekly(?:\s+comics?)?\s+pack\b|\bweek\s+\d{1,2}\b", lowered):
        pack_type = "weekly_pack"
    elif re.search(r"\b(?:publisher|marvel|dc|image|dark horse|boom)\s+(?:dump|bundle)\b", lowered):
        pack_type = "publisher_dump"
    elif re.search(r"\b(?:omnibus|compendium|collected edition|absolute edition|deluxe edition)\b", lowered):
        pack_type = "omnibus_collected_edition"
    elif re.search(r"\b(?:complete|full)\s+(?:series|collection|run)\b|\bcomplete\b", lowered):
        pack_type = "complete_series_pack"
    elif range_match:
        pack_type = "volume_pack" if re.search(r"(?i)\b(?:volumes?|vols?\.?|v)\b", text) else "issue_range"
        if likely_members is None:
            try:
                likely_members = max(1, int(float(range_match.group(2))) - int(float(range_match.group(1))) + 1)
            except (TypeError, ValueError):
                pass
    elif re.search(r"(?i)\bvolumes?\s+\d+(?:\s*[,/&+]\s*\d+)+", text):
        pack_type = "volume_pack"
    elif bool(raw.get("pack")) or bool(raw.get("is_pack")):
        pack_type = "unknown_archive"
    elif source_providers.normalize_extension(extension or text) in _ARCHIVE_EXTENSIONS and re.search(
        r"(?i)\b(?:pack|bundle|collection|archive)\b", text
    ):
        pack_type = "unknown_archive"
    return {
        "pack_candidate": pack_type != "single_issue_chapter",
        "pack_type": pack_type,
        "estimated_pack_members": likely_members,
        "range_start": range_match.group(1) if range_match else "",
        "range_end": range_match.group(2) if range_match else "",
        "manifest_evidence_available": bool(
            _first(raw.get("pack_detail_entries"), raw.get("files"), raw.get("manifest"), raw.get("nfo"))
        ),
    }


def _pack_evidence(candidate: dict[str, Any], raw: dict[str, Any]) -> tuple[bool, bool]:
    containers = [candidate, raw]
    if isinstance(raw.get("result"), dict):
        containers.append(raw["result"])
    explicit = any(bool(row.get("pack") or row.get("is_pack") or row.get("pack_candidate")) for row in containers)
    manifest_keys = (
        "files", "pack_detail_entries", "manifest", "nfo",
        "pack_contents_match", "pack_contents_matching_entry", "pack_contents_entry_count",
        "pack_contents_coverage_source", "pack_manifest_match", "pack_manifest_entry",
        "pack_manifest_entry_count", "pack_manifest_coverage_source", "manifest_matching_entry",
        "manifest_entry_count", "manifest_coverage_source",
    )
    manifest = any(
        row.get(key) not in (None, "", [], {})
        for row in containers
        for key in manifest_keys
    )
    return explicit, manifest


def _requested_number_in_title(title: str, requested: Any) -> str:
    requested_text = _text(requested)
    if not requested_text or not re.fullmatch(r"\d+(?:\.\d+)?", requested_text):
        return ""
    normalized = requested_text.lstrip("0") or "0"
    pattern = re.compile(rf"(?<!\d)0*{re.escape(normalized)}(?!\d)", re.IGNORECASE)
    return requested_text if pattern.search(title) else ""


def interpret_title(
    title: Any,
    context: dict[str, Any] | None = None,
    candidate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    title = _text(title)
    context = structured_search_input(context)
    candidate = candidate if isinstance(candidate, dict) else {}
    issue = _ISSUE_RE.search(title)
    chapter = _CHAPTER_RE.search(title)
    volume = _VOLUME_RE.search(title)
    episode = _EPISODE_RE.search(title)
    year = _YEAR_RE.search(title)
    interpreted_type = context["unit_type"]
    interpreted_number = ""
    candidate_unit_type = _normalized_unit_type(candidate.get("unit_type"), context.get("media_type", ""))
    explicit_numbers = (
        ("chapter", _text(_first(candidate.get("chapter_number"), candidate.get("chapter")))),
        ("issue", _text(_first(candidate.get("issue_number"), candidate.get("issue")))),
        ("episode", _text(_first(candidate.get("episode_number"), candidate.get("episode")))),
        ("volume", _text(_first(candidate.get("volume_number"), candidate.get("volume")))),
    )
    explicit_type, explicit_number = next(
        (
            (kind, number)
            for kind, number in explicit_numbers
            if number and kind in {candidate_unit_type, context["unit_type"]}
        ),
        ("", ""),
    )
    if explicit_number:
        interpreted_type, interpreted_number = explicit_type, explicit_number
    elif chapter:
        interpreted_type, interpreted_number = "chapter", chapter.group(1)
    elif issue:
        interpreted_type, interpreted_number = "issue", issue.group(1)
    elif episode:
        interpreted_type, interpreted_number = "episode", episode.group(1)
    elif volume:
        interpreted_type, interpreted_number = "volume", volume.group(1)
    elif _requested_number_in_title(title, context["unit_number"]):
        # A bare number is only interpreted when it exactly corroborates the
        # requested structured unit. It is never used as a generic matcher.
        interpreted_number = context["unit_number"]
    interpreted_volume = _text(_first(candidate.get("volume_number"), candidate.get("volume")))
    if not interpreted_volume and volume:
        interpreted_volume = volume.group(1)
    return {
        "interpreted_work": context["canonical_work_title"],
        "interpreted_publication": context["publication_title"],
        "interpreted_unit_type": interpreted_type,
        "interpreted_unit_number": interpreted_number,
        "interpreted_volume": interpreted_volume,
        "year": _int(year.group(1)) if year else None,
    }


def _age_seconds(candidate: dict[str, Any], provider_id: str) -> int | None:
    exact = _int(_first(candidate.get("age_seconds"), candidate.get("ageSeconds")))
    if exact is not None:
        return max(0, exact)
    minutes = _int(_first(candidate.get("age_minutes"), candidate.get("ageMinutes")))
    if minutes is not None:
        return max(0, minutes) * 60
    hours = _int(_first(candidate.get("age_hours"), candidate.get("ageHours")))
    if hours is not None:
        return max(0, hours) * 3600
    days = _int(_first(candidate.get("age_days"), candidate.get("ageDays")))
    if days is not None:
        return max(0, days) * 86400
    # Prowlarr's legacy `age` field is days. Other providers keep an unknown
    # age rather than guessing the unit of an unqualified number.
    legacy = _int(candidate.get("age"))
    if provider_id == "prowlarr" and legacy is not None:
        return max(0, legacy) * 86400
    return None


def provider_display_name(provider_id: Any, candidate: dict[str, Any] | None = None) -> str:
    candidate = candidate if isinstance(candidate, dict) else {}
    explicit = _text(_first(candidate.get("provider_display_name"), candidate.get("provider_name")))
    if explicit:
        return explicit
    key = _text(provider_id).lower()
    return {
        "prowlarr": "Prowlarr",
        "slskd": "SLSKD",
        "suwayomi": "Suwayomi",
        "mangadex": "MangaDex",
        "rss": "RSS",
        "rss_getcomics": "GetComics",
        "getcomics": "GetComics",
        "generic_http": "HTTP",
        "local_manual_inbox": "Manual Inbox",
    }.get(key, _text(provider_id).replace("_", " ").title())


def child_source(candidate: dict[str, Any] | None) -> tuple[str, str]:
    candidate = candidate if isinstance(candidate, dict) else {}
    child_id = _text(
        _first(
            candidate.get("child_source_id"),
            candidate.get("indexer_id"),
            candidate.get("indexerId"),
            candidate.get("suwayomi_source_id"),
            candidate.get("source_id"),
            candidate.get("extension_id"),
        )
    )
    child_name = _text(
        _first(
            candidate.get("child_source_name"),
            candidate.get("indexer"),
            candidate.get("indexer_name"),
            candidate.get("suwayomi_source_name"),
            candidate.get("source_name"),
            candidate.get("extension_name"),
            candidate.get("source_site"),
        )
    )
    if not child_id and _text(candidate.get("provider_id")).lower() == "mangadex":
        child_id = "mangadex"
    if not child_name and _text(candidate.get("provider_id")).lower() == "mangadex":
        child_name = "MangaDex API"
    return child_id, child_name


def _protocol(provider_id: str, candidate: dict[str, Any]) -> str:
    raw_value = _text(
        _first(candidate.get("protocol"), candidate.get("download_protocol"), candidate.get("downloadProtocol"))
    ).lower()
    aliases = {"http": "direct", "https": "direct", "nzb": "usenet", "magnet": "torrent"}
    value = aliases.get(raw_value, raw_value)
    if value not in PROTOCOLS:
        value = source_providers.normalize_protocol(raw_value)
        value = aliases.get(value, value)
    if value in PROTOCOLS:
        return value
    kind = _text(_first(candidate.get("source_kind"), candidate.get("provider_type"))).lower()
    provider_id = provider_id.lower()
    if provider_id == "slskd" or "soulseek" in kind:
        return "soulseek"
    if provider_id in {"suwayomi", "mangadex"} or "page" in kind:
        return "page_source"
    if provider_id == "local_manual_inbox" or "manual_inbox" in kind:
        return "local"
    if _first(candidate.get("download_url_hash"), candidate.get("direct_artifact_key")):
        return "direct"
    return ""


def _basename(value: Any) -> str:
    text = _text(value).replace("\\", "/")
    if not text:
        return ""
    try:
        return PurePath(text).name
    except (TypeError, ValueError):
        return text.rsplit("/", 1)[-1]


def _safe_raw_reference(candidate: dict[str, Any], *, max_keys: int = 24) -> dict[str, Any]:
    """Return bounded evidence labels and hashes, never reusable locators."""

    raw = candidate.get("raw") if isinstance(candidate.get("raw"), dict) else {}
    raw_result = raw.get("result") if isinstance(raw.get("result"), dict) else raw
    keys = []
    for key in sorted(str(item) for item in raw_result.keys()):
        if not _SECRET_RE.search(key):
            keys.append(key)
        if len(keys) >= max_keys:
            break
    source_locator = _first(
        candidate.get("guid"),
        candidate.get("info_hash"),
        candidate.get("download_url_hash"),
        candidate.get("source_url_hash"),
        candidate.get("candidate_identity"),
    )
    path = _first(candidate.get("filename"), candidate.get("file_name"), candidate.get("path"), candidate.get("output_path"))
    return {
        "schema": "inkdrop.bounded_raw_evidence.v1",
        "available_keys": keys,
        "locator_hash": _hash(source_locator) if source_locator else "",
        "filename": _basename(path),
        "path_hash": _hash(path) if path else "",
        "raw_size_bytes": len(json.dumps(raw_result, sort_keys=True, default=str).encode("utf-8")),
    }


def _rejection_codes(candidate: dict[str, Any], protocol: str) -> list[str]:
    values: list[str] = []
    for source in (
        candidate.get("rejection_codes"),
        candidate.get("block_reasons"),
        candidate.get("review_reasons"),
        candidate.get("rejection_reasons"),
    ):
        values.extend(_list(source))
    for key in (
        "preview_or_sample", "known_bad_candidate", "manual_source_bad_candidate",
        "source_memory_bad_candidate", "already_downloading", "already_imported",
        "duplicate_active", "known_malicious", "malicious", "invalid_archive",
        "missing_credential", "unsupported_client", "virus", "malware", "corrupt",
        "forbidden", "unauthorized", "access_denied", "access_required",
        "auth_required", "authentication_required", "credential_required",
        "duplicate", "already_present", "unsafe_locator", "archive_invalid",
        "bad_archive", "unsupported_archive", "preview_not_importable", "source_memory_bad",
    ):
        if candidate.get(key):
            values.append(key)
    verdict = _text(_first(candidate.get("auto_grab_verdict"), candidate.get("verdict"))).lower()
    confidence = _text(_first(candidate.get("match_confidence"), candidate.get("confidence_tier"))).lower()
    language_status = _text(candidate.get("language_status")).lower()
    if language_status in {"blocked", "rejected", "mismatch", "not_allowed"}:
        values.append("language_rejected")
    if confidence in {"mismatch", "rejected", "wrong_title"}:
        values.append("title_mismatch")
    if verdict in {"blocked", "reject", "rejected"} and not values:
        values.append("candidate_not_safe")
    if protocol not in PROTOCOLS:
        values.append("unsupported_protocol")
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        safe_value = _redacted_text(value, 160)
        code = re.sub(r"[^a-z0-9_]+", "_", safe_value.strip().lower()).strip("_")
        if code and code not in seen:
            seen.add(code)
            out.append(code)
    return out


def _confidence(candidate: dict[str, Any], rejected: bool) -> str:
    if rejected:
        return "rejected"
    raw = _text(_first(candidate.get("confidence_tier"), candidate.get("match_confidence"))).lower()
    mapping = {
        "exact_match": "exact",
        "title_issue_match": "high",
        "title_chapter_match": "high",
        "title_volume_match": "high",
        "title_match": "high",
        "candidate": "medium",
        "series_title_only": "low",
        "unknown": "unknown",
    }
    value = mapping.get(raw, raw)
    return value if value in CONFIDENCE_TIERS else "unknown"


def _canonical_pack_target_safe(candidate: dict[str, Any], context: dict[str, Any]) -> bool:
    canonical = _text(context.get("canonical_work_title"))
    unit_type = _text(context.get("unit_type")).lower()
    unit_number = _text(context.get("unit_number"))
    volume_number = _text(context.get("volume_number")) or (unit_number if unit_type == "volume" else "")
    if not canonical or unit_type not in {"issue", "chapter", "volume"}:
        return False
    manifest_probe = dict(candidate)
    manifest_probe["series_title"] = canonical
    manifest_probe["series"] = canonical
    manifest_probe["unit_type"] = unit_type
    for key in ("issue", "issue_number", "chapter", "chapter_number", "volume", "volume_number"):
        manifest_probe.pop(key, None)
    if unit_type == "issue":
        manifest_probe["issue_number"] = unit_number
    elif unit_type == "chapter":
        manifest_probe["chapter_number"] = unit_number
    else:
        manifest_probe["volume_number"] = volume_number
    manifest = source_providers.indexer_manifest_pack_match(manifest_probe)
    if not manifest:
        return False
    member_entry = manifest.get("file_entry") or manifest.get("entry")
    member_evidence = inkdrop_candidate_matching.parse_release_title(member_entry)
    if member_evidence.get("preview_or_sample") or member_evidence.get("edition_marker") in inkdrop_candidate_matching.COLLECTED_MARKERS:
        return False
    outer_evidence = inkdrop_candidate_matching.parse_release_title(
        _first(candidate.get("original_result_title"), candidate.get("title"))
    )

    target_year = _text(context.get("publication_year"))
    # A pack title's parsed year can be the beginning of its publication span;
    # the exact manifest member, not that outer range, owns the target year.
    declared_years = {
        _text(value)
        for value in (
            candidate.get("year"), candidate.get("publication_year"),
            member_evidence.get("year"),
        )
        if _text(value)
    }
    if target_year and (member_evidence.get("year") != target_year or any(value != target_year for value in declared_years)):
        return False
    target_publisher = source_providers.normalized_query(context.get("publisher"))
    declared_publishers = {
        source_providers.normalized_query(value)
        for value in (candidate.get("publisher"), candidate.get("source_publisher"), candidate.get("provider_publisher"))
        if _text(value)
    }
    if target_publisher and (not declared_publishers or any(value != target_publisher for value in declared_publishers)):
        return False
    target_edition_id = _text(context.get("edition_id"))
    candidate_edition_id = _text(_first(candidate.get("edition_id"), candidate.get("publication_id")))
    if target_edition_id and target_edition_id != candidate_edition_id:
        return False
    target_publication = source_providers.normalized_query(context.get("publication_title"))
    candidate_publication = source_providers.normalized_query(
        _first(candidate.get("publication_title"), candidate.get("edition_title"), candidate.get("publication"))
    )
    if target_publication and target_publication != candidate_publication:
        return False
    target_marker = _text(context.get("edition_marker")).lower()
    declared_markers = {
        _text(value).lower()
        for value in (
            candidate.get("edition_marker"), outer_evidence.get("edition_marker"), member_evidence.get("edition_marker"),
        )
        if _text(value)
    }
    if target_marker and (
        member_evidence.get("edition_marker") != target_marker
        or any(value != target_marker for value in declared_markers)
    ):
        return False

    current_target = {
        "canonical_work_title": canonical,
        "series_title": canonical,
        "media_type": context.get("media_type"),
        "unit_type": unit_type,
        "unit_number": unit_number,
        "issue_number": unit_number if unit_type == "issue" else "",
        "chapter_number": unit_number if unit_type == "chapter" else "",
        "volume_number": volume_number if unit_type == "volume" else "",
        "year": context.get("publication_year"),
        "publisher": context.get("publisher"),
        "edition_id": context.get("edition_id"),
        "edition_marker": context.get("edition_marker"),
        "publication_title": context.get("publication_title"),
        "allow_collected_edition": False,
    }
    bound_candidate = dict(candidate)
    bound_candidate["pack_contents_match"] = manifest
    bound_candidate["filename"] = manifest.get("entry")
    compatibility = inkdrop_candidate_matching.candidate_compatibility(bound_candidate, current_target)
    member_candidate = dict(bound_candidate)
    member_candidate["title"] = member_entry
    member_candidate["original_result_title"] = member_entry
    member_candidate["filename"] = member_entry
    member_compatibility = inkdrop_candidate_matching.candidate_compatibility(member_candidate, current_target)
    original_compatibility = candidate.get("target_compatibility") if isinstance(candidate.get("target_compatibility"), dict) else {}
    exact_manifest_evidence = "exact_pack_manifest_member" in _list(
        original_compatibility.get("positive_evidence")
    ) or "exact_pack_manifest_member" in _list(compatibility.get("positive_evidence"))
    return bool(
        manifest.get("coverage_source") in {"pack_contents_filename", "pack_contents_volume_filename"}
        and _text(manifest.get("entry"))
        and compatibility.get("status") == "compatible"
        and original_compatibility.get("status") in {None, "", "compatible"}
        and exact_manifest_evidence
        and member_compatibility.get("status") == "compatible"
    )


def _canonical_artifact_safe(candidate: dict[str, Any], *, rejected: bool, pack_verified: bool) -> bool:
    if rejected or not pack_verified or not bool(candidate.get("artifact_safe")):
        return False
    if _text(candidate.get("auto_grab_verdict")).lower() != "auto_grab_safe":
        return False
    if any(
        bool(candidate.get(key))
        for key in (
            "assisted_only", "requires_manual_review", "manual_review_only", "evidence_only",
            "preview_or_sample", "known_bad_candidate", "manual_source_bad_candidate",
            "source_memory_bad_candidate", "already_downloading", "already_imported", "duplicate_active",
            "known_malicious", "malicious", "invalid_archive", "virus", "malware", "corrupt",
            "forbidden", "unauthorized", "access_denied", "access_required", "auth_required",
            "authentication_required", "credential_required", "missing_credential", "unsupported_client",
        )
    ):
        return False
    explicit = _text(candidate.get("acquisition_capability")).lower()
    return not explicit or explicit == "automatic"


def _capability(
    provider_id: str,
    protocol: str,
    candidate: dict[str, Any],
    rejected: bool,
    *,
    pack_candidate: bool = False,
    pack_verified: bool = False,
) -> tuple[str, bool]:
    if rejected:
        return "unavailable", False
    if pack_candidate and not pack_verified:
        assisted = bool(candidate.get("assisted_only")) or bool(candidate.get("requires_manual_review"))
        return ("assisted", True) if assisted else ("manual", False)
    explicit = _text(candidate.get("acquisition_capability")).lower()
    if explicit in ACQUISITION_CAPABILITIES:
        return explicit, explicit == "assisted"
    assisted = bool(candidate.get("assisted_only")) or bool(candidate.get("requires_manual_review"))
    if provider_id in {"getcomics", "rss_getcomics"} and not bool(candidate.get("candidate_safe")):
        assisted = True
    if assisted:
        return "assisted", True
    if protocol in PROTOCOLS and bool(
        candidate.get("candidate_safe")
        or candidate.get("accepted")
        or _text(candidate.get("auto_grab_verdict")).lower() in {"accept", "accepted", "auto", "safe"}
        or _canonical_artifact_safe(
            candidate,
            rejected=rejected,
            pack_verified=pack_verified,
        )
    ):
        return "automatic", False
    return "manual", False


def normalize_candidate(
    candidate: dict[str, Any] | None,
    search_input: dict[str, Any] | None,
    *,
    search_run_id: str,
    provider_id: str = "",
    query_evidence: dict[str, Any] | None = None,
    source_health: dict[str, Any] | None = None,
    discovered_at: Any = None,
) -> dict[str, Any]:
    """Project an existing adapter candidate into the Manual Search contract."""

    candidate = candidate if isinstance(candidate, dict) else {}
    context = structured_search_input(search_input)
    provider_id = _text(_first(provider_id, candidate.get("provider_id"), candidate.get("source"))).lower()
    original_title = _text(
        _first(candidate.get("original_result_title"), candidate.get("title"), candidate.get("releaseTitle"), candidate.get("name"))
    )
    normalized_title = source_providers.normalized_query(original_title)
    child_id, child_name = child_source(candidate)
    protocol = _protocol(provider_id, candidate)
    raw = candidate.get("raw") if isinstance(candidate.get("raw"), dict) else candidate
    pack = classify_pack(original_title, extension=_first(candidate.get("extension"), candidate.get("format")), raw=raw)
    explicit_pack, manifest_evidence = _pack_evidence(candidate, raw)
    if pack.get("pack_type") == "single_issue_chapter" and (explicit_pack or manifest_evidence):
        pack["pack_candidate"] = True
        pack["pack_type"] = "unknown_archive"
    pack["manifest_evidence_available"] = bool(pack.get("manifest_evidence_available") or manifest_evidence)
    pack_verified = not bool(pack.get("pack_candidate")) or _canonical_pack_target_safe(candidate, context)
    interpreted = interpret_title(original_title, context, candidate)
    rejection_codes = _rejection_codes(candidate, protocol)
    explicit_accepted = candidate.get("accepted")
    if explicit_accepted is None:
        explicit_accepted = (
            bool(candidate.get("candidate_safe"))
            or _text(candidate.get("auto_grab_verdict")).lower() in {"accept", "accepted", "auto", "safe"}
            or _canonical_artifact_safe(
                candidate,
                rejected=bool(rejection_codes),
                pack_verified=pack_verified,
            )
        )
    accepted = bool(explicit_accepted) and not rejection_codes and pack_verified
    confidence = _confidence(candidate, bool(rejection_codes))
    capability, assisted_only = _capability(
        provider_id,
        protocol,
        candidate,
        bool(rejection_codes),
        pack_candidate=bool(pack.get("pack_candidate")),
        pack_verified=pack_verified,
    )
    score = _float(_first(candidate.get("match_score"), candidate.get("score")))
    identity_seed = _first(
        candidate.get("candidate_family_identity"),
        candidate.get("candidate_identity"),
        candidate.get("indexer_candidate_key"),
        candidate.get("direct_artifact_key"),
        candidate.get("guid"),
        candidate.get("info_hash"),
        candidate.get("download_url_hash"),
        candidate.get("source_url_hash"),
        original_title,
    )
    # Provider identities are stable across searches, but the stored/public
    # candidate is owned by one run.  Reusing the stable identity as the table
    # primary key made a second search for the same release abort with a UNIQUE
    # constraint error before any provider results could be rendered.
    provider_candidate_identity = _text(candidate.get("provider_candidate_identity")) or (
        "manual-provider-candidate:" + _hash(provider_id, child_id, protocol, identity_seed)[:32]
    )
    candidate_id = "manual-candidate:" + _hash(search_run_id, provider_candidate_identity)[:32]
    query_evidence = query_evidence if isinstance(query_evidence, dict) else {}
    health = source_health if isinstance(source_health, dict) else {}
    remote_user = _first(candidate.get("remote_user"), candidate.get("username"), candidate.get("user"))
    remote_identity = {
        "present": bool(remote_user),
        "masked_label": "remote-user" if remote_user else "",
        "identity_hash": _hash(provider_id, remote_user)[:20] if remote_user else "",
    }
    publisher = _text(_first(candidate.get("publisher"), context.get("publisher")))
    creators = _list(_first(candidate.get("creators"), candidate.get("creator"), context.get("creators")))[:12]
    language = _text(_first(candidate.get("language"), candidate.get("translated_language"))).lower()
    extension = source_providers.normalize_extension(
        _first(candidate.get("extension"), candidate.get("format"), original_title)
    )
    health_snapshot = safe_health_snapshot(health)
    raw_reference = _safe_raw_reference(candidate)
    discovery_timestamp = _timestamp(discovered_at)
    interpreted_edition = interpreted["interpreted_publication"]
    interpreted_year = interpreted["year"]
    age_seconds = _age_seconds(candidate, provider_id)
    if "direct_url_available" in candidate:
        direct_url_available = bool(candidate.get("direct_url_available"))
    else:
        direct_url_available = bool(
            protocol == "direct"
            and _first(candidate.get("download_url_hash"), candidate.get("direct_artifact_key"))
        )
    rejection_explanations = [
        REJECTION_EXPLANATIONS.get(code, _text(code).replace("_", " ").capitalize() + ".")
        for code in rejection_codes
    ]
    return {
        "manual_search_candidate_contract_version": CONTRACT_VERSION,
        "candidate_id": candidate_id,
        "candidate_family_identity": _text(candidate.get("candidate_family_identity")),
        "candidate_instance_identity": _text(candidate.get("candidate_instance_identity")),
        "content_identity": _text(candidate.get("content_identity")),
        "provider_candidate_identity": provider_candidate_identity,
        "search_run_id": _text(search_run_id),
        "request_id": _text(_first(query_evidence.get("request_id"), candidate.get("request_id"))),
        "provider_id": provider_id,
        "provider_display_name": provider_display_name(provider_id, candidate),
        "child_source_id": child_id,
        "child_source_name": child_name,
        "indexer_or_extension": child_name or child_id,
        "provider_result_label": " · ".join(
            item for item in (provider_display_name(provider_id, candidate), child_name) if item
        ),
        "protocol": protocol,
        "original_title": original_title,
        "original_result_title": original_title,
        "normalized_title": normalized_title,
        **interpreted,
        "interpreted_edition": interpreted_edition,
        "interpreted_year": interpreted_year,
        "interpreted_publisher": publisher,
        "publisher": publisher,
        "creator_evidence": creators,
        "language": language,
        "format": extension.lstrip("."),
        "file_archive_format": extension.lstrip("."),
        "size_bytes": _int(_first(candidate.get("size_bytes"), candidate.get("size"))),
        "age_seconds": age_seconds,
        "age": _first(candidate.get("age"), candidate.get("publish_date"), candidate.get("publishDate")),
        "seeders": _int(_first(candidate.get("seeders"), candidate.get("seedCount"))),
        "peers": _int(_first(candidate.get("peers"), candidate.get("leechers"), candidate.get("peerCount"))),
        "remote_availability_state": _text(
            _first(candidate.get("remote_availability_state"), candidate.get("availability"))
        ),
        "remote_queue_state": _text(_first(candidate.get("remote_queue_state"), candidate.get("queue_state"))),
        "remote_identity": remote_identity,
        "direct_url_available": direct_url_available,
        **pack,
        "likely_wanted_coverage": _list(
            _first(candidate.get("likely_wanted_coverage"), candidate.get("wanted_coverage"))
        )[:100],
        "match_score": score,
        "confidence_tier": confidence,
        "accepted": accepted,
        "rejection_codes": rejection_codes,
        "rejection_explanations": rejection_explanations,
        "acquisition_capability": capability,
        "assisted_only": assisted_only,
        "health_snapshot": health_snapshot,
        "source_health_snapshot": health_snapshot,
        "raw_evidence_reference": raw_reference,
        "bounded_raw_evidence_reference": raw_reference,
        "query_evidence": {
            "query": _text(
                _first(
                    query_evidence.get("query"),
                    candidate.get("query_variant"),
                    candidate.get("_inkdrop_query_variant"),
                )
            ),
            "query_kind": _text(query_evidence.get("query_kind")),
            "query_group": _text(_first(query_evidence.get("query_group"), candidate.get("query_group"))),
            "request_id": _text(_first(query_evidence.get("request_id"), candidate.get("request_id"))),
            "ordinal": _int(_first(query_evidence.get("ordinal"), candidate.get("_inkdrop_query_index"))),
        },
        "source_unit_evidence": safe_public_structure(candidate.get("source_unit_evidence") or {}),
        "parsed_unit_type": _text(candidate.get("parsed_unit_type")),
        "parsed_volume_number": _text(candidate.get("parsed_volume_number")),
        "parsed_book_number": _text(candidate.get("parsed_book_number")),
        "parsed_issue_number": _text(candidate.get("parsed_issue_number")),
        "parsed_chapter_number": _text(candidate.get("parsed_chapter_number")),
        "parsed_coverage_start": _text(candidate.get("parsed_coverage_start")),
        "parsed_coverage_end": _text(candidate.get("parsed_coverage_end")),
        "edition_marker": _text(candidate.get("edition_marker")),
        "pack_marker": _text(candidate.get("pack_marker")),
        "preview_or_sample": bool(candidate.get("preview_or_sample")),
        "target_compatibility": safe_public_structure(candidate.get("target_compatibility") or {}),
        "block_reasons": [_redacted_text(value, 160) for value in _list(candidate.get("block_reasons"))[:24]],
        "review_reasons": [_redacted_text(value, 160) for value in _list(candidate.get("review_reasons"))[:24]],
        "inspection_message": _redacted_text(candidate.get("inspection_message"), 160),
        "preferred_size_bytes": _int(candidate.get("preferred_size_bytes")),
        "candidate_safe": bool(candidate.get("candidate_safe")),
        "artifact_safe": bool(candidate.get("artifact_safe")),
        "auto_grab_verdict": _text(candidate.get("auto_grab_verdict")),
        "quality_status": _text(candidate.get("quality_status")),
        "discovered_at": discovery_timestamp,
        "discovery_timestamp": discovery_timestamp,
    }


def deduplicate_candidates(candidates: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the strongest copy and mark duplicate evidence without hiding it."""

    ordered = list(candidates)
    groups: dict[str, list[dict[str, Any]]] = {}
    for candidate in ordered:
        groups.setdefault(_text(candidate.get("candidate_id")), []).append(candidate)
    out: list[dict[str, Any]] = []
    for candidate_id, rows in groups.items():
        rows.sort(
            key=lambda row: (
                bool(row.get("accepted")),
                _float(row.get("match_score")) or 0,
                bool(row.get("child_source_name")),
            ),
            reverse=True,
        )
        primary = dict(rows[0])
        primary["duplicate_result_count"] = len(rows) - 1
        primary["duplicate_query_evidence"] = [row.get("query_evidence") or {} for row in rows[1:10]]
        out.append(primary)
    return out


def classify_provider_call(*, completed: bool, result_count: int = 0, error: Any = "") -> dict[str, Any]:
    """Keep a timeout/provider failure distinct from a valid zero-result call."""

    error_text = _text(error)
    if completed and not error_text:
        status = "results" if int(result_count or 0) > 0 else "zero_results"
    elif re.search(r"(?i)timeout|timed out|deadline", error_text):
        status = "provider_timeout"
    else:
        status = "provider_failure"
    return {
        "status": status,
        "provider_call_completed": bool(completed and not error_text),
        "result_count": max(0, int(result_count or 0)),
        "error_summary": _redacted_text(error_text),
    }


def contract_fields() -> list[str]:
    """Stable display/fixture field list for Core and UIBot handoff."""

    sample = normalize_candidate({}, {}, search_run_id="contract-fields")
    return list(sample.keys())
