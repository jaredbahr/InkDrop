"""Request-building prep for InkDrop source worker adapters.

The functions here do not own matching, import, or queue mutation. They build
settings-derived HTTP/tool/manual plans and optionally execute HTTP requests via
an injected client supplied by a live worker or smoke test.
"""

from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from datetime import datetime, timedelta
from urllib.parse import quote, quote_plus, urlparse

from core import inkdrop_sources
from core import inkdrop_source_providers as providers


CONTRACT_VERSION = 1
TRUSTED_SINGLETON_PROOF_SOURCES = {
    "comicvine_authoritative_count_and_canonical_issue_identity",
    "comicvine_collected_single_wanted_identity_without_declared_count",
}

STANDARD_EBOOKS_OPDS_URL = "https://standardebooks.org/feeds/opds"
GUTENDEX_BOOKS_URL = "https://gutendex.com/books"
INTERNET_ARCHIVE_SEARCH_URL = "https://archive.org/services/search/v1/scrape"
INTERNET_ARCHIVE_METADATA_BASE = "https://archive.org/metadata"
MANGADEX_API_BASE = "https://api.mangadex.org"
# MangaDex's own API default is safe + suggestive + erotica; it excludes only
# pornographic. InkDrop defaulted to safe + suggestive, which is stricter than
# upstream and silently so: the rating filter is applied in the request, so an
# excluded title is not down-ranked or flagged, it simply does not exist as far as
# the user can tell. Berserk is rated erotica on MangaDex, so searching for it
# returned nothing at all -- reported by a tester on 2026-08-01, and it is 8,959
# titles wide, not one book.
#
# Mature content is still handled, just at the right layer: mangadex_search_result
# scores erotica and pornographic at -35, so it ranks below an equally-matching
# safe title instead of vanishing. That penalty was dead code for erotica while
# the request filter removed those rows first. Pornographic stays excluded by
# default, and the whole list remains user-configurable.
MANGADEX_DEFAULT_CONTENT_RATINGS = ("safe", "suggestive", "erotica")

SUWAYOMI_API_BASE = str(os.environ.get("INKDROP_SUWAYOMI_API_BASE_URL") or "").strip().rstrip("/")
GETCOMICS_FEED_URL = "https://getcomics.org/feed/"
GETCOMICS_DISCOVERY_HOSTS = ("getcomics.org", "www.getcomics.org")
PIXELDRAIN_TRANSPORT_HOSTS = ("pixeldrain.com", "www.pixeldrain.com")
COMICSCODES_FEED_URL = "https://comics.codes/feed/"
COMICSCODES_LIST_URLS = (
    "https://comics.codes/all-comics-list/",
    "https://comics.codes/all-manga-list/",
)
PROWLARR_SEARCH_PATH = "/api/v1/search"
INDEXER_PACK_DETAIL_MAX_BYTES = 5 * 1024 * 1024
INDEXER_PACK_DETAIL_SIDECAR_MAX_BYTES = 1024 * 1024
INDEXER_PACK_DETAIL_HEADER_KEYS = (
    "x-dnzb-nfo",
    "x-nzb-nfo",
    "x-dnzb-details",
    "x-nzb-details",
)
INDEXER_RESULT_ADAPTER_FAMILIES = {
    "prowlarr_indexer",
    "torznab_indexer",
    "newznab_indexer",
    "torrent_rss_feed",
}

SUWAYOMI_FETCH_MANGA_AND_CHAPTERS_MUTATION = """
mutation($input: FetchMangaAndChaptersInput!) {
  fetchMangaAndChapters(input: $input) {
    manga {
      id
      sourceId
      title
      realUrl
      url
      artist
      author
      status
    }
    chapters {
      id
      name
      chapterNumber
      meta {
        key
        value
      }
      sourceOrder
      pageCount
      mangaId
      realUrl
      scanlator
    }
  }
}
""".strip()

SUWAYOMI_FETCH_MANGA_AND_CHAPTERS_NO_META_MUTATION = """
mutation($input: FetchMangaAndChaptersInput!) {
  fetchMangaAndChapters(input: $input) {
    manga {
      id
      sourceId
      title
      realUrl
      url
      artist
      author
      status
    }
    chapters {
      id
      name
      chapterNumber
      sourceOrder
      pageCount
      mangaId
      realUrl
      scanlator
    }
  }
}
""".strip()

SUWAYOMI_FETCH_CHAPTER_PAGES_MUTATION = """
mutation($input: FetchChapterPagesInput!) {
  fetchChapterPages(input: $input) {
    chapter {
      id
      name
      chapterNumber
      meta {
        key
        value
      }
      sourceOrder
      pageCount
      mangaId
      realUrl
      scanlator
    }
    pages
  }
}
""".strip()

SUWAYOMI_FETCH_CHAPTER_PAGES_NO_META_MUTATION = """
mutation($input: FetchChapterPagesInput!) {
  fetchChapterPages(input: $input) {
    chapter {
      id
      name
      chapterNumber
      sourceOrder
      pageCount
      mangaId
      realUrl
      scanlator
    }
    pages
  }
}
""".strip()
RSS_FEED_EVIDENCE_ADAPTER_FAMILIES = {
    "rss_feed",
    "rss_direct_feed",
    "rss_detail_direct_feed",
    "rss_detail_probe_feed",
    "rss_reader_page_pack_feed",
    "torrent_rss_feed",
    "torrent_detail_rss_feed",
}

DEFAULT_SECRET_REFS_BY_PROVIDER_ID = {
    "generic_newznab_indexer": "generic_newznab_indexer_api_key",
    "generic_torznab_indexer": "generic_torznab_indexer_api_key",
    "prowlarr": "prowlarr_api_key",
}


def source_query(wanted_item=None):
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    return providers.normalized_query(
        wanted_item.get("query")
        or wanted_item.get("series_title")
        or wanted_item.get("series")
        or wanted_item.get("title")
        or ""
    )


def indexer_source_query(wanted_item=None):
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    query = source_query(wanted_item)
    if wanted_item.get("query"):
        return query
    number = providers.first_text(
        wanted_item.get("issue_number"),
        wanted_item.get("normalized_number"),
        wanted_item.get("chapter_number"),
        wanted_item.get("chapter"),
        wanted_item.get("number"),
    )
    series = providers.normalized_query(
        providers.first_text(
            wanted_item.get("series_title"),
            wanted_item.get("series"),
        )
    )
    # A number that belongs to the series name is not evidence that the
    # wanted issue is already present.  Substring checks collapsed titles such
    # as "9-1-1" issue 1, "20th Century Boys" issue 20, and "300" issue 3 to
    # the bare series query.  When the query came from the canonical series
    # field, add the wanted number even if that same number appears in the
    # title.  Explicit operator queries still return unchanged above.
    query_is_numeric_series = bool(
        series
        and query.casefold() == series.casefold()
        and _query_title_contains_number(series)
    )
    if query and number and (query_is_numeric_series or str(number) not in query):
        return providers.normalized_query(f"{query} {number}")
    return query


def _query_has_issue_number(query, issue_number):
    issue = str(issue_number or "").strip()
    if not issue:
        return True
    tokens = re.findall(r"[A-Za-z0-9]+", str(query or "").lower())
    issue_tokens = re.findall(r"[A-Za-z0-9]+", issue.lower())
    return bool(issue_tokens and " ".join(issue_tokens) in {" ".join(tokens[index:index + len(issue_tokens)]) for index in range(len(tokens))})


def _query_has_volume_number(query, volume_number):
    volume = str(volume_number or "").strip()
    if not volume:
        return False
    query = providers.normalized_query(query)
    if not query:
        return False
    try:
        volume_int = int(float(volume))
        volume_pattern = f"0*{volume_int}"
    except Exception:
        volume_pattern = re.escape(volume)
    return bool(re.search(rf"(?i)\b(?:vol(?:ume)?\.?|v)\s*{volume_pattern}\b", query))


def _query_title_contains_number(query):
    return bool(re.search(r"\d", providers.normalized_query(query)))


def _query_without_trailing_year(query):
    text = providers.normalized_query(query)
    return providers.normalized_query(re.sub(r"\s+(?:19|20)\d{2}$", "", text))


_LEADING_CREATOR_POSSESSIVE_RE = re.compile(
    r"^([A-Z][A-Za-z0-9.\-]*(?:\s+[A-Z][A-Za-z0-9.\-]*){1,4})['\u2019]s\s+(.+)$",
)


def _query_without_leading_creator_possessive(query):
    text = providers.normalized_query(query)
    if not text:
        return ""
    match = _LEADING_CREATOR_POSSESSIVE_RE.match(text)
    if not match:
        return ""
    tail = providers.normalized_query(match.group(2))
    if not tail or not re.search(r"[A-Za-z]", tail):
        return ""
    return tail


def _leading_creator_possessive_name(query):
    """The creator name a leading possessive strips, e.g. "Naoki Urasawa" from
    "Naoki Urasawa's Monster". Used to validate a MangaDex work's real author/
    artist relationships once title matching has already accepted a broad
    alias -- see mangadex_manga_matches_wanted's expected-creator check.
    """
    text = providers.normalized_query(query)
    if not text:
        return ""
    match = _LEADING_CREATOR_POSSESSIVE_RE.match(text)
    if not match:
        return ""
    return providers.normalized_query(match.group(1))


def _query_without_leading_article(query):
    text = providers.normalized_query(query)
    if not text:
        return ""
    match = re.match(r"^(?:the|an|a)\s+(.+)$", text, flags=re.IGNORECASE)
    if not match:
        return ""
    tail = providers.normalized_query(match.group(1))
    alpha_tokens = re.findall(r"[A-Za-z]+", tail)
    if len(alpha_tokens) < 2:
        return ""
    return tail


def _query_ascii_fold(query):
    text = providers.normalized_query(query)
    if not text:
        return ""
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    folded = providers.normalized_query(folded)
    return folded if folded and folded.lower() != text.lower() else ""


def _query_alias_values(value):
    if value in (None, "", [], {}):
        return []
    if isinstance(value, str):
        parts = []
        for line in value.splitlines():
            for part in re.split(r"[,|]", line):
                text = providers.normalized_query(part)
                if text:
                    parts.append(text)
        return parts
    if isinstance(value, (list, tuple, set)):
        out = []
        for item in value:
            out.extend(_query_alias_values(item))
        return out
    return [providers.normalized_query(value)]


def _query_alias_policy_entries(policy):
    policy = policy if isinstance(policy, dict) else {}
    entries = []
    for key in ("series_query_aliases", "query_aliases", "title_query_aliases", "series_title_aliases"):
        value = policy.get(key)
        if isinstance(value, dict):
            for match, aliases in value.items():
                match_text = providers.normalized_query(match)
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
                match_text = providers.normalized_query(match)
                alias_values = _query_alias_values(aliases)
                if match_text and alias_values:
                    entries.append((match_text, alias_values))
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                if isinstance(item, dict):
                    match_text = providers.normalized_query(
                        providers.first_text(
                            item.get("series"),
                            item.get("series_title"),
                            item.get("title"),
                            item.get("query"),
                            item.get("match"),
                        )
                    )
                    alias_values = _query_alias_values(
                        providers.first_value(
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
                    match_text = providers.normalized_query(match)
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
    ]
    query = source_query(wanted_item)
    values.append(query)
    values.append(_query_without_trailing_year(query))
    values.append(_query_without_leading_creator_possessive(query))
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
    supplied_aliases = providers.first_value(
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
            alias = providers.normalized_query(value)
            key = alias.lower()
            if alias and key not in seen:
                seen.add(key)
                aliases.append(alias)
    return aliases


def _issue_number_variants(issue_number):
    text = providers.normalized_query(str(issue_number or "").lstrip("#"))
    if not text:
        return []
    out = [text]
    if re.fullmatch(r"\d+", text):
        integer = int(text)
        if len(text) == 1:
            for padded in (f"{integer:03d}", f"{integer:02d}"):
                if padded != text and padded not in out:
                    out.append(padded)
        elif len(text) == 2:
            padded = f"{integer:03d}"
            if padded != text:
                out.append(padded)
    return out


def _wanted_indexer_unit_type(wanted_item=None):
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    return str(
        providers.first_text(
            wanted_item.get("unitType"),
            wanted_item.get("unit_type"),
            wanted_item.get("unit"),
        )
    ).strip().lower()


def _wanted_indexer_volume_number(wanted_item=None):
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    explicit = providers.first_text(
        wanted_item.get("volume"),
        wanted_item.get("volume_number"),
        wanted_item.get("volumeNumber"),
        wanted_item.get("book_volume"),
        wanted_item.get("manga_volume"),
    )
    if explicit:
        return explicit
    for key in ("issue_title", "title", "searchQuery", "search_query", "query"):
        text = providers.normalized_query(wanted_item.get(key))
        if not text:
            continue
        match = re.search(r"(?i)\bvol(?:ume)?\.?\s*(\d+(?:\.\d+)?)\b", text)
        if match:
            return match.group(1)
    if providers.wanted_item_is_single_volume_artifact_unit(wanted_item):
        return providers.first_text(
            wanted_item.get("issue_number"),
            wanted_item.get("normalized_number"),
            wanted_item.get("number"),
        )
    if _wanted_indexer_unit_type(wanted_item) in {"volume", "vol", "book_volume", "manga_volume"}:
        return providers.first_text(
            wanted_item.get("issue_number"),
            wanted_item.get("normalized_number"),
            wanted_item.get("number"),
        )
    return ""


def _volume_number_variants(volume_number):
    text = providers.normalized_query(str(volume_number or "").lstrip("#"))
    if not text:
        return []
    out = [text]
    try:
        numeric = float(text)
    except Exception:
        return out
    if not numeric.is_integer():
        return out
    integer = int(numeric)
    for padded in (f"{integer:02d}", f"{integer:03d}"):
        if padded not in out:
            out.append(padded)
    return out


def _is_volume_wanted_item(wanted_item=None):
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    unit_type = _wanted_indexer_unit_type(wanted_item)
    return bool(
        unit_type in {"volume", "vol", "book_volume", "manga_volume"}
        or _wanted_indexer_volume_number(wanted_item)
    )


def _prefer_ascii_folded_volume_queries(values, wanted_item=None):
    if not _is_volume_wanted_item(wanted_item):
        return list(values or [])
    volume_number = _wanted_indexer_volume_number(wanted_item)
    if not volume_number:
        return list(values or [])
    out = list(values or [])
    for query in list(out):
        if not _query_has_volume_number(query, volume_number):
            continue
        folded = _query_ascii_fold(query)
        if not folded:
            continue
        try:
            query_index = out.index(query)
            folded_index = out.index(folded)
        except ValueError:
            continue
        if folded_index <= query_index:
            continue
        out.pop(folded_index)
        query_index = out.index(query)
        out.insert(query_index, folded)
    return out


def _series_only_indexer_queries(wanted_item=None, *, max_queries=1, policy=None):
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    policy = policy if isinstance(policy, dict) else {}
    try:
        limit = int(max_queries or 1)
    except (TypeError, ValueError):
        limit = 1
    limit = max(0, min(limit, 3))
    if limit <= 0:
        return []
    series = providers.normalized_query(
        providers.first_text(
            wanted_item.get("series_title"),
            wanted_item.get("series"),
            wanted_item.get("manga_title"),
            wanted_item.get("title"),
        )
    )
    values = []
    seen = set()

    def add(value):
        query = providers.normalized_query(value)
        key = query.lower()
        if query and key not in seen:
            seen.add(key)
            values.append(query)

    add(series)
    for alias in inkdrop_sources.contributor_title_aliases(series):
        add(alias)
    add(_query_without_leading_creator_possessive(series))
    add(_query_without_leading_article(series))
    for alias in _series_query_aliases(wanted_item, policy=policy):
        add(alias)
    return values[:limit]


def _safe_short_collected_query_aliases(series, wanted_item=None):
    """Return structurally-derived, distinctive aliases shortest-first.

    These forms come only from the qualified-title contract (for example an
    edition qualifier before a colon). Removing punctuation or one leading
    article preserves the same token identity; it never collapses to a bare
    franchise name.
    """

    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    values = []
    seen = set()

    def add(value):
        query = providers.normalized_query(value)
        words = re.findall(r"[a-z0-9]+", query.lower())
        meaningful = [word for word in words if word not in {"a", "an", "the", "of"}]
        if len(words) < 3 or len(meaningful) < 2:
            return
        key = query.lower()
        if key not in seen:
            seen.add(key)
            values.append(query)

    aliases = list(inkdrop_sources.collected_title_aliases(series))
    if (
        wanted_item.get("collected_singleton_proof") is True
        and str(wanted_item.get("collected_singleton_proof_source") or "")
        == "comicvine_collected_single_wanted_identity"
        and providers.int_value(wanted_item.get("collected_singleton_wanted_count"), 0) == 1
    ):
        aliases.extend(wanted_item.get("collected_singleton_title_aliases") or [])
    for alias in aliases:
        add(alias)
        add(re.sub(r"[:._\-\u2013\u2014]+", " ", str(alias or "")))
        add(re.sub(r"(?i)^(?:a|an|the)\s+", "", str(alias or "").strip()))
    return sorted(
        values,
        key=lambda value: (
            len(re.findall(r"[a-z0-9]+", value.lower())),
            len(value),
            value.lower(),
        ),
    )


def _include_series_fallback_within_limit(values, wanted_item=None, *, limit=3, policy=None):
    try:
        cap = int(limit or 3)
    except (TypeError, ValueError):
        cap = 3
    cap = max(1, min(cap, 6))
    capped = list(values or [])[:cap]
    if cap < 2:
        return capped
    fallback_queries = _series_only_indexer_queries(wanted_item, max_queries=3, policy=policy)
    for fallback in fallback_queries:
        key = str(fallback or "").strip().lower()
        if not key:
            continue
        if any(str(query or "").strip().lower() == key for query in capped):
            continue
        if len(capped) < cap:
            capped.append(fallback)
        else:
            capped[-1] = fallback
    return capped


def _proven_singleton_comic_wanted(wanted_item=None):
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    media_type = providers.normalized_query(
        providers.first_text(wanted_item.get("media_type"), wanted_item.get("library_type"))
    ).lower()
    issue_number = str(
        providers.first_text(wanted_item.get("issue_number"), wanted_item.get("normalized_number")) or ""
    ).strip().lstrip("0") or "0"
    try:
        canonical_issue_count = int(wanted_item.get("canonical_issue_count") or 0)
        metadata_issue_count = int(wanted_item.get("metadata_issue_count") or 0)
    except (TypeError, ValueError):
        canonical_issue_count = metadata_issue_count = 0
    proof_source = str(wanted_item.get("singleton_issue_proof_source") or "")
    count_supported = bool(
        metadata_issue_count == 1
        if proof_source == "comicvine_authoritative_count_and_canonical_issue_identity"
        else (
            metadata_issue_count == 0
            and proof_source == "comicvine_collected_single_wanted_identity_without_declared_count"
        )
    )
    return bool(
        wanted_item.get("singleton_issue_proof")
        and proof_source in TRUSTED_SINGLETON_PROOF_SOURCES
        and wanted_item.get("singleton_metadata_trusted") is True
        and wanted_item.get("singleton_metadata_fresh") is True
        and wanted_item.get("singleton_issue_metadata_trusted") is True
        and issue_number == "1"
        and canonical_issue_count == 1
        and count_supported
        and ("comic" in media_type or "graphic novel" in media_type)
    )


def indexer_source_queries(wanted_item=None, *, max_queries=3, policy=None, include_series_fallback=False):
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    policy = policy if isinstance(policy, dict) else {}
    try:
        limit = int(max_queries or 3)
    except (TypeError, ValueError):
        limit = 3
    limit = max(1, min(limit, 6))
    issue_number = providers.first_text(
        wanted_item.get("issue_number"),
        wanted_item.get("normalized_number"),
        wanted_item.get("chapter_number"),
        wanted_item.get("chapter"),
        wanted_item.get("number"),
    )
    series = providers.normalized_query(
        providers.first_text(
            wanted_item.get("series_title"),
            wanted_item.get("series"),
            wanted_item.get("manga_title"),
            wanted_item.get("title"),
        )
    )
    base = indexer_source_query(wanted_item)
    base_without_creator = _query_without_leading_creator_possessive(base)
    series_without_creator = _query_without_leading_creator_possessive(series)
    series_without_article = _query_without_leading_article(series)
    issue_variants = _issue_number_variants(issue_number)
    volume_variants = _volume_number_variants(_wanted_indexer_volume_number(wanted_item))
    is_volume_wanted = _is_volume_wanted_item(wanted_item)
    alias_stems = _series_query_aliases(wanted_item, policy=policy)
    manual_queries = wanted_item.get("manual_search_queries") if isinstance(wanted_item.get("manual_search_queries"), list) else []
    values = []
    seen = set()

    def add(value, *, include_ascii=False):
        query = providers.normalized_query(value)
        key = query.lower()
        if query and key not in seen:
            seen.add(key)
            values.append(query)
        if include_ascii:
            folded = _query_ascii_fold(query)
            folded_key = folded.lower()
            if folded and folded_key not in seen:
                seen.add(folded_key)
                values.append(folded)

    # Qualified collected works often index only under their shortest
    # distinctive subtitle. Automatic and Manual Search share this exact
    # bounded progression: unit-bearing alias first, then title-only discovery
    # (needed for collected singletons), then a second exact alias spelling.
    collected_query_aliases = _safe_short_collected_query_aliases(series, wanted_item)
    if collected_query_aliases and issue_variants and not is_volume_wanted:
        shortest_alias = collected_query_aliases[0]
        add(f"{shortest_alias} {issue_variants[0]}", include_ascii=True)
        add(shortest_alias, include_ascii=True)
        for alias in collected_query_aliases[1:]:
            add(f"{alias} {issue_variants[0]}", include_ascii=True)
            if len(values) >= limit:
                break
        if len(values) >= limit:
            return values[:limit]

    # Volume searches previously diverged at the first request: Manual Search
    # used the common unpadded ``Title Vol 7`` spelling, while unattended
    # Prowlarr spent its bounded slots on a broad title, ``Volume 7``, and
    # padded ``Vol 07``. Use one canonical exact/broad/alternate progression
    # for both paths; candidate compatibility still enforces the wanted volume.
    if is_volume_wanted and volume_variants and series:
        primary_volume = volume_variants[0]
        add(f"{series} Vol {primary_volume}", include_ascii=True)
        add(series, include_ascii=True)
        # Preserve one exact slot for an operator-validated alternate title.
        # A broad alias alone may return the right series but gives an indexer
        # no volume identity to target inside the bounded automatic budget.
        if alias_stems:
            add(f"{alias_stems[0]} Vol {primary_volume}", include_ascii=True)
        add(f"{series} Volume {primary_volume}", include_ascii=True)
        if len(values) >= limit:
            return values[:limit]

    # Numeric series names make especially noisy bare indexer searches.  A
    # production search for "300" returned dozens of unrelated issue-300
    # releases while the wanted issue was absent from the first bounded slot.
    # Put the exact issue-bearing form first for this title class, then retain
    # the existing broad and padded fallbacks in the remaining slots.
    if (
        not is_volume_wanted
        and not wanted_item.get("manual_search")
        and series
        and issue_variants
        and _query_title_contains_number(series)
    ):
        add(f"{series} {issue_variants[0]}", include_ascii=True)

    if not wanted_item.get("manual_search"):
        collected_aliases = inkdrop_sources.collected_title_aliases(series)
        contributor_aliases = inkdrop_sources.contributor_title_aliases(series)
        discovery_stems = [*collected_aliases, *contributor_aliases, *alias_stems, series_without_creator, series]
        for stem in discovery_stems:
            add(stem, include_ascii=True)
            if len(values) >= min(limit, 2):
                break

    if wanted_item.get("manual_search") and manual_queries and not alias_stems:
        # The Manual Search contract already supplies a bounded narrow-to-broad
        # progression. For ordinary titles, use it instead of spending every
        # call on zero-padding variants (for example Adventureman 9/009/09).
        # Collected-title aliases retain the specialized narrow alias sequence
        # below, and automated acquisition remains unchanged.
        for query in manual_queries:
            add(query, include_ascii=True)
            if len(values) >= limit:
                break
        if values:
            return values[:limit]

    if not is_volume_wanted:
        add(base, include_ascii=True)
        add(base_without_creator, include_ascii=True)
    if wanted_item.get("manual_search") and alias_stems and issue_variants:
        # One issue-bearing alias plus one series alias gives indexers a useful
        # narrow/broad pair inside the existing three-call default budget.
        # Result matching below still requires the full anchored alias.
        add(f"{alias_stems[0]} {issue_variants[0]}", include_ascii=True)
        add(alias_stems[0], include_ascii=True)
    if alias_stems:
        for alias in alias_stems:
            if is_volume_wanted and volume_variants:
                for volume in volume_variants[:1]:
                    add(f"{alias} Vol. {volume}", include_ascii=True)
                    add(f"{alias} {volume}", include_ascii=True)
                    add(f"{alias} v{volume}", include_ascii=True)
                    add(f"{alias} Volume {volume}", include_ascii=True)
            elif issue_variants:
                for issue in issue_variants:
                    add(f"{alias} {issue}", include_ascii=True)
            else:
                add(alias, include_ascii=True)
    if is_volume_wanted and volume_variants:
        volume_stems = [
            value
            for value in [
                series_without_creator,
                series_without_article,
                series,
            ]
            if value
        ]
        for stem in volume_stems:
            primary_volume = volume_variants[0]
            padded_volume = (
                volume_variants[1]
                if len(str(primary_volume)) == 1 and len(volume_variants) > 1
                else primary_volume
            )
            add(f"{stem} Volume {primary_volume}", include_ascii=True)
            add(f"{stem} Vol {padded_volume}", include_ascii=True)
            add(f"{stem} v{padded_volume}", include_ascii=True)
        add(base, include_ascii=True)
        add(base_without_creator, include_ascii=True)
    primary_issues = issue_variants[:1]
    padded_issues = issue_variants[1:]
    if base and issue_number and not _query_has_issue_number(base, issue_number):
        for issue in primary_issues:
            add(f"{base} {issue}", include_ascii=True)
        if base_without_creator:
            for issue in primary_issues:
                add(f"{base_without_creator} {issue}", include_ascii=True)
    if series_without_creator and issue_variants:
        for issue in issue_variants:
            add(f"{series_without_creator} {issue}", include_ascii=True)
    if series_without_article and issue_variants:
        for issue in issue_variants:
            add(f"{series_without_article} {issue}", include_ascii=True)
    if series and primary_issues:
        for issue in primary_issues:
            add(f"{series} {issue}", include_ascii=True)
    if base and issue_number and not _query_has_issue_number(base, issue_number):
        for issue in padded_issues:
            add(f"{base} {issue}", include_ascii=True)
        if base_without_creator:
            for issue in padded_issues:
                add(f"{base_without_creator} {issue}", include_ascii=True)
    if series and padded_issues:
        for issue in padded_issues:
            add(f"{series} {issue}", include_ascii=True)
    add(_query_without_trailing_year(base), include_ascii=True)
    add(_query_without_leading_creator_possessive(_query_without_trailing_year(base)), include_ascii=True)
    add(_query_without_leading_article(_query_without_trailing_year(base)), include_ascii=True)
    if include_series_fallback and issue_number and not is_volume_wanted:
        queries = _include_series_fallback_within_limit(
            values,
            wanted_item,
            limit=limit,
            policy=policy,
        )
        if _proven_singleton_comic_wanted(wanted_item) and len(queries) > 1:
            exact_series_key = series.lower()
            if series and not any(str(query or "").strip().lower() == exact_series_key for query in queries):
                queries[-1] = series
        return queries
    return values[:limit]


def _wanted_years(wanted_item=None, *, include_current=True):
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    years = []
    seen = set()

    def add(value):
        text = str(value or "").strip()
        if not text:
            return
        chunks = (
            text.replace("/", " ")
            .replace("-", " ")
            .replace("_", " ")
            .replace(".", " ")
            .split()
        )
        for chunk in chunks or [text]:
            if not str(chunk).isdigit():
                continue
            year = int(chunk)
            if 1900 <= year <= 2100 and year not in seen:
                seen.add(year)
                years.append(year)
                return

    for key in (
        "year",
        "issue_year",
        "publication_year",
        "release_year",
        "start_year",
        "cover_year",
        "date",
        "publish_date",
        "cover_date",
    ):
        add(wanted_item.get(key))
    if include_current:
        add(time.localtime().tm_year)
    return years


def _wanted_date_anchors(wanted_item=None):
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    out = []
    seen = set()
    for key in ("release_date", "issue_date", "date", "publish_date", "publishedAt", "publishAt", "cover_date", "coverDate"):
        value = str(wanted_item.get(key) or "").strip()
        if not value:
            continue
        match = re.search(r"\b((?:19|20)\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b", value)
        if not match:
            continue
        try:
            anchor = datetime(int(match.group(1)), int(match.group(2)), int(match.group(3))).date()
        except ValueError:
            continue
        if anchor not in seen:
            seen.add(anchor)
            out.append(anchor)
    return out


def _weekly_pack_date_queries(wanted_item=None, *, max_queries=7):
    try:
        limit = int(max_queries or 0)
    except (TypeError, ValueError):
        limit = 0
    limit = max(0, min(limit, 12))
    if limit <= 0:
        return []
    out = []
    seen = set()
    for anchor in _wanted_date_anchors(wanted_item):
        start = anchor - timedelta(days=70)
        end = anchor + timedelta(days=7)
        center = anchor - timedelta(days=35) if anchor.day == 1 else anchor
        candidate_dates = []
        current = end
        while current >= start:
            if current.weekday() == 2:
                candidate_dates.append(current)
            current -= timedelta(days=1)
        candidate_dates.sort(key=lambda value: (abs((value - center).days), value > center, -value.toordinal()))
        for date_value in candidate_dates:
            query = f"{date_value.isoformat()} Weekly Comics Pack"
            key = query.lower()
            if key not in seen:
                seen.add(key)
                out.append(query)
                if len(out) >= limit:
                    break
        if len(out) >= limit:
            break
    return out


def _historical_weekly_pack_years(wanted_item=None, *, current_year=None, broad_max_age_years=2):
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    current_year = int(current_year or time.localtime().tm_year)
    try:
        max_age = max(0, int(broad_max_age_years))
    except (TypeError, ValueError):
        max_age = 2
    years = []
    seen = set()

    def add(year):
        try:
            value = int(year)
        except (TypeError, ValueError):
            return
        if 1900 <= value <= 2100 and value not in seen:
            seen.add(value)
            years.append(value)

    date_anchors = _wanted_date_anchors(wanted_item)
    for anchor in date_anchors:
        add(anchor.year)
    if not years:
        for year in _wanted_years(wanted_item, include_current=False):
            add(year)
    if not years:
        return []
    if max(years) >= current_year - max_age:
        return []
    return sorted(years, reverse=True)


def _comic_pack_publisher_key(wanted_item=None):
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    publisher = providers.normalized_query(
        providers.first_text(wanted_item.get("publisher"), wanted_item.get("imprint"), wanted_item.get("publisher_name"))
    ).lower()
    if not publisher:
        return ""
    if publisher in {"dc", "dc comics"} or publisher.startswith("dc "):
        return "dc"
    if "marvel" in publisher:
        return "marvel"
    if publisher in {"image", "image comics"} or "image comics" in publisher:
        return "image"
    if "dark horse" in publisher:
        return "dark_horse"
    if publisher.startswith("idw") or "idw publishing" in publisher:
        return "idw"
    if "boom" in publisher:
        return "boom"
    return ""


def _weekly_pack_queries_enabled(row, wanted_item=None):
    explicit = _policy_value(row, "weekly_pack_queries_enabled", None)
    if explicit is not None:
        return _policy_bool(row, "weekly_pack_queries_enabled", False)
    if _policy_bool(row, "disable_weekly_pack_queries", False):
        return False
    provider_id = inkdrop_sources.provider_key((row or {}).get("provider_id"))
    scope_policy = providers.normalized_query(
        providers.first_text(
            _policy_value(row, "scope_policy", ""),
            _policy_value(row, "media_scope", ""),
        )
    ).lower()
    if provider_id in {"prowlarr_nyaa", "prowlarr_tokyo_toshokan_manga"}:
        return False
    if scope_policy in {"manga", "manga_metadata_or_manga_publisher"}:
        return False
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    media_type = providers.normalized_query(
        providers.first_text(wanted_item.get("media_type"), wanted_item.get("library_type"), wanted_item.get("format"))
    ).lower()
    if "comic" in media_type or "graphic novel" in media_type:
        return True
    return bool(_comic_pack_publisher_key(wanted_item))


def _comic_series_fallback_queries_enabled(row, wanted_item=None):
    for key in ("comic_series_fallback_queries_enabled", "series_fallback_queries_enabled"):
        if _policy_value(row, key, None) is not None:
            return _policy_bool(row, key, False)
    if _policy_bool(row, "disable_comic_series_fallback_queries", False):
        return False
    provider_id = inkdrop_sources.provider_key((row or {}).get("provider_id"))
    adapter_family = str(
        providers.first_text((row or {}).get("adapter_family"), (row or {}).get("source_kind")) or ""
    ).strip().lower()
    prowlarr_comic_lane = bool(
        (provider_id == "prowlarr" or provider_id.startswith("prowlarr_"))
        and adapter_family == "prowlarr_indexer"
    )
    if prowlarr_comic_lane and _proven_singleton_comic_wanted(wanted_item):
        return True
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    media_type = providers.normalized_query(
        providers.first_text(wanted_item.get("media_type"), wanted_item.get("library_type"))
    ).lower()
    if prowlarr_comic_lane and ("comic" in media_type or "graphic novel" in media_type):
        return True
    trusted_comic_pack_providers = {
        "prowlarr_dognzb_comics",
        "prowlarr_torrentleech_comics",
    }
    return bool(provider_id in trusted_comic_pack_providers and _weekly_pack_queries_enabled(row, wanted_item))


def indexer_weekly_pack_queries(row=None, wanted_item=None, *, max_queries=None):
    if not _weekly_pack_queries_enabled(row, wanted_item):
        return []
    limit = providers.int_value(
        max_queries,
        providers.int_value(
            _policy_value(row, "weekly_pack_query_limit", _policy_value(row, "max_weekly_pack_queries", 4)),
            4,
        ),
    )
    limit = max(0, min(int(limit or 0), 8))
    if limit <= 0:
        return []
    metadata_years = _wanted_years(wanted_item, include_current=False)
    years = _wanted_years(wanted_item)
    current_year = time.localtime().tm_year
    date_anchors = _wanted_date_anchors(wanted_item)
    historical_years = _historical_weekly_pack_years(
        wanted_item,
        current_year=current_year,
        broad_max_age_years=_policy_value(row, "weekly_pack_broad_query_max_age_years", 2),
    )
    current_first_years = []
    for year in (current_year, current_year - 1, *years):
        if year not in current_first_years:
            current_first_years.append(year)
    publisher_key = _comic_pack_publisher_key(wanted_item)
    publisher_templates = {
        "dc": ("DC Week", "DC Comics Weekly Releases {year}"),
        "marvel": ("Marvel Week", "Marvel Comics Weekly Releases {year}"),
        "image": ("Image Week", "Image Comics Weekly Releases {year}"),
        "dark_horse": ("Dark Horse Week", "Dark Horse Comics Weekly Releases {year}"),
        "idw": ("IDW Week", "IDW Publishing Weekly Releases {year}"),
        "boom": ("BOOM Week", "BOOM Studios Weekly Releases {year}"),
    }
    templates = list(publisher_templates.get(publisher_key, ()))
    publisher = providers.normalized_query((wanted_item or {}).get("publisher") or "")
    if not templates and publisher:
        templates.append(f"{publisher} Weekly Releases {{year}}")
    generic_year_templates = ("Weekly Comics Pack {year}",)
    generic_static_templates = ("Weekly Comics Pack",)
    out = []
    seen = set()

    def add_query(query):
        text = providers.normalized_query(query)
        key = text.lower()
        if not text or key in seen:
            return False
        seen.add(key)
        out.append(text)
        return len(out) >= limit

    publisher_year_templates = [template for template in templates if "{year}" in template]
    publisher_static_templates = [template for template in templates if "{year}" not in template]
    reserved_historical_year_slots = 1 if historical_years and publisher_year_templates else 0
    historical_without_date_anchor = bool(historical_years and not date_anchors)
    reserved_static_slots = 1 if publisher_static_templates and (not historical_years or historical_without_date_anchor) else 0
    date_query_limit = max(0, limit - len(out) - reserved_historical_year_slots - reserved_static_slots)
    if historical_years:
        historical_date_query_limit = providers.int_value(
            _policy_value(
                row,
                "historical_weekly_pack_date_query_limit",
                _policy_value(row, "weekly_pack_historical_date_query_limit", 3),
            ),
            3,
        )
        date_query_limit = min(date_query_limit, max(0, historical_date_query_limit))
    for query in _weekly_pack_date_queries(
        wanted_item,
        max_queries=date_query_limit,
    ):
        if add_query(query):
            return out

    if not historical_years or historical_without_date_anchor:
        for template in publisher_static_templates:
            if add_query(template):
                return out

    if historical_years and publisher_year_templates:
        for template in publisher_year_templates:
            for query in [template.format(year=year) for year in historical_years]:
                if add_query(query):
                    return out
    elif limit <= 2:
        short_years = metadata_years or [current_year]
        for template in publisher_year_templates:
            for query in [template.format(year=year) for year in short_years]:
                if add_query(query):
                    return out
    else:
        for template in publisher_year_templates:
            if add_query(template.format(year=current_year)):
                return out

    generic_years = historical_years or current_first_years
    for template in generic_year_templates:
        for query in [template.format(year=year) for year in generic_years]:
            if add_query(query):
                return out

    if not historical_years:
        for template in generic_static_templates:
            if add_query(template):
                return out

    for template in publisher_year_templates:
        for query in [template.format(year=year) for year in generic_years]:
            if add_query(query):
                return out
    return out


def _base_url(row, default=""):
    value = str((row or {}).get("base_url") or "").strip().rstrip("/")
    return value or default.rstrip("/")


def _secret_ref(row):
    row = row or {}
    provider_id = inkdrop_sources.provider_key(row.get("provider_id"))
    value = str(row.get("secret_ref") or "").strip()
    source_kind = str(row.get("source_kind") or "").strip().lower()
    adapter_family = str(row.get("adapter_family") or "").strip().lower()
    is_default_placeholder = (
        not value
        or "provider setting" in value.lower()
        or "fallback" in value.lower()
    )
    if provider_id in DEFAULT_SECRET_REFS_BY_PROVIDER_ID and (
        is_default_placeholder
    ):
        return DEFAULT_SECRET_REFS_BY_PROVIDER_ID[provider_id]
    if (
        is_default_placeholder
        and (
            provider_id.startswith("prowlarr_")
            or source_kind == "prowlarr_indexer"
            or adapter_family == "prowlarr_indexer"
        )
    ):
        return DEFAULT_SECRET_REFS_BY_PROVIDER_ID["prowlarr"]
    if not value and str(row.get("source_kind") or "").strip().lower() in {"torznab_indexer", "newznab_indexer"}:
        return f"{provider_id}_api_key" if provider_id else ""
    return value


def _secret_header(row, header_name="X-Api-Key"):
    secret_ref = _secret_ref(row)
    if not secret_ref:
        return {}
    return {header_name: f"<secret_ref:{secret_ref}>"}


def _secret_query_params(row, param_name="apikey"):
    secret_ref = _secret_ref(row)
    if not secret_ref:
        return {}
    return {param_name: f"<secret_ref:{secret_ref}>"}


def _request(
    request_id,
    method,
    url,
    *,
    params=None,
    secret_params=None,
    headers=None,
    json_body=None,
    body=None,
    allowed_hosts=None,
    max_bytes=None,
    purpose="fetch_payload",
):
    out = {
        "request_id": request_id,
        "method": str(method or "GET").upper(),
        "url": str(url or "").strip(),
        "params": dict(params or {}),
        "headers": dict(headers or {}),
        "purpose": purpose,
    }
    if secret_params:
        out["secret_params"] = dict(secret_params or {})
    if json_body is not None:
        out["json"] = json_body
    if body is not None:
        out["body"] = body
    if allowed_hosts:
        out["allowed_hosts"] = list(allowed_hosts)
    if max_bytes not in (None, ""):
        out["max_bytes"] = int(max_bytes)
    return out


def _string_list(value):
    if value in (None, ""):
        return []
    values = value if isinstance(value, (list, tuple, set)) else str(value or "").split(",")
    out = []
    for item in values:
        text = str(item or "").strip()
        if text:
            out.append(text)
    return out


def _prowlarr_indexer_ids(row):
    row = row if isinstance(row, dict) else {}
    policy = row.get("policy") if isinstance(row.get("policy"), dict) else {}
    values = []
    for source in (row, policy):
        for key in ("indexer_ids", "indexerIds", "indexer_id"):
            values.extend(_string_list(source.get(key)))
    out = []
    seen = set()
    for value in values:
        key = value.lower()
        if key not in seen:
            seen.add(key)
            out.append(value)
    return out


def _prowlarr_categoryless_fallback_indexer_ids(row):
    row = row if isinstance(row, dict) else {}
    policy = row.get("policy") if isinstance(row.get("policy"), dict) else {}
    values = []
    for source in (row, policy):
        for key in (
            "categoryless_fallback_indexer_ids",
            "prowlarr_categoryless_fallback_indexer_ids",
        ):
            values.extend(_string_list(source.get(key)))
    configured_ids = _prowlarr_indexer_ids(row)
    configured_keys = {value.lower() for value in configured_ids}
    out = []
    seen = set()
    for value in values:
        key = value.lower()
        if configured_keys and key not in configured_keys:
            continue
        if key not in seen:
            seen.add(key)
            out.append(value)
    return out


def _allowed_language(row, wanted_item=None, default="en"):
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    policy = (row or {}).get("policy") if isinstance((row or {}).get("policy"), dict) else {}
    languages = (
        wanted_item.get("languages")
        or wanted_item.get("language")
        or policy.get("languages")
        or policy.get("allowed_languages")
        or default
    )
    if isinstance(languages, (list, tuple, set)):
        values = [str(value).strip().lower() for value in languages if str(value or "").strip()]
        return ",".join(values)
    return str(languages or default).strip().lower()


def _gutendex_mime_type(row):
    allowed = providers.normalized_extensions((row or {}).get("allowed_extensions") or ((row or {}).get("policy") or {}).get("allowed_extensions") or [])
    if ".epub" in allowed:
        return "application/epub+zip"
    if ".pdf" in allowed:
        return "application/pdf"
    if ".txt" in allowed:
        return "text/plain"
    return "application/epub+zip"


def standard_ebooks_request(row, plan, wanted_item=None, limit=20):
    return _request(
        "standard_ebooks_opds",
        "GET",
        _base_url(row, STANDARD_EBOOKS_OPDS_URL),
        headers={"Accept": "application/atom+xml, application/xml;q=0.9, */*;q=0.1"},
        purpose="fetch_opds_catalog",
    )


def gutendex_request(row, plan, wanted_item=None, limit=20):
    query = source_query(wanted_item)
    params = {
        "copyright": "false",
        "languages": _allowed_language(row, wanted_item, default="en"),
        "mime_type": _gutendex_mime_type(row),
    }
    if query:
        params["search"] = query
    return _request(
        "gutendex_books",
        "GET",
        _base_url(row, GUTENDEX_BOOKS_URL),
        params=params,
        headers={"Accept": "application/json"},
        purpose="search_books",
    )


def internet_archive_search_query(wanted_item=None):
    query = source_query(wanted_item)
    terms = []
    if query:
        escaped = query.replace('"', " ")
        terms.append(f'title:("{escaped}")')
    terms.append("mediatype:texts")
    return " AND ".join(terms)


def internet_archive_search_request(row, plan, wanted_item=None, limit=20):
    count = max(100, int(limit or 0) or 100)
    return _request(
        "internet_archive_search",
        "GET",
        "https://archive.org/services/search/v1/scrape",
        params={
            "q": internet_archive_search_query(wanted_item),
            "fields": "identifier,title,creator,mediatype,collection,licenseurl,rights",
            "sorts": "downloads desc,identifier",
            "count": count,
        },
        headers={"Accept": "application/json"},
        purpose="search_archive_items",
    )


def internet_archive_metadata_request(identifier):
    identifier = str(identifier or "").strip()
    return _request(
        "internet_archive_metadata",
        "GET",
        f"{INTERNET_ARCHIVE_METADATA_BASE}/{quote(identifier, safe='')}",
        headers={"Accept": "application/json", "Accept-Encoding": "gzip, deflate"},
        purpose="fetch_archive_item_metadata",
    )


def _policy_list(row, key, default):
    wanted = (row or {}).get("policy") if isinstance((row or {}).get("policy"), dict) else {}
    value = wanted.get(key)
    if isinstance(value, str):
        rows = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, (list, tuple, set)):
        rows = [str(part).strip() for part in value if str(part or "").strip()]
    else:
        rows = []
    return rows or list(default)


def _mangadex_languages(row, wanted_item=None):
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    language = wanted_item.get("language") or wanted_item.get("translated_language")
    if isinstance(language, (list, tuple, set)):
        wanted = [str(item).strip().lower() for item in language if str(item or "").strip()]
    elif language:
        wanted = [str(language).strip().lower()]
    else:
        wanted = []
    return wanted or [item.lower() for item in _policy_list(row, "allowed_languages", ["en"])]


def _mangadex_content_ratings(row):
    return _policy_list(row, "content_ratings", MANGADEX_DEFAULT_CONTENT_RATINGS)


def _mangadex_allowed_hosts(row):
    hosts = list(_policy_host_list(row, ("mangadex_allowed_hosts", "source_allowed_hosts")))
    parsed = urlparse(_base_url(row, MANGADEX_API_BASE))
    host = str(parsed.hostname or "").strip().lower().strip("[]")
    if host and host not in hosts:
        hosts.append(host)
    return hosts


def mangadex_series_query(wanted_item=None):
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    return providers.normalized_query(
        wanted_item.get("series_title")
        or wanted_item.get("series")
        or wanted_item.get("manga_title")
        or wanted_item.get("title")
        or wanted_item.get("query")
        or ""
    )


def _mangadex_clean_query(value, wanted_item=None):
    text = providers.normalized_query(value)
    if not text:
        return ""
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    issue = providers.first_text(
        wanted_item.get("issue_number"),
        wanted_item.get("normalized_number"),
        wanted_item.get("chapter_number"),
        wanted_item.get("chapter"),
        wanted_item.get("number"),
    )
    text = re.sub(r"\s*\((?:19|20)\d{2}\)\s*$", "", text).strip()
    text = re.sub(r"\s+(?:19|20)\d{2}$", "", text).strip()
    text = re.sub(
        r"\s+\b(?:vol(?:ume)?|v|chapter|chap|ch|issue|#)\.?\s*\d+(?:\.\d+)?\b.*$",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()
    if issue:
        issue_pattern = re.escape(str(issue).strip())
        stripped = re.sub(rf"\s+{issue_pattern}$", "", text).strip()
        if stripped and re.search(r"[A-Za-z]", stripped):
            text = stripped
    return providers.normalized_query(text)


def mangadex_series_query_pool(wanted_item=None, *, policy=None):
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    values = []
    seen = set()

    def add(value, *, include_ascii=False):
        query = providers.normalized_query(value)
        key = query.lower()
        if query and key not in seen:
            seen.add(key)
            values.append(query)
        if include_ascii:
            folded = _query_ascii_fold(query)
            folded_key = folded.lower()
            if folded and folded_key not in seen:
                seen.add(folded_key)
                values.append(folded)

    seriesish_count = len(values)
    for key in ("series_title", "series", "manga_title"):
        value = wanted_item.get(key)
        add(value, include_ascii=True)
        add(_query_without_leading_creator_possessive(value), include_ascii=True)
    for alias in _series_query_aliases(wanted_item, policy=policy):
        add(alias, include_ascii=True)
    if len(values) == seriesish_count:
        add(wanted_item.get("title"), include_ascii=True)
    raw_query = providers.first_text(
        wanted_item.get("searchQuery"),
        wanted_item.get("search_query"),
        wanted_item.get("query"),
    )
    clean_query = _mangadex_clean_query(raw_query, wanted_item)
    add(clean_query, include_ascii=True)
    add(_query_without_leading_creator_possessive(clean_query), include_ascii=True)
    add(raw_query, include_ascii=True)
    add(_query_without_leading_creator_possessive(raw_query), include_ascii=True)
    volume_variants = _volume_number_variants(_wanted_indexer_volume_number(wanted_item))
    if volume_variants and not _query_has_volume_number(raw_query, volume_variants[0]):
        stems = []
        for value in list(values):
            stem = _mangadex_clean_query(value, wanted_item)
            if stem and stem not in stems:
                stems.append(stem)
            creator_stem = _query_without_leading_creator_possessive(stem)
            if creator_stem and creator_stem not in stems:
                stems.append(creator_stem)
        for stem in stems:
            for volume in volume_variants[:1]:
                add(f"{stem} Vol. {volume}", include_ascii=True)
                add(f"{stem} Volume {volume}", include_ascii=True)
                add(f"{stem} v{volume}", include_ascii=True)
            for volume in volume_variants[1:]:
                add(f"{stem} v{volume}", include_ascii=True)
    return _prefer_ascii_folded_volume_queries(values, wanted_item)[:6]


def mangadex_series_queries(wanted_item=None, *, max_queries=3, policy=None):
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    try:
        limit = int(max_queries or 3)
    except (TypeError, ValueError):
        limit = 3
    limit = max(1, min(limit, 6))
    values = mangadex_series_query_pool(wanted_item, policy=policy)
    try:
        query_offset = int(wanted_item.get("_mangadex_query_offset") or 0)
    except (TypeError, ValueError):
        query_offset = 0
    if values and query_offset:
        query_offset %= len(values)
        values = [*values[query_offset:], *values[:query_offset]]
    return values[:limit]


def mangadex_search_request(row, plan, wanted_item=None, limit=20, *, query=None, request_id="mangadex_manga_search"):
    query = providers.normalized_query(query or mangadex_series_query(wanted_item))
    params = {
        "title": query,
        "limit": max(1, min(int(limit or 10), 10)),
        "availableTranslatedLanguage[]": _mangadex_languages(row, wanted_item),
        "contentRating[]": _mangadex_content_ratings(row),
        "includes[]": ["author", "artist"],
        "order[relevance]": "desc",
    }
    return _request(
        request_id,
        "GET",
        f"{_base_url(row, MANGADEX_API_BASE)}/manga",
        params=params,
        headers={"Accept": "application/json"},
        allowed_hosts=_mangadex_allowed_hosts(row),
        purpose="search_mangadex_manga",
    )


def mangadex_search_requests(row, plan, wanted_item=None, limit=20):
    max_queries = providers.int_value(
        _policy_value(row, "mangadex_max_query_variants", _policy_value(row, "max_query_variants", 3)),
        3,
    )
    requests = []
    for index, query in enumerate(
        mangadex_series_queries(
            wanted_item,
            max_queries=max_queries,
            policy=_source_query_alias_policy(row),
        )
    ):
        requests.append(
            mangadex_search_request(
                row,
                plan,
                wanted_item,
                limit=limit,
                query=query,
                request_id="mangadex_manga_search" if index == 0 else f"mangadex_manga_search_{index + 1}",
            )
        )
    return requests


def _mangadex_number_filter(value):
    text = providers.normalized_query(str(value or ""))
    if not text:
        return ""
    try:
        numeric = float(text)
    except Exception:
        return text
    if numeric.is_integer():
        return str(int(numeric))
    return text


def _mangadex_wanted_unit_type(wanted_item=None):
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    return str(
        providers.first_text(
            wanted_item.get("unitType"),
            wanted_item.get("unit_type"),
            wanted_item.get("unit"),
        )
    ).strip().lower()


def _mangadex_wanted_volume_number(wanted_item=None):
    return _mangadex_number_filter(_wanted_indexer_volume_number(wanted_item))


def _mangadex_wanted_chapter_number(wanted_item=None):
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    explicit = providers.first_text(wanted_item.get("chapter"), wanted_item.get("chapter_number"))
    if explicit:
        return _mangadex_number_filter(explicit)
    if _mangadex_wanted_unit_type(wanted_item) in {"volume", "vol", "book_volume", "manga_volume"}:
        return ""
    return _mangadex_number_filter(
        providers.first_text(
            wanted_item.get("issue_number"),
            wanted_item.get("normalized_number"),
            wanted_item.get("number"),
        )
    )


def mangadex_feed_request(manga_id, row, plan, wanted_item=None, limit=100, *, offset=0):
    manga_id = str(manga_id or "").strip()
    params = {
        "translatedLanguage[]": _mangadex_languages(row, wanted_item),
        "contentRating[]": _mangadex_content_ratings(row),
        "includes[]": ["scanlation_group"],
        "order[chapter]": "asc",
        "limit": max(1, min(int(limit or 100), 100)),
    }
    try:
        offset_value = int(offset or 0)
    except (TypeError, ValueError):
        offset_value = 0
    if offset_value > 0:
        params["offset"] = offset_value
    return _request(
        "mangadex_manga_feed",
        "GET",
        f"{_base_url(row, MANGADEX_API_BASE)}/manga/{quote(manga_id, safe='')}/feed",
        params=params,
        headers={"Accept": "application/json"},
        allowed_hosts=_mangadex_allowed_hosts(row),
        purpose="fetch_mangadex_manga_feed",
    )


def _mangadex_feed_page_limit(row, fallback=100):
    value = providers.int_value(_policy_value(row, "mangadex_feed_page_size", 100), 100)
    return max(1, min(int(value or fallback or 100), 100))


def _mangadex_feed_max_pages(row):
    value = providers.int_value(_policy_value(row, "mangadex_feed_max_pages", 3), 3)
    return max(1, min(int(value or 3), 10))


def _mangadex_feed_rows(feed_payload):
    if isinstance(feed_payload, dict) and isinstance(feed_payload.get("data"), list):
        return feed_payload.get("data") or []
    return []


def _mangadex_feed_total(feed_payload):
    if not isinstance(feed_payload, dict):
        return 0
    return providers.int_value(feed_payload.get("total"), 0) or 0


def mangadex_at_home_request(chapter_id, row, plan=None):
    chapter_id = str(chapter_id or "").strip()
    return _request(
        "mangadex_at_home_server",
        "GET",
        f"{_base_url(row, MANGADEX_API_BASE)}/at-home/server/{quote(chapter_id, safe='')}",
        headers={"Accept": "application/json"},
        allowed_hosts=_mangadex_allowed_hosts(row),
        purpose="fetch_mangadex_at_home_pages",
    )


def _source_query_alias_policy(row):
    row = row if isinstance(row, dict) else {}
    policy = dict(row.get("policy") or {}) if isinstance(row.get("policy"), dict) else {}
    for key in ("series_query_aliases", "query_aliases", "title_query_aliases", "series_title_aliases"):
        value = row.get(key)
        if value not in (None, "", [], {}):
            policy[key] = value
    return policy


def suwayomi_series_queries(wanted_item=None, *, max_queries=3, policy=None):
    try:
        limit = int(max_queries or 3)
    except (TypeError, ValueError):
        limit = 3
    limit = max(1, min(limit, 6))
    base_values = mangadex_series_queries(wanted_item, max_queries=limit)
    aliases = _series_query_aliases(wanted_item, policy=policy)
    if not aliases:
        return base_values[:limit]
    values = []
    seen = set()

    def add(value, *, include_ascii=False):
        query = providers.normalized_query(value)
        key = query.lower()
        if query and key not in seen:
            seen.add(key)
            values.append(query)
        if include_ascii:
            folded = _query_ascii_fold(query)
            folded_key = folded.lower()
            if folded and folded_key not in seen:
                seen.add(folded_key)
                values.append(folded)

    if base_values:
        add(base_values[0])
    for alias in aliases:
        add(alias, include_ascii=True)
    for value in base_values[1:]:
        add(value)
    return values[:limit]


def _suwayomi_base_url(row):
    return _base_url(row, SUWAYOMI_API_BASE)


def _suwayomi_allowed_hosts(row):
    hosts = list(_policy_host_list(row, ("suwayomi_allowed_hosts", "source_allowed_hosts")))
    parsed = urlparse(_suwayomi_base_url(row))
    host = str(parsed.hostname or "").strip().lower().strip("[]")
    if host and host not in hosts:
        hosts.append(host)
    return hosts


def suwayomi_source_list_request(row, plan=None):
    return _request(
        "suwayomi_source_list",
        "GET",
        f"{_suwayomi_base_url(row)}/api/v1/source/list",
        headers={"Accept": "application/json"},
        allowed_hosts=_suwayomi_allowed_hosts(row),
        purpose="fetch_suwayomi_sources",
    )


def suwayomi_extension_list_request(row, plan=None):
    return _request(
        "suwayomi_extension_list",
        "GET",
        f"{_suwayomi_base_url(row)}/api/v1/extension/list",
        headers={"Accept": "application/json"},
        allowed_hosts=_suwayomi_allowed_hosts(row),
        purpose="fetch_suwayomi_extensions",
    )


def _suwayomi_source_name_key(value):
    return inkdrop_sources.normalize_title(value)


def _suwayomi_configured_source_ids(row):
    values = []
    for key in ("suwayomi_source_ids", "source_ids", "source_id"):
        values.extend(_policy_list(row, key, []))
    out = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _suwayomi_configured_source_names(row):
    values = []
    for key in ("suwayomi_source_names", "source_names", "source_name"):
        values.extend(_policy_list(row, key, []))
    out = []
    seen = set()
    for value in values:
        key = _suwayomi_source_name_key(value)
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _suwayomi_disabled_source_ids(row):
    values = []
    for key in ("suwayomi_disabled_source_ids", "disabled_source_ids", "excluded_source_ids"):
        values.extend(_policy_list(row, key, []))
    out = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _suwayomi_disabled_source_names(row):
    values = []
    for key in ("suwayomi_disabled_source_names", "disabled_source_names", "excluded_source_names"):
        values.extend(_policy_list(row, key, []))
    out = []
    seen = set()
    for value in values:
        key = _suwayomi_source_name_key(value)
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _suwayomi_source_cooldown_ids(row):
    values = []
    for key in ("suwayomi_source_cooldown_ids", "source_cooldown_ids"):
        values.extend(_policy_list(row, key, []))
    out = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _suwayomi_source_cooldown_names(row):
    values = []
    for key in ("suwayomi_source_cooldown_names", "source_cooldown_names"):
        values.extend(_policy_list(row, key, []))
    out = []
    seen = set()
    for value in values:
        key = _suwayomi_source_name_key(value)
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _suwayomi_source_cooldown_reason(row, source_row):
    source_row = source_row if isinstance(source_row, dict) else {}
    source_id = str(source_row.get("id") or source_row.get("sourceId") or "").strip()
    by_id = _policy_value(row, "suwayomi_source_cooldown_reasons_by_id", {})
    by_name = _policy_value(row, "suwayomi_source_cooldown_reasons_by_name", {})
    reason = ""
    if isinstance(by_id, dict) and source_id:
        reason = str(by_id.get(source_id) or "").strip()
    if not reason and isinstance(by_name, dict):
        name_reason_map = {
            _suwayomi_source_name_key(key): str(value or "").strip()
            for key, value in by_name.items()
            if str(key or "").strip()
        }
        for key in _suwayomi_source_name_keys(source_row):
            reason = name_reason_map.get(key, "")
            if reason:
                break
    if reason == "volume_evidence_gap_cooldown":
        return reason
    return "source_error_cooldown"


def _suwayomi_source_cooldown_probe_ids(row):
    values = []
    for key in ("suwayomi_source_cooldown_probe_ids", "source_cooldown_probe_ids"):
        values.extend(_policy_list(row, key, []))
    out = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _suwayomi_source_cooldown_probe_names(row):
    values = []
    for key in ("suwayomi_source_cooldown_probe_names", "source_cooldown_probe_names"):
        values.extend(_policy_list(row, key, []))
    out = []
    seen = set()
    for value in values:
        key = _suwayomi_source_name_key(value)
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _suwayomi_source_name_keys(source_row):
    source_row = source_row if isinstance(source_row, dict) else {}
    values = [
        source_row.get("displayName"),
        _suwayomi_display_base_name(source_row.get("displayName")),
        source_row.get("name"),
        source_row.get("baseUrl"),
    ]
    return {_suwayomi_source_name_key(value) for value in values if str(value or "").strip()}


def _suwayomi_source_rows(payload):
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = []
        for key in ("sourceList", "sources", "data", "items", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                rows = value
                break
    else:
        rows = []
    return [row for row in rows if isinstance(row, dict)]


def _suwayomi_extension_rows(payload):
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = []
        for key in ("extensionList", "extensions", "data", "items", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                rows = value
                break
    else:
        rows = []
    return [row for row in rows if isinstance(row, dict)]


def _suwayomi_display_base_name(value):
    text = str(value or "").strip()
    return re.sub(r"\s*\([^)]+\)\s*$", "", text).strip()


def _suwayomi_source_extension_keys(source_row):
    source_row = source_row if isinstance(source_row, dict) else {}
    keys = []
    icon = str(source_row.get("iconUrl") or "").strip()
    marker = "/extension/icon/"
    if marker in icon:
        keys.append(icon.rsplit(marker, 1)[-1].strip())
    for key in ("pkgName", "extensionPkgName", "extensionPackage", "extensionId"):
        value = str(source_row.get(key) or "").strip()
        if value:
            keys.append(value)
    for key in ("name", "displayName"):
        value = _suwayomi_display_base_name(source_row.get(key))
        if value:
            keys.append(_suwayomi_source_name_key(value))
    out = []
    seen = set()
    for key in keys:
        text = str(key or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _suwayomi_extension_index(payload):
    index = {}
    for row in _suwayomi_extension_rows(payload):
        keys = []
        for key in ("pkgName", "packageName", "extensionId", "id"):
            value = str(row.get(key) or "").strip()
            if value:
                keys.append(value)
        name = _suwayomi_display_base_name(row.get("name") or row.get("displayName"))
        if name:
            keys.append(_suwayomi_source_name_key(name))
        for key in keys:
            if key and key not in index:
                index[key] = row
    return index


def _suwayomi_extension_health(row):
    row = row if isinstance(row, dict) else {}
    if not row:
        return {}
    out = {
        "name": row.get("name") or row.get("displayName"),
        "pkgName": row.get("pkgName") or row.get("packageName") or row.get("id"),
        "versionName": row.get("versionName"),
        "versionCode": row.get("versionCode") or row.get("versionCodeLong"),
        "lang": row.get("lang"),
        "installed": row.get("installed") if row.get("installed") is not None else row.get("isInstalled"),
        "hasUpdate": row.get("hasUpdate"),
        "obsolete": row.get("obsolete") if row.get("obsolete") is not None else row.get("isObsolete"),
        "isNsfw": row.get("isNsfw") if row.get("isNsfw") is not None else row.get("nsfw"),
        "repo": row.get("repo") or row.get("storeIndexUrl"),
    }
    return {key: value for key, value in out.items() if value not in (None, "", [], {})}


def _suwayomi_bool_flag(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _suwayomi_source_is_nsfw(source_row):
    source_row = source_row if isinstance(source_row, dict) else {}
    health = source_row.get("extension_health") if isinstance(source_row.get("extension_health"), dict) else {}
    for key in ("isNsfw", "nsfw", "isAdult", "adult", "extension_is_nsfw"):
        if _suwayomi_bool_flag(source_row.get(key)):
            return True
    return _suwayomi_bool_flag(health.get("isNsfw") or health.get("nsfw"))


def _suwayomi_annotate_source_rows(source_payload, extension_payload=None):
    rows = [dict(row) for row in _suwayomi_source_rows(source_payload)]
    extension_index = _suwayomi_extension_index(extension_payload)
    if not extension_index:
        return rows
    for source_row in rows:
        extension = {}
        for key in _suwayomi_source_extension_keys(source_row):
            extension = extension_index.get(key) or {}
            if extension:
                break
        health = _suwayomi_extension_health(extension)
        if health:
            source_row["extension_health"] = health
            source_row["extension_pkg_name"] = health.get("pkgName")
            source_row["extension_obsolete"] = bool(health.get("obsolete"))
            source_row["extension_has_update"] = bool(health.get("hasUpdate"))
            if health.get("versionName"):
                source_row["extension_version_name"] = health.get("versionName")
            if health.get("isNsfw") is not None:
                source_row["extension_is_nsfw"] = _suwayomi_bool_flag(health.get("isNsfw"))
    return rows


def _suwayomi_extension_health_summary(extension_payload, sources=None):
    extension_rows = _suwayomi_extension_rows(extension_payload)
    selected = []
    for source_row in sources or []:
        if not isinstance(source_row, dict):
            continue
        health = source_row.get("extension_health") if isinstance(source_row.get("extension_health"), dict) else {}
        if not health:
            continue
        selected.append(
            {
                key: value
                for key, value in {
                    "source_id": source_row.get("id") or source_row.get("sourceId"),
                    "source_name": source_row.get("displayName") or source_row.get("name"),
                    "pkgName": health.get("pkgName"),
                    "versionName": health.get("versionName"),
                    "installed": health.get("installed"),
                    "hasUpdate": health.get("hasUpdate"),
                    "obsolete": health.get("obsolete"),
                    "isNsfw": health.get("isNsfw"),
                }.items()
                if value not in (None, "", [], {})
            }
        )
    return {
        key: value
        for key, value in {
            "extension_count": len(extension_rows),
            "installed_count": sum(1 for row in extension_rows if bool(row.get("installed"))),
            "obsolete_count": sum(1 for row in extension_rows if bool(row.get("obsolete"))),
            "update_count": sum(1 for row in extension_rows if bool(row.get("hasUpdate"))),
            "nsfw_count": sum(1 for row in extension_rows if _suwayomi_bool_flag(row.get("isNsfw") or row.get("nsfw"))),
            "selected_sources": selected[:20],
        }.items()
        if value not in (None, "", [], {})
    }


def _suwayomi_source_sort_key(source_row):
    source_row = source_row if isinstance(source_row, dict) else {}
    health = source_row.get("extension_health") if isinstance(source_row.get("extension_health"), dict) else {}
    obsolete = bool(health.get("obsolete") or source_row.get("extension_obsolete"))
    has_update = bool(health.get("hasUpdate") or source_row.get("extension_has_update"))
    return (1 if obsolete else 0, 1 if has_update else 0)


def _suwayomi_source_summary(source_row, *, reason=""):
    source_row = source_row if isinstance(source_row, dict) else {}
    out = {
        "source_id": source_row.get("id") or source_row.get("sourceId"),
        "source_name": source_row.get("displayName") or source_row.get("name"),
        "source_base_name": _suwayomi_display_base_name(source_row.get("displayName") or source_row.get("name")),
        "lang": source_row.get("lang"),
        "is_nsfw": _suwayomi_source_is_nsfw(source_row),
        "extension_pkg_name": source_row.get("extension_pkg_name"),
        "extension_obsolete": source_row.get("extension_obsolete"),
        "extension_has_update": source_row.get("extension_has_update"),
        "extension_is_nsfw": source_row.get("extension_is_nsfw"),
        "cooldown_probe": bool(source_row.get("suwayomi_source_error_cooldown_probe")),
        "cooldown_probe_reason": source_row.get("suwayomi_source_error_cooldown_probe_reason"),
        "reason": reason,
    }
    return {key: value for key, value in out.items() if value not in (None, "", [], {})}


def _suwayomi_source_allowed_status(row, source_row, wanted_item=None, *, allow_source_error_cooldown=False):
    source_row = source_row if isinstance(source_row, dict) else {}
    source_id = str(source_row.get("id") or source_row.get("sourceId") or "").strip()
    if source_id == "0":
        return False, "invalid_source_id"
    allowed_languages = {str(value or "").strip().lower() for value in _mangadex_languages(row, wanted_item)}
    language = str(source_row.get("lang") or "").strip().lower()
    if allowed_languages and language and language not in allowed_languages:
        return False, "language_not_allowed"
    if _suwayomi_source_is_nsfw(source_row) and not _policy_bool(row, "suwayomi_allow_nsfw_sources", True):
        return False, "nsfw_source_not_allowed"
    source_name_keys = _suwayomi_source_name_keys(source_row)
    disabled_ids = set(_suwayomi_disabled_source_ids(row))
    disabled_names = set(_suwayomi_disabled_source_names(row))
    if source_id in disabled_ids:
        return False, "disabled_source_id"
    if source_name_keys.intersection(disabled_names):
        return False, "disabled_source_name"
    cooldown_ids = set(_suwayomi_source_cooldown_ids(row))
    cooldown_names = set(_suwayomi_source_cooldown_names(row))
    if source_id in cooldown_ids and not allow_source_error_cooldown:
        return False, _suwayomi_source_cooldown_reason(row, source_row)
    if source_name_keys.intersection(cooldown_names) and not allow_source_error_cooldown:
        return False, _suwayomi_source_cooldown_reason(row, source_row)
    health = source_row.get("extension_health") if isinstance(source_row.get("extension_health"), dict) else {}
    obsolete = bool(health.get("obsolete") or source_row.get("extension_obsolete"))
    has_update = bool(health.get("hasUpdate") or source_row.get("extension_has_update"))
    if obsolete and _policy_bool(row, "suwayomi_skip_obsolete_sources", False):
        return False, "obsolete_extension"
    if has_update and _policy_bool(row, "suwayomi_skip_sources_with_updates", False):
        return False, "extension_update_available"
    configured_ids = set(_suwayomi_configured_source_ids(row))
    configured_names = set(_suwayomi_configured_source_names(row))
    if configured_ids or configured_names:
        if source_id in configured_ids or source_name_keys.intersection(configured_names):
            return True, ""
        return False, "not_configured"
    return True, ""


def _suwayomi_source_allowed(row, source_row, wanted_item=None):
    allowed, _reason = _suwayomi_source_allowed_status(row, source_row, wanted_item)
    return allowed


def _suwayomi_source_cooldown_probe_rank(row, source_row):
    source_row = source_row if isinstance(source_row, dict) else {}
    source_id = str(source_row.get("id") or source_row.get("sourceId") or "").strip()
    probe_ids = _suwayomi_source_cooldown_probe_ids(row)
    if source_id and source_id in probe_ids:
        return probe_ids.index(source_id)
    probe_names = _suwayomi_source_cooldown_probe_names(row)
    source_name_keys = _suwayomi_source_name_keys(source_row)
    for index, name_key in enumerate(probe_names):
        if name_key in source_name_keys:
            return len(probe_ids) + index
    return len(probe_ids) + len(probe_names) + 999


def _suwayomi_source_error_cooldown_probe_rows(row, payload, wanted_item=None, *, probe_reason="all_active_sources_cooled"):
    if not _policy_bool(row, "suwayomi_source_error_cooldown_probe_enabled", True):
        return []
    probe_ids = set(_suwayomi_source_cooldown_probe_ids(row))
    probe_names = set(_suwayomi_source_cooldown_probe_names(row))
    if not (probe_ids or probe_names):
        return []
    rows = []
    seen = set()
    for source_row in _suwayomi_source_rows(payload):
        source_row = dict(source_row or {})
        source_id = str(source_row.get("id") or source_row.get("sourceId") or "").strip()
        if not source_id or source_id in seen:
            continue
        source_name_keys = _suwayomi_source_name_keys(source_row)
        if source_id not in probe_ids and not source_name_keys.intersection(probe_names):
            continue
        normal_allowed, normal_reason = _suwayomi_source_allowed_status(row, source_row, wanted_item)
        if normal_allowed or normal_reason not in {"source_error_cooldown", "volume_evidence_gap_cooldown"}:
            continue
        allowed_without_cooldown, _reason = _suwayomi_source_allowed_status(
            row,
            source_row,
            wanted_item,
            allow_source_error_cooldown=True,
        )
        if not allowed_without_cooldown:
            continue
        source_row["suwayomi_source_error_cooldown_probe"] = True
        source_row["suwayomi_source_error_cooldown_probe_reason"] = probe_reason or "all_active_sources_cooled"
        seen.add(source_id)
        rows.append(source_row)
    rows = sorted(
        enumerate(rows),
        key=lambda item: (
            _suwayomi_source_cooldown_probe_rank(row, item[1]),
            _suwayomi_configured_source_rank(row, item[1], item[0]),
            _suwayomi_source_sort_key(item[1]),
        ),
    )
    max_probe_sources = providers.int_value(
        _policy_value(
            row,
            "suwayomi_source_cooldown_probe_max_sources",
            _policy_value(row, "suwayomi_source_error_cooldown_probe_max_sources", 2),
        ),
        2,
    )
    max_probe_sources = max(1, min(max_probe_sources or 1, providers.int_value(_policy_value(row, "suwayomi_max_source_count", 5), 5) or 5, 20))
    return [source for _, source in rows[:max_probe_sources]]


def _suwayomi_source_selection_summary(row, payload, sources, wanted_item=None):
    selected_ids = {
        str((source or {}).get("id") or (source or {}).get("sourceId") or "").strip()
        for source in sources or []
        if isinstance(source, dict)
    }
    selected = [_suwayomi_source_summary(source) for source in sources or [] if isinstance(source, dict)]
    skipped = []
    for source_row in _suwayomi_source_rows(payload):
        source_id = str(source_row.get("id") or source_row.get("sourceId") or "").strip()
        if source_id in selected_ids:
            continue
        _allowed, reason = _suwayomi_source_allowed_status(row, source_row, wanted_item)
        if reason in {
            "disabled_source_id",
            "disabled_source_name",
            "source_error_cooldown",
            "volume_evidence_gap_cooldown",
            "nsfw_source_not_allowed",
            "obsolete_extension",
            "extension_update_available",
        }:
            skipped.append(_suwayomi_source_summary(source_row, reason=reason))
    out = {
        "selected_count": len(selected),
        "selected_sources": selected[:20],
        "cooldown_probe_count": sum(1 for source in sources or [] if isinstance(source, dict) and source.get("suwayomi_source_error_cooldown_probe")),
        "skipped_count": len(skipped),
        "skipped_sources": skipped[:20],
    }
    return {key: value for key, value in out.items() if value not in (None, "", [], {})}


def _suwayomi_configured_source_rank(row, source_row, fallback_index=0):
    source_row = source_row if isinstance(source_row, dict) else {}
    source_id = str(source_row.get("id") or source_row.get("sourceId") or "").strip()
    configured_ids = _suwayomi_configured_source_ids(row)
    if source_id and source_id in configured_ids:
        return configured_ids.index(source_id)
    configured_names = _suwayomi_configured_source_names(row)
    source_name_keys = _suwayomi_source_name_keys(source_row)
    for index, name_key in enumerate(configured_names):
        if name_key in source_name_keys:
            return len(configured_ids) + index
    return len(configured_ids) + len(configured_names) + int(fallback_index or 0)


def _suwayomi_selected_sources(row, payload, wanted_item=None):
    rows = []
    seen = set()
    max_sources = providers.int_value(_policy_value(row, "suwayomi_max_source_count", 5), 5)
    max_sources = max(1, min(max_sources or 5, 20))
    configured_ids = set(_suwayomi_configured_source_ids(row))
    configured_names = set(_suwayomi_configured_source_names(row))
    for source_row in _suwayomi_source_rows(payload):
        source_id = str(source_row.get("id") or source_row.get("sourceId") or "").strip()
        if not source_id or source_id in seen:
            continue
        if not _suwayomi_source_allowed(row, source_row, wanted_item):
            continue
        seen.add(source_id)
        rows.append(source_row)
    if not (configured_ids or configured_names):
        rows = sorted(rows, key=_suwayomi_source_sort_key)
    else:
        rows = sorted(
            enumerate(rows),
            key=lambda item: (_suwayomi_configured_source_rank(row, item[1], item[0]), _suwayomi_source_sort_key(item[1])),
        )
        rows = [row for _, row in rows]
    probe_reason = "partial_source_pool_capacity" if rows else "all_active_sources_cooled"
    if len(rows) < max_sources:
        for source_row in _suwayomi_source_error_cooldown_probe_rows(
            row,
            payload,
            wanted_item,
            probe_reason=probe_reason,
        ):
            source_id = str(source_row.get("id") or source_row.get("sourceId") or "").strip()
            if not source_id or source_id in seen:
                continue
            seen.add(source_id)
            rows.append(source_row)
            if len(rows) >= max_sources:
                break
    return rows[:max_sources]


def suwayomi_search_request(row, plan, wanted_item=None, limit=20, *, source_row=None, query=None, request_id="suwayomi_source_search"):
    source_row = source_row if isinstance(source_row, dict) else {}
    source_id = str(source_row.get("id") or source_row.get("sourceId") or "").strip()
    query = providers.normalized_query(query or mangadex_series_query(wanted_item))
    page = providers.int_value(_policy_value(row, "suwayomi_search_page", 1), 1)
    request = _request(
        request_id,
        "GET",
        f"{_suwayomi_base_url(row)}/api/v1/source/{quote(source_id, safe='')}/search",
        params={"searchTerm": query, "page": max(1, page or 1)},
        headers={"Accept": "application/json"},
        allowed_hosts=_suwayomi_allowed_hosts(row),
        purpose="search_suwayomi_source",
    )
    request["source_id"] = source_id
    request["source_display_name"] = str(source_row.get("displayName") or source_row.get("name") or source_id)
    request["source_lang"] = str(source_row.get("lang") or "")
    if isinstance(source_row.get("extension_health"), dict):
        request["source_extension_health"] = dict(source_row.get("extension_health") or {})
    if source_row.get("extension_pkg_name"):
        request["source_extension_pkg_name"] = source_row.get("extension_pkg_name")
    if source_row.get("extension_obsolete") not in (None, ""):
        request["source_extension_obsolete"] = bool(source_row.get("extension_obsolete"))
    if source_row.get("extension_has_update") not in (None, ""):
        request["source_extension_has_update"] = bool(source_row.get("extension_has_update"))
    if _suwayomi_source_is_nsfw(source_row):
        request["source_is_nsfw"] = True
    if source_row.get("suwayomi_source_error_cooldown_probe"):
        request["source_error_cooldown_probe"] = True
        request["source_error_cooldown_probe_reason"] = source_row.get("suwayomi_source_error_cooldown_probe_reason") or "all_active_sources_cooled"
    request["query_variant"] = query
    return request


def suwayomi_search_requests(row, plan, wanted_item=None, limit=20, *, sources=None):
    max_queries = providers.int_value(
        _policy_value(row, "suwayomi_max_query_variants", _policy_value(row, "max_query_variants", 3)),
        3,
    )
    queries = suwayomi_series_queries(wanted_item, max_queries=max_queries, policy=_source_query_alias_policy(row))
    requests = []
    source_rows = [source for source in sources or [] if isinstance(source, dict)]
    for query_index, query in enumerate(queries):
        for source_index, source_row in enumerate(source_rows):
            suffix = f"{source_index + 1}_{query_index + 1}"
            requests.append(
                suwayomi_search_request(
                    row,
                    plan,
                    wanted_item,
                    limit=limit,
                    source_row=source_row,
                    query=query,
                    request_id="suwayomi_source_search" if not requests else f"suwayomi_source_search_{suffix}",
                )
            )
    return requests


def suwayomi_estimated_request_count(row, wanted_item=None):
    configured_source_count = len(_suwayomi_configured_source_ids(row)) + len(_suwayomi_configured_source_names(row))
    max_sources = providers.int_value(_policy_value(row, "suwayomi_max_source_count", 5), 5)
    max_sources = max(1, min(max_sources or 5, 20))
    source_count = max(1, min(configured_source_count or max_sources, max_sources))
    max_queries = providers.int_value(
        _policy_value(row, "suwayomi_max_query_variants", _policy_value(row, "max_query_variants", 3)),
        3,
    )
    query_count = len(
        suwayomi_series_queries(
            wanted_item,
            max_queries=max_queries,
            policy=_source_query_alias_policy(row),
        )
    )
    query_count = max(1, min(query_count or max_queries or 1, max(1, int(max_queries or 1))))
    max_manga_matches = providers.int_value(_policy_value(row, "suwayomi_max_manga_matches", 3), 3)
    max_manga_matches = max(1, min(max_manga_matches or 3, 10))
    max_chapters = providers.int_value(_policy_value(row, "suwayomi_max_chapters", 3), 3)
    max_chapters = max(1, min(max_chapters or 3, 20))
    volume_pack = (
        providers.volume_page_pack_enabled(row)
        and providers.wanted_item_is_volume_unit(wanted_item)
        and not _policy_bool(row, "allow_single_chapter_volume_page_pack", False)
    )
    chapter_budget = providers.volume_page_pack_max_chapters(row) if volume_pack else max_chapters
    rest_fallback_budget = max_manga_matches if _policy_bool(row, "suwayomi_rest_chapter_fallback_enabled", True) else 0
    estimate = 2 + (source_count * query_count) + max_manga_matches + rest_fallback_budget + chapter_budget
    cap = 80 if volume_pack else 20
    return max(2, min(int(estimate), cap))


def suwayomi_fetch_manga_and_chapters_request(manga_id, row, plan=None, *, include_meta=True):
    manga_id = providers.int_value(manga_id, None)
    if manga_id is None:
        return None
    request_id = "suwayomi_fetch_manga_and_chapters" if include_meta else "suwayomi_fetch_manga_and_chapters_no_meta"
    return _request(
        request_id,
        "POST",
        f"{_suwayomi_base_url(row)}/api/graphql",
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        json_body={
            "query": SUWAYOMI_FETCH_MANGA_AND_CHAPTERS_MUTATION
            if include_meta
            else SUWAYOMI_FETCH_MANGA_AND_CHAPTERS_NO_META_MUTATION,
            "variables": {"input": {"id": int(manga_id), "fetchManga": True, "fetchChapters": True}},
        },
        allowed_hosts=_suwayomi_allowed_hosts(row),
        purpose="fetch_suwayomi_manga_and_chapters",
    )


def suwayomi_fetch_manga_chapters_rest_request(manga_id, row, plan=None):
    manga_id = providers.int_value(manga_id, None)
    if manga_id is None:
        return None
    return _request(
        "suwayomi_fetch_manga_chapters_rest",
        "GET",
        f"{_suwayomi_base_url(row)}/api/v1/manga/{int(manga_id)}/chapters",
        headers={"Accept": "application/json"},
        allowed_hosts=_suwayomi_allowed_hosts(row),
        purpose="fetch_suwayomi_manga_chapters_rest",
    )


def suwayomi_fetch_chapter_pages_request(chapter_id, row, plan=None, *, include_meta=True):
    chapter_id = providers.int_value(chapter_id, None)
    if chapter_id is None:
        return None
    request_id = "suwayomi_fetch_chapter_pages" if include_meta else "suwayomi_fetch_chapter_pages_no_meta"
    return _request(
        request_id,
        "POST",
        f"{_suwayomi_base_url(row)}/api/graphql",
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        json_body={
            "query": SUWAYOMI_FETCH_CHAPTER_PAGES_MUTATION
            if include_meta
            else SUWAYOMI_FETCH_CHAPTER_PAGES_NO_META_MUTATION,
            "variables": {"input": {"chapterId": int(chapter_id)}},
        },
        allowed_hosts=_suwayomi_allowed_hosts(row),
        purpose="fetch_suwayomi_chapter_pages",
    )


def _known_mangadex_manga_id(wanted_item=None):
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    for key in ("mangadex_id", "manga_id"):
        value = str(wanted_item.get(key) or "").strip()
        if value:
            return value
    metadata_provider = str(wanted_item.get("metadata_provider") or "").strip().lower()
    if metadata_provider == "mangadex":
        return str(wanted_item.get("metadata_id") or "").strip()
    return ""


def prowlarr_search_request(row, plan, wanted_item=None, limit=20, *, query=None, request_id="prowlarr_search"):
    base = _base_url(row)
    if not base:
        return None
    search_url = f"{base}/search" if base.rstrip("/").endswith("/api/v1") else f"{base}{PROWLARR_SEARCH_PATH}"
    params = {
        "query": providers.normalized_query(query or indexer_source_query(wanted_item)),
        "limit": int(limit or 20),
    }
    policy = (row or {}).get("policy") if isinstance((row or {}).get("policy"), dict) else {}
    categories = providers.category_ids(policy.get("categories") or policy.get("comic_categories") or [])
    if categories:
        params["categories"] = categories
    indexer_ids = _prowlarr_indexer_ids(row)
    if len(indexer_ids) == 1:
        params["indexerIds"] = indexer_ids[0]
    elif indexer_ids:
        params["indexerIds"] = indexer_ids
    return _request(
        request_id,
        "GET",
        search_url,
        params=params,
        headers={"Accept": "application/json", **_secret_header(row)},
        purpose="search_prowlarr",
    )


def prowlarr_search_requests(row, plan, wanted_item=None, limit=20):
    policy = (row or {}).get("policy") if isinstance((row or {}).get("policy"), dict) else {}
    max_queries = providers.int_value(
        policy.get("prowlarr_max_query_variants") or policy.get("max_query_variants"),
        3,
    )
    weekly_pack_limit = providers.int_value(
        policy.get("prowlarr_weekly_pack_query_limit")
        or policy.get("weekly_pack_query_limit")
        or policy.get("max_weekly_pack_queries"),
        None,
    )
    issue_queries = indexer_source_queries(
        wanted_item,
        max_queries=max_queries,
        policy=policy,
        include_series_fallback=_comic_series_fallback_queries_enabled(row, wanted_item),
    )
    # An operator-started issue/volume search already supplies deliberate title
    # variants.  Appending up to eight dated weekly-pack probes makes the child
    # lane exceed the manual provider deadline and discards earlier good hits.
    # Automatic backlog discovery keeps its weekly-pack coverage unchanged.
    weekly_queries = [] if bool((wanted_item or {}).get("manual_search")) else indexer_weekly_pack_queries(
        row,
        wanted_item,
        max_queries=weekly_pack_limit,
    )
    weekly_query_keys = {query.lower() for query in weekly_queries}
    queries = []
    seen_queries = set()
    for query in [*issue_queries, *weekly_queries]:
        key = str(query or "").strip().lower()
        if not key or key in seen_queries:
            continue
        seen_queries.add(key)
        queries.append(query)
    requests = []
    variant_index = 0
    weekly_index = 0
    for index, query in enumerate(queries):
        is_weekly_pack = str(query or "").strip().lower() in weekly_query_keys
        if is_weekly_pack:
            request_id = f"prowlarr_weekly_pack_search_{weekly_index}"
            weekly_index += 1
        else:
            request_id = "prowlarr_search" if variant_index == 0 else f"prowlarr_search_variant_{variant_index}"
            variant_index += 1
        request = prowlarr_search_request(
            row,
            plan,
            wanted_item,
            limit=limit,
            query=query,
            request_id=request_id,
        )
        if request:
            request["query_group"] = "weekly_pack" if is_weekly_pack else ("volume" if _is_volume_wanted_item(wanted_item) else "issue")
            request["pack_query"] = bool(is_weekly_pack)
            request["query_index"] = index
            requests.append(request)
    return requests


def prowlarr_categoryless_fallback_requests(row, plan, wanted_item=None, limit=20):
    fallback_indexer_ids = _prowlarr_categoryless_fallback_indexer_ids(row)
    if not fallback_indexer_ids:
        return []
    policy = (row or {}).get("policy") if isinstance((row or {}).get("policy"), dict) else {}
    if not providers.category_ids(policy.get("categories") or policy.get("comic_categories") or []):
        return []
    requests = []
    for fallback_index, request in enumerate(prowlarr_search_requests(row, plan, wanted_item, limit=limit)):
        params = dict(request.get("params") or {})
        if not params.get("categories"):
            continue
        params.pop("categories", None)
        if len(fallback_indexer_ids) == 1:
            params.pop("indexerId", None)
            params["indexerIds"] = fallback_indexer_ids[0]
        else:
            params.pop("indexerId", None)
            params["indexerIds"] = list(fallback_indexer_ids)
        fallback = dict(request)
        fallback["request_id"] = f"{request.get('request_id') or 'prowlarr_search'}_categoryless_fallback"
        if fallback_index:
            fallback["request_id"] = f"{fallback['request_id']}_{fallback_index}"
        fallback["params"] = params
        fallback["purpose"] = "search_prowlarr_categoryless_fallback"
        fallback["categoryless_fallback"] = True
        fallback["primary_request_id"] = request.get("request_id")
        requests.append(fallback)
    return requests


# Extra query-text variants an indexer-style provider (Prowlarr, Torznab,
# Newznab) tries immediately after its planned batch executes cleanly with
# zero combined results -- the same "the pool had more, it just wasn't tried"
# gap SLSKD's probe_item()/manual_search_discovery() already close for
# Soulseek, generalized here because indexer_source_queries() has the exact
# same pool-then-hard-truncate shape (values[:limit], tail simply dropped)
# with no cross-cycle rotation to ever reach the tail otherwise. Kept small
# and bounded on purpose -- each extra query is still a full request through
# the normal fetch path below, so an already rate-limited/unavailable
# provider gets the same handling as the planned batch, never a bypass.
SEARCH_EXPANSION_MAX_QUERIES = 2


def _indexer_max_query_variants(row, *, provider_prefix="", max_queries_default=3):
    policy = (row or {}).get("policy") if isinstance((row or {}).get("policy"), dict) else {}
    provider_prefix = str(provider_prefix or "").strip().lower()
    prefix_max_key = f"{provider_prefix}_max_query_variants" if provider_prefix else ""
    return providers.int_value(
        policy.get(prefix_max_key)
        or policy.get("indexer_max_query_variants")
        or policy.get("max_query_variants"),
        max_queries_default,
    )


def _indexer_query_expansion_pool(
    wanted_item,
    *,
    policy,
    planned_max_queries,
    include_series_fallback,
    max_expansion=SEARCH_EXPANSION_MAX_QUERIES,
):
    """The untried tail of the same priority-ordered pool the planned batch was cut from."""
    planned_queries = indexer_source_queries(
        wanted_item,
        max_queries=planned_max_queries,
        policy=policy,
        include_series_fallback=include_series_fallback,
    )
    full_pool = indexer_source_queries(
        wanted_item,
        max_queries=planned_max_queries + max_expansion,
        policy=policy,
        include_series_fallback=include_series_fallback,
    )
    already_planned = {str(query or "").strip().lower() for query in planned_queries}
    return [
        query for query in full_pool
        if str(query or "").strip().lower() not in already_planned
    ][:max_expansion]


def prowlarr_search_expansion_requests(row, plan, wanted_item=None, limit=20):
    policy = (row or {}).get("policy") if isinstance((row or {}).get("policy"), dict) else {}
    max_queries = providers.int_value(
        policy.get("prowlarr_max_query_variants") or policy.get("max_query_variants"),
        3,
    )
    expansion_queries = _indexer_query_expansion_pool(
        wanted_item,
        policy=policy,
        planned_max_queries=max_queries,
        include_series_fallback=_comic_series_fallback_queries_enabled(row, wanted_item),
    )
    requests = []
    for index, query in enumerate(expansion_queries):
        request = prowlarr_search_request(
            row,
            plan,
            wanted_item,
            limit=limit,
            query=query,
            request_id=f"prowlarr_search_expansion_{index}",
        )
        if request:
            request["query_group"] = "volume" if _is_volume_wanted_item(wanted_item) else "issue"
            request["pack_query"] = False
            request["query_expansion"] = True
            requests.append(request)
    return requests


def _indexer_query_variants(row, wanted_item=None, *, provider_prefix="", max_queries_default=3):
    policy = (row or {}).get("policy") if isinstance((row or {}).get("policy"), dict) else {}
    provider_prefix = str(provider_prefix or "").strip().lower()
    prefix_pack_key = f"{provider_prefix}_weekly_pack_query_limit" if provider_prefix else ""
    max_queries = _indexer_max_query_variants(
        row,
        provider_prefix=provider_prefix,
        max_queries_default=max_queries_default,
    )
    weekly_pack_limit = providers.int_value(
        policy.get(prefix_pack_key)
        or policy.get("indexer_weekly_pack_query_limit")
        or policy.get("weekly_pack_query_limit")
        or policy.get("max_weekly_pack_queries"),
        None,
    )
    issue_queries = indexer_source_queries(
        wanted_item,
        max_queries=max_queries,
        policy=policy,
        include_series_fallback=_comic_series_fallback_queries_enabled(row, wanted_item),
    )
    weekly_queries = indexer_weekly_pack_queries(row, wanted_item, max_queries=weekly_pack_limit)
    weekly_query_keys = {query.lower() for query in weekly_queries}
    queries = []
    seen_queries = set()
    for query in [*issue_queries, *weekly_queries]:
        key = str(query or "").strip().lower()
        if not key or key in seen_queries:
            continue
        seen_queries.add(key)
        queries.append(query)
    return queries, weekly_query_keys


def torznab_search_request(row, plan, wanted_item=None, limit=20, *, query=None, request_id="torznab_search"):
    base = _base_url(row)
    if not base:
        return None
    policy = (row or {}).get("policy") if isinstance((row or {}).get("policy"), dict) else {}
    categories = providers.category_ids(policy.get("categories") or policy.get("comic_categories") or [])
    params = {
        "t": "search",
        "q": providers.normalized_query(query or indexer_source_query(wanted_item)),
        "limit": int(limit or 20),
        "extended": "1",
    }
    if categories:
        params["cat"] = ",".join(categories)
    return _request(
        request_id,
        "GET",
        base,
        params=params,
        secret_params=_secret_query_params(row, "apikey"),
        headers={"Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.1"},
        purpose="search_torznab",
    )


def torznab_search_requests(row, plan, wanted_item=None, limit=20):
    queries, weekly_query_keys = _indexer_query_variants(row, wanted_item, provider_prefix="torznab")
    requests = []
    variant_index = 0
    weekly_index = 0
    for index, query in enumerate(queries):
        is_weekly_pack = str(query or "").strip().lower() in weekly_query_keys
        if is_weekly_pack:
            request_id = f"torznab_weekly_pack_search_{weekly_index}"
            weekly_index += 1
        else:
            request_id = "torznab_search" if variant_index == 0 else f"torznab_search_variant_{variant_index}"
            variant_index += 1
        request = torznab_search_request(
            row,
            plan,
            wanted_item,
            limit=limit,
            query=query,
            request_id=request_id,
        )
        if request:
            request["query_group"] = "weekly_pack" if is_weekly_pack else ("volume" if _is_volume_wanted_item(wanted_item) else "issue")
            request["pack_query"] = bool(is_weekly_pack)
            request["query_index"] = index
            requests.append(request)
    return requests


def newznab_search_request(row, plan, wanted_item=None, limit=20, *, query=None, request_id="newznab_search"):
    base = _base_url(row)
    if not base:
        return None
    policy = (row or {}).get("policy") if isinstance((row or {}).get("policy"), dict) else {}
    categories = providers.category_ids(policy.get("categories") or policy.get("comic_categories") or [])
    params = {
        "t": "search",
        "q": providers.normalized_query(query or indexer_source_query(wanted_item)),
        "limit": int(limit or 20),
        "extended": "1",
    }
    if categories:
        params["cat"] = ",".join(categories)
    return _request(
        request_id,
        "GET",
        base,
        params=params,
        secret_params=_secret_query_params(row, "apikey"),
        headers={"Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.1"},
        purpose="search_newznab",
    )


def newznab_search_requests(row, plan, wanted_item=None, limit=20):
    queries, weekly_query_keys = _indexer_query_variants(row, wanted_item, provider_prefix="newznab")
    requests = []
    variant_index = 0
    weekly_index = 0
    for index, query in enumerate(queries):
        is_weekly_pack = str(query or "").strip().lower() in weekly_query_keys
        if is_weekly_pack:
            request_id = f"newznab_weekly_pack_search_{weekly_index}"
            weekly_index += 1
        else:
            request_id = "newznab_search" if variant_index == 0 else f"newznab_search_variant_{variant_index}"
            variant_index += 1
        request = newznab_search_request(
            row,
            plan,
            wanted_item,
            limit=limit,
            query=query,
            request_id=request_id,
        )
        if request:
            request["query_group"] = "weekly_pack" if is_weekly_pack else ("volume" if _is_volume_wanted_item(wanted_item) else "issue")
            request["pack_query"] = bool(is_weekly_pack)
            request["query_index"] = index
            requests.append(request)
    return requests


def _indexer_family_expansion_requests(row, plan, wanted_item, limit, *, provider_prefix, request_builder, request_id_prefix):
    policy = (row or {}).get("policy") if isinstance((row or {}).get("policy"), dict) else {}
    max_queries = _indexer_max_query_variants(row, provider_prefix=provider_prefix)
    expansion_queries = _indexer_query_expansion_pool(
        wanted_item,
        policy=policy,
        planned_max_queries=max_queries,
        include_series_fallback=_comic_series_fallback_queries_enabled(row, wanted_item),
    )
    requests = []
    for index, query in enumerate(expansion_queries):
        request = request_builder(
            row,
            plan,
            wanted_item,
            limit=limit,
            query=query,
            request_id=f"{request_id_prefix}_{index}",
        )
        if request:
            request["query_group"] = "volume" if _is_volume_wanted_item(wanted_item) else "issue"
            request["pack_query"] = False
            request["query_expansion"] = True
            requests.append(request)
    return requests


def torznab_search_expansion_requests(row, plan, wanted_item=None, limit=20):
    return _indexer_family_expansion_requests(
        row,
        plan,
        wanted_item,
        limit,
        provider_prefix="torznab",
        request_builder=torznab_search_request,
        request_id_prefix="torznab_search_expansion",
    )


def newznab_search_expansion_requests(row, plan, wanted_item=None, limit=20):
    return _indexer_family_expansion_requests(
        row,
        plan,
        wanted_item,
        limit,
        provider_prefix="newznab",
        request_builder=newznab_search_request,
        request_id_prefix="newznab_search_expansion",
    )


def indexer_pack_detail_request(
    url,
    index,
    *,
    source="download_metadata",
    max_bytes=INDEXER_PACK_DETAIL_MAX_BYTES,
    allowed_hosts=None,
):
    request = _request(
        f"indexer_pack_detail_{index}",
        "GET",
        url,
        headers={
            "Accept": "application/x-nzb, application/x-bittorrent, application/xml;q=0.9, text/plain;q=0.8, text/html;q=0.7, */*;q=0.1"
        },
        purpose=f"fetch_indexer_pack_detail_{source}",
    )
    request["allow_truncated"] = True
    request["max_bytes"] = max(1024, int(max_bytes or INDEXER_PACK_DETAIL_MAX_BYTES))
    if allowed_hosts:
        request["allowed_hosts"] = list(allowed_hosts)
    return request


def _normalized_request_hosts(values):
    out = []
    seen = set()
    for value in values or []:
        text = str(value or "").strip().lower()
        if "://" in text:
            text = str(urlparse(text).hostname or "").strip().lower()
        text = text.strip("[]")
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _rss_discovery_allowed_hosts(row):
    row = row if isinstance(row, dict) else {}
    configured = _policy_host_list(
        row,
        ("feed_detail_allowed_hosts", "rss_allowed_hosts", "source_allowed_hosts"),
    )
    if row.get("provider_id") == "rss_getcomics":
        return list(GETCOMICS_DISCOVERY_HOSTS)
    return _normalized_request_hosts(configured)


def _direct_transport_allowed_hosts(row):
    row = row if isinstance(row, dict) else {}
    configured = _policy_host_list(
        row,
        ("transport_allowed_hosts", "direct_download_allowed_hosts", "direct_allowed_hosts"),
    )
    if row.get("provider_id") == "rss_getcomics":
        return list(PIXELDRAIN_TRANSPORT_HOSTS)
    return _normalized_request_hosts(configured)


def _url_host_allowed(url, allowed_hosts):
    host = str(urlparse(str(url or "")).hostname or "").strip().lower()
    allowed = set(_normalized_request_hosts(allowed_hosts))
    return bool(host and (not allowed or host in allowed))


def rss_feed_request(row, plan, wanted_item=None, limit=20):
    return _request(
        "rss_feed",
        "GET",
        _base_url(row, GETCOMICS_FEED_URL),
        headers={"Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.1"},
        allowed_hosts=_rss_discovery_allowed_hosts(row),
        purpose="poll_rss_feed",
    )


def rss_direct_feed_request(row, plan, wanted_item=None, limit=20):
    base = _base_url(row)
    if not base:
        return None
    return _request(
        "rss_direct_feed",
        "GET",
        base,
        headers={"Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.1"},
        allowed_hosts=_rss_discovery_allowed_hosts(row),
        purpose="poll_direct_rss_feed",
    )


def rss_detail_direct_feed_request(row, plan, wanted_item=None, limit=20):
    default_url = GETCOMICS_FEED_URL if (row or {}).get("provider_id") == "rss_getcomics" else ""
    base = _base_url(row, default_url)
    if not base:
        return None
    return _request(
        "rss_detail_direct_feed",
        "GET",
        base,
        headers={"Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.1"},
        allowed_hosts=_rss_discovery_allowed_hosts(row),
        purpose="poll_rss_detail_direct_feed",
    )


def rss_detail_probe_feed_request(row, plan, wanted_item=None, limit=20):
    default_url = GETCOMICS_FEED_URL if (row or {}).get("provider_id") == "rss_getcomics" else ""
    base = _base_url(row, default_url)
    if not base:
        return None
    return _request(
        "rss_detail_probe_feed",
        "GET",
        base,
        headers={"Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.1"},
        allowed_hosts=_rss_discovery_allowed_hosts(row),
        purpose="poll_rss_detail_probe_feed",
    )


def rss_reader_page_pack_feed_request(row, plan, wanted_item=None, limit=20):
    base = _base_url(row)
    if not base:
        return None
    return _request(
        "rss_reader_page_pack_feed",
        "GET",
        base,
        headers={"Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.1"},
        purpose="poll_rss_reader_page_pack_feed",
    )


def torrent_rss_feed_request(row, plan, wanted_item=None, limit=20):
    base = _base_url(row)
    if not base:
        return None
    return _request(
        "torrent_rss_feed",
        "GET",
        base,
        headers={"Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.1"},
        purpose="poll_torrent_rss_feed",
    )


def torrent_detail_rss_feed_request(row, plan, wanted_item=None, limit=20):
    base = _base_url(row)
    if not base:
        return None
    return _request(
        "torrent_detail_rss_feed",
        "GET",
        base,
        headers={"Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.1"},
        purpose="poll_torrent_detail_rss_feed",
    )


def opds_catalog_request(row, plan, wanted_item=None, limit=20):
    base = _base_url(row)
    if not base:
        return None
    return _request(
        "opds_catalog",
        "GET",
        base,
        headers={"Accept": "application/atom+xml, application/xml;q=0.9, application/opds+json;q=0.7, */*;q=0.1"},
        purpose="fetch_opds_acquisition_catalog",
    )


def _policy_url_list(row, key, default):
    policy = (row or {}).get("policy") if isinstance((row or {}).get("policy"), dict) else {}
    value = (row or {}).get(key)
    if value in (None, "", [], ()):
        value = policy.get(key)
    if isinstance(value, str):
        urls = [part.strip() for part in value.splitlines() if part.strip()]
    elif isinstance(value, (list, tuple, set)):
        urls = [str(part).strip() for part in value if str(part or "").strip()]
    else:
        urls = []
    return urls or list(default)


def _policy_value(row, key, default=None):
    row = row if isinstance(row, dict) else {}
    policy = row.get("policy") if isinstance(row.get("policy"), dict) else {}
    value = row.get(key)
    if value in (None, "", [], {}):
        value = policy.get(key, default)
    return value


def _policy_text(row, key, default=""):
    return str(_policy_value(row, key, default) or "").strip()


def _policy_bool(row, key, default=False):
    value = _policy_value(row, key, default)
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _policy_list(row, key, default=None):
    default = default if default is not None else []
    value = _policy_value(row, key, default)
    if isinstance(value, str):
        parts = []
        for line in value.splitlines():
            for part in str(line or "").split(","):
                text = part.strip()
                if text:
                    parts.append(text)
        return parts
    if isinstance(value, (list, tuple, set)):
        return [str(part).strip() for part in value if str(part or "").strip()]
    return list(default)


def _policy_dict(row, key):
    value = _policy_value(row, key, {})
    return dict(value) if isinstance(value, dict) else {}


def _host_value(value):
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if "://" in text:
        text = urlparse(text).hostname or ""
    return text.strip("[]")


def _policy_host_list(row, keys):
    out = []
    seen = set()
    for key in keys or []:
        for value in _policy_list(row, key, []):
            host = _host_value(value)
            if not host or host in seen:
                continue
            seen.add(host)
            out.append(host)
    return out


def _template_context(wanted_item=None, *, staged_output_root="", limit=20):
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    query = source_query(wanted_item)
    title = providers.normalized_query(
        wanted_item.get("title")
        or wanted_item.get("series_title")
        or wanted_item.get("series")
        or query
    )
    series_title = providers.normalized_query(
        wanted_item.get("series_title")
        or wanted_item.get("series")
        or title
    )
    return {
        "query": query,
        "query_plus": quote_plus(query),
        "query_quote": quote(query, safe=""),
        "title": title,
        "series_title": series_title,
        "limit": str(int(limit or 20)),
        "staged_output_root": str(staged_output_root or ""),
    }


def _format_template(value, context):
    text = str(value or "")
    for key, replacement in (context or {}).items():
        text = text.replace("{" + key + "}", str(replacement))
    return text


def comicscodes_requests(row, plan, wanted_item=None, limit=20):
    base = _base_url(row, "https://comics.codes")
    feed_urls = _policy_url_list(row, "feed_urls", [f"{base}/feed/"])
    list_urls = _policy_url_list(row, "list_urls", COMICSCODES_LIST_URLS)
    requests = []
    for index, url in enumerate([*feed_urls, *list_urls]):
        request_id = "comicscodes_feed" if index == 0 else f"comicscodes_list_{index}"
        requests.append(
            _request(
                request_id,
                "GET",
                url,
                headers={"Accept": "application/rss+xml, application/xml;q=0.9, text/html;q=0.8, */*;q=0.1"},
                purpose="poll_comicscodes_feed_or_list",
            )
        )
    return requests


def _format_search_url(template, query, *, page=1, limit=20):
    template = str(template or "").strip()
    if not template:
        return ""
    encoded_query = quote_plus(str(query or ""))
    path_query = quote(str(query or ""), safe="")
    page_number = max(1, providers.int_value(page, 1) or 1)
    page_limit = max(1, providers.int_value(limit, 20) or 20)
    has_query_placeholder = "{query}" in template or "{query_plus}" in template or "{query_quote}" in template
    formatted = (
        template.replace("{query_plus}", encoded_query)
        .replace("{query_quote}", path_query)
        .replace("{query}", encoded_query)
        .replace("{page0}", str(page_number - 1))
        .replace("{page}", str(page_number))
        .replace("{offset}", str((page_number - 1) * page_limit))
        .replace("{limit}", str(page_limit))
    )
    if has_query_placeholder:
        return formatted
    separator = "&" if "?" in template else "?"
    return f"{formatted}{separator}q={encoded_query}"


def _format_list_url(template, query="", *, page=1, limit=20):
    template = str(template or "").strip()
    if not template:
        return ""
    encoded_query = quote_plus(str(query or ""))
    path_query = quote(str(query or ""), safe="")
    page_number = max(1, providers.int_value(page, 1) or 1)
    page_limit = max(1, providers.int_value(limit, 20) or 20)
    return (
        template.replace("{query_plus}", encoded_query)
        .replace("{query_quote}", path_query)
        .replace("{query}", encoded_query)
        .replace("{page0}", str(page_number - 1))
        .replace("{page}", str(page_number))
        .replace("{offset}", str((page_number - 1) * page_limit))
        .replace("{limit}", str(page_limit))
    )


def _search_page_cap(row, limit=20):
    policy = (row or {}).get("policy") if isinstance((row or {}).get("policy"), dict) else {}
    cap = providers.int_value(policy.get("max_search_pages"), 1)
    cap = max(1, cap or 1)
    return min(cap, 20)


def _dedup_urls(urls):
    out = []
    seen = set()
    for url in urls or []:
        text = str(url or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _append_pagination_urls(row, urls, query, *, limit=20, template_key="pagination_url_templates", append_query=True):
    templates = _policy_url_list(row, template_key, [])
    if not templates:
        return _dedup_urls(urls)
    cap = _search_page_cap(row, limit=limit)
    if cap <= 1:
        return _dedup_urls(urls)
    out = list(urls or [])
    for page in range(2, cap + 1):
        for template in templates:
            if append_query:
                url = _format_search_url(template, query, page=page, limit=limit)
            else:
                url = _format_list_url(template, query, page=page, limit=limit)
            if url:
                out.append(url)
    return _dedup_urls(out)


def _search_api_flavors(row):
    flavors = _policy_list(row, "search_api_flavors", [])
    if not flavors:
        text = _policy_text(row, "search_api_flavor", "")
        flavors = [text] if text else []
    return {str(flavor or "").strip().lower() for flavor in flavors if str(flavor or "").strip()}


def _wordpress_search_url(base_url, query, limit=20):
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return ""
    per_page = max(1, min(int(limit or 20), 100))
    return f"{base}/wp-json/wp/v2/search?search={quote_plus(str(query or ''))}&per_page={per_page}&subtype=post"


def _wordpress_posts_url(base_url, query, limit=20):
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return ""
    per_page = max(1, min(int(limit or 20), 100))
    fields = "id,link,title,content,excerpt"
    return f"{base}/wp-json/wp/v2/posts?search={quote_plus(str(query or ''))}&per_page={per_page}&_fields={quote(fields, safe=',')}"


def _configured_search_urls(row, query, *, default_base=True, limit=20):
    search_templates = _policy_url_list(row, "search_url_templates", [])
    search_urls = []
    for template in search_templates:
        url = _format_search_url(template, query, page=1, limit=limit)
        if url:
            search_urls.append(url)
    if search_urls:
        search_urls = _append_pagination_urls(row, search_urls, query, limit=limit)
    list_templates = _policy_url_list(row, "list_url_templates", [])
    list_urls = []
    for template in list_templates:
        url = _format_list_url(template, query, page=1, limit=limit)
        if url:
            list_urls.append(url)
    if list_urls:
        list_urls = _append_pagination_urls(
            row,
            list_urls,
            query,
            limit=limit,
            template_key="list_pagination_url_templates",
            append_query=False,
        )
    urls = _dedup_urls([*search_urls, *list_urls])
    if urls:
        return urls
    base = _base_url(row)
    if base and "wordpress" in _search_api_flavors(row):
        url = _wordpress_search_url(base, query, limit=limit)
        if url:
            return _append_pagination_urls(row, [url], query, limit=limit)
    if base and default_base:
        return _append_pagination_urls(row, [_format_search_url(base, query, page=1, limit=limit)], query, limit=limit)
    return []


def html_search_requests(row, plan, wanted_item=None, limit=20):
    query = source_query(wanted_item)
    urls = _configured_search_urls(row, query, default_base=False, limit=limit)
    requests = []
    for index, url in enumerate(urls):
        requests.append(
            _request(
                f"html_search_{index}",
                "GET",
                url,
                headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.8,*/*;q=0.1"},
                purpose="search_html_source",
            )
        )
    return requests


def _html_adapter_query_variants(row, wanted_item=None, *, provider_prefix=""):
    policy = (row or {}).get("policy") if isinstance((row or {}).get("policy"), dict) else {}
    prefix_key = f"{provider_prefix}_max_query_variants" if provider_prefix else ""
    max_queries = providers.int_value(
        (policy.get(prefix_key) if prefix_key else None)
        or policy.get("html_max_query_variants")
        or policy.get("max_query_variants"),
        3,
    )
    return indexer_source_queries(
        wanted_item,
        max_queries=max_queries,
        policy=policy,
        include_series_fallback=False,
    )


def _html_adapter_search_requests(
    row,
    wanted_item,
    *,
    limit,
    request_prefix,
    purpose,
    accept,
    provider_prefix,
    default_base=True,
):
    requests = []
    queries = _html_adapter_query_variants(row, wanted_item, provider_prefix=provider_prefix)
    for query_index, query in enumerate(queries):
        urls = _configured_search_urls(row, query, default_base=default_base, limit=limit)
        for url in urls:
            request = _request(
                f"{request_prefix}_{len(requests)}",
                "GET",
                url,
                headers={"Accept": accept},
                purpose=purpose,
            )
            request["query_variant"] = query
            request["query_variant_index"] = query_index
            requests.append(request)
    return requests


def direct_file_html_search_requests(row, plan, wanted_item=None, limit=20):
    return _html_adapter_search_requests(
        row,
        wanted_item,
        limit=limit,
        request_prefix="direct_file_html_search",
        purpose="search_direct_file_html_source",
        accept="text/html,application/xhtml+xml,application/xml;q=0.8,*/*;q=0.1",
        provider_prefix="direct_file",
        default_base=False,
    )


def direct_file_detail_search_requests(row, plan, wanted_item=None, limit=20):
    return _html_adapter_search_requests(
        row,
        wanted_item,
        limit=limit,
        request_prefix="direct_file_detail_search",
        purpose="search_direct_file_detail_source",
        accept="application/json,text/html,application/xhtml+xml,application/xml;q=0.8,*/*;q=0.1",
        provider_prefix="direct_file",
    )


def direct_file_detail_page_request(url, index, allowed_hosts=None):
    return _request(
        f"direct_file_detail_page_{index}",
        "GET",
        url,
        headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.8,*/*;q=0.1"},
        allowed_hosts=allowed_hosts,
        purpose="fetch_direct_file_detail_page",
    )


def direct_file_probe_search_requests(row, plan, wanted_item=None, limit=20):
    return _html_adapter_search_requests(
        row,
        wanted_item,
        limit=limit,
        request_prefix="direct_file_probe_search",
        purpose="search_direct_file_probe_source",
        accept="application/json,text/html,application/xhtml+xml,application/xml;q=0.8,*/*;q=0.1",
        provider_prefix="direct_file",
    )


def direct_file_probe_request(url, index, method="HEAD", allowed_hosts=None):
    method = str(method or "HEAD").strip().upper()
    if method not in {"GET", "HEAD"}:
        method = "HEAD"
    headers = {"Accept": "application/zip,application/pdf,application/epub+zip,application/octet-stream,*/*;q=0.1"}
    if method == "GET":
        headers["Range"] = "bytes=0-0"
    return _request(
        f"direct_file_probe_{method.lower()}_{index}",
        method,
        url,
        headers=headers,
        allowed_hosts=allowed_hosts,
        max_bytes=1024 if method == "GET" else None,
        purpose="probe_direct_file_headers",
    )


def direct_file_probe_head_request(url, index):
    return direct_file_probe_request(url, index, method="HEAD")


def reader_page_pack_search_requests(row, plan, wanted_item=None, limit=20):
    query = source_query(wanted_item)
    urls = _configured_search_urls(row, query, limit=limit)
    requests = []
    for index, url in enumerate(urls):
        requests.append(
            _request(
                f"reader_page_pack_search_{index}",
                "GET",
                url,
                headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.8,*/*;q=0.1"},
                purpose="search_reader_page_pack_source",
            )
        )
    return requests


def reader_page_pack_page_request(url, index):
    return _request(
        f"reader_page_pack_page_{index}",
        "GET",
        url,
        headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.8,*/*;q=0.1"},
        purpose="fetch_reader_page_pack_page",
    )


def reader_page_pack_chapter_page_request(url, index):
    return _request(
        f"reader_page_pack_chapter_page_{index}",
        "GET",
        url,
        headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.8,*/*;q=0.1"},
        purpose="fetch_reader_page_pack_chapter_page",
    )


def torrent_html_search_requests(row, plan, wanted_item=None, limit=20):
    return _html_adapter_search_requests(
        row,
        wanted_item,
        limit=limit,
        request_prefix="torrent_html_search",
        purpose="search_torrent_html_source",
        accept="text/html,application/xhtml+xml,application/xml;q=0.8,*/*;q=0.1",
        provider_prefix="torrent_html",
    )


def torrent_detail_search_requests(row, plan, wanted_item=None, limit=20):
    return _html_adapter_search_requests(
        row,
        wanted_item,
        limit=limit,
        request_prefix="torrent_detail_search",
        purpose="search_torrent_detail_source",
        accept="application/json,text/html,application/xhtml+xml,application/xml;q=0.8,*/*;q=0.1",
        provider_prefix="torrent_html",
    )


def torrent_detail_page_request(url, index):
    return _request(
        f"torrent_detail_page_{index}",
        "GET",
        url,
        headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.8,*/*;q=0.1"},
        purpose="fetch_torrent_detail_page",
    )


def json_direct_source_requests(row, plan, wanted_item=None, limit=20):
    query = source_query(wanted_item)
    templates = _policy_url_list(row, "search_url_templates", [])
    urls = []
    for template in templates:
        url = _format_search_url(template, query, page=1, limit=limit)
        if url:
            urls.append(url)
    base = _base_url(row)
    flavors = _search_api_flavors(row)
    if not urls and base and "wordpress_posts" in flavors:
        urls.append(_wordpress_posts_url(base, query, limit=limit))
    if not urls and base:
        urls.append(base)
    urls = _append_pagination_urls(row, urls, query, limit=limit)
    requests = []
    for index, url in enumerate(urls):
        request_id = "json_direct_source" if len(urls) == 1 else f"json_direct_source_{index}"
        requests.append(
            _request(
                request_id,
                "GET",
                url,
                headers={"Accept": "application/json, application/activity+json;q=0.8, */*;q=0.1"},
                purpose="fetch_json_direct_source",
            )
        )
    return requests


def external_tool_command_plan(row, plan, wanted_item=None, limit=20):
    policy = (row or {}).get("policy") if isinstance((row or {}).get("policy"), dict) else {}
    auto_stage_output = bool(policy.get("auto_stage_tool_output") or policy.get("allow_staged_tool_output"))
    staged_output_root = _policy_text(row, "staged_output_root") or _policy_text(row, "staging_root")
    context = _template_context(wanted_item, staged_output_root=staged_output_root, limit=limit)
    executable = _format_template(
        providers.first_text(
            _policy_text(row, "command_executable"),
            _policy_text(row, "executable"),
        ),
        context,
    ).strip()
    args = [_format_template(arg, context) for arg in _policy_list(row, "command_args", [])]
    argv = _policy_list(row, "command_argv", [])
    if argv:
        formatted_argv = [_format_template(part, context) for part in argv]
        if not executable and formatted_argv:
            executable = formatted_argv[0]
            args = formatted_argv[1:]
        elif executable:
            args = formatted_argv
    command_env = {
        str(key): _format_template(value, context)
        for key, value in _policy_dict(row, "command_env").items()
        if str(key or "").strip()
    }
    secret_env = {
        str(key): f"<secret_ref:{str(value).strip()}>"
        for key, value in _policy_dict(row, "secret_env").items()
        if str(key or "").strip() and str(value or "").strip()
    }
    if (row or {}).get("secret_ref") and _policy_text(row, "secret_env_var"):
        secret_env[_policy_text(row, "secret_env_var")] = f"<secret_ref:{str((row or {}).get('secret_ref')).strip()}>"
    timeout_seconds = providers.int_value(_policy_value(row, "timeout_seconds", 900), 900)
    if timeout_seconds <= 0:
        timeout_seconds = 900
    timeout_seconds = min(timeout_seconds, 24 * 60 * 60)
    can_execute_with_tool_runner = bool(auto_stage_output and staged_output_root and executable)
    requires_operator = not can_execute_with_tool_runner
    out = {
        "command_plan_contract_version": CONTRACT_VERSION,
        "provider_id": (row or {}).get("provider_id"),
        "adapter_family": (plan or {}).get("adapter_family"),
        "tool_name": (row or {}).get("display_name") or (row or {}).get("provider_id"),
        "mode": "search_or_dry_run",
        "query": source_query(wanted_item),
        "limit": int(limit or 20),
        "requires_operator": requires_operator,
        "manual_review_only": requires_operator,
        "auto_stage_tool_output": auto_stage_output,
        "staged_output_root": staged_output_root,
        "command_executable": executable,
        "command_args": args,
        "argv": [part for part in [executable, *args] if part],
        "command_env": command_env,
        "secret_env": secret_env,
        "working_directory": _format_template(_policy_text(row, "working_directory"), context),
        "timeout_seconds": timeout_seconds,
        "can_execute_with_tool_runner": can_execute_with_tool_runner,
        "output_contract": "external_tool_candidates_from_results",
        "output_schema": providers.EXTERNAL_TOOL_CANDIDATE_OUTPUT_SCHEMA,
        "notes": "A live adapter must execute argv without a shell, confine output to staged_output_root, and capture stdout/stderr without storing secrets.",
    }
    if auto_stage_output and not staged_output_root:
        out["configuration_reason"] = "missing_staged_output_root"
    elif auto_stage_output and not executable:
        out["configuration_reason"] = "missing_external_tool_command"
    elif not auto_stage_output:
        out["configuration_reason"] = "operator_payload_required"
    return {key: value for key, value in out.items() if value not in (None, "", [], {})}


def manual_source_input_plan(row, plan, wanted_item=None, limit=20):
    return {
        "manual_input_contract_version": CONTRACT_VERSION,
        "provider_id": (row or {}).get("provider_id"),
        "adapter_family": (plan or {}).get("adapter_family"),
        "mode": "manual_review_cards",
        "query": source_query(wanted_item),
        "limit": int(limit or 20),
        "requires_operator": True,
        "manual_review_only": True,
        "output_contract": "manual_source_cards_from_results",
        "notes": "A live adapter may surface user-provided page/search results as manual cards; it must not resolve or download URLs automatically.",
    }


def adapter_fetch_plan(row, plan, wanted_item=None, limit=20):
    row = row if isinstance(row, dict) else {}
    plan = plan if isinstance(plan, dict) else {}
    adapter_family = str(plan.get("adapter_family") or "").strip()
    provider_id = row.get("provider_id")
    out = {
        "fetch_plan_contract_version": CONTRACT_VERSION,
        "provider_id": provider_id,
        "adapter_family": adapter_family,
        "adapter_id": plan.get("adapter_id"),
        "search_operation": plan.get("search_operation"),
        "query": source_query(wanted_item),
        "payload_mode": "none",
        "requests": [],
        "requires_operator": bool(plan.get("requires_operator")),
        "can_execute_with_http_client": False,
    }
    if provider_id == "standard_ebooks":
        out["payload_mode"] = "single_payload"
        out["requests"].append(standard_ebooks_request(row, plan, wanted_item, limit=limit))
    elif provider_id == "gutendex":
        out["payload_mode"] = "single_payload"
        out["requests"].append(gutendex_request(row, plan, wanted_item, limit=limit))
    elif provider_id == "internet_archive":
        out["payload_mode"] = "archive_search_then_metadata"
        if (wanted_item or {}).get("archive_identifier"):
            out["requests"].append(internet_archive_metadata_request((wanted_item or {}).get("archive_identifier")))
        else:
            out["requests"].append(internet_archive_search_request(row, plan, wanted_item, limit=limit))
    elif provider_id == "mangadex" or adapter_family == "mangadex_api":
        out["payload_mode"] = "mangadex_search_then_feed"
        manga_id = _known_mangadex_manga_id(wanted_item)
        if manga_id:
            out["requests"].append(mangadex_feed_request(manga_id, row, plan, wanted_item, limit=limit))
        else:
            out["mangadex_query_pool"] = mangadex_series_query_pool(
                wanted_item,
                policy=_source_query_alias_policy(row),
            )
            requests = mangadex_search_requests(row, plan, wanted_item, limit=limit)
            out["requests"].extend(requests)
            out["query_variants"] = [request.get("params", {}).get("title") for request in requests]
        if (
            providers.volume_page_pack_enabled(row)
            and providers.wanted_item_is_volume_unit(wanted_item)
            and not _policy_bool(row, "allow_single_chapter_volume_page_pack", False)
        ):
            out["estimated_request_count"] = min(
                80,
                len(out["requests"]) + _mangadex_feed_max_pages(row) + providers.volume_page_pack_max_chapters(row),
            )
    elif provider_id == "suwayomi" or adapter_family == "suwayomi_api":
        out["payload_mode"] = "suwayomi_search_then_chapters"
        out["requests"].append(suwayomi_source_list_request(row, plan))
        out["requests"].append(suwayomi_extension_list_request(row, plan))
        out["estimated_request_count"] = suwayomi_estimated_request_count(row, wanted_item)
        max_queries = providers.int_value(
            _policy_value(row, "suwayomi_max_query_variants", _policy_value(row, "max_query_variants", 3)),
            3,
        )
        out["query_variants"] = suwayomi_series_queries(
            wanted_item,
            max_queries=max_queries,
            policy=_source_query_alias_policy(row),
        )
    elif adapter_family == "prowlarr_indexer":
        requests = prowlarr_search_requests(row, plan, wanted_item, limit=limit)
        fallback_requests = prowlarr_categoryless_fallback_requests(row, plan, wanted_item, limit=limit)
        expansion_requests = prowlarr_search_expansion_requests(row, plan, wanted_item, limit=limit) if requests else []
        if requests:
            out["payload_mode"] = "prowlarr_multi_search" if len(requests) > 1 or fallback_requests or expansion_requests else "single_payload"
            out["requests"].extend(requests)
            out["query_variants"] = [request.get("params", {}).get("query") for request in requests]
            if fallback_requests:
                out["categoryless_fallback_requests"] = fallback_requests
                out["categoryless_fallback_indexer_ids"] = _prowlarr_categoryless_fallback_indexer_ids(row)
                out["estimated_request_count"] = len(requests) + len(fallback_requests)
            if expansion_requests:
                out["expansion_requests"] = expansion_requests
                out["estimated_request_count"] = len(requests) + len(fallback_requests) + len(expansion_requests)
        else:
            out["payload_mode"] = "configuration_required"
            out["reason"] = "missing_prowlarr_base_url"
    elif adapter_family == "indexer_discovery":
        requests = prowlarr_search_requests(row, plan, wanted_item, limit=limit)
        if requests:
            out["payload_mode"] = "prowlarr_multi_search" if len(requests) > 1 else "single_payload"
            out["requests"].extend(requests)
            out["query_variants"] = [request.get("params", {}).get("query") for request in requests]
        else:
            out["payload_mode"] = "configuration_required"
            out["reason"] = "missing_indexer_discovery_base_url"
    elif adapter_family == "torznab_indexer":
        requests = torznab_search_requests(row, plan, wanted_item, limit=limit)
        expansion_requests = torznab_search_expansion_requests(row, plan, wanted_item, limit=limit) if requests else []
        if requests:
            out["payload_mode"] = "indexer_multi_search" if len(requests) > 1 or expansion_requests else "single_payload"
            out["requests"].extend(requests)
            out["query_variants"] = [request.get("params", {}).get("q") for request in requests]
            if expansion_requests:
                out["expansion_requests"] = expansion_requests
        else:
            out["payload_mode"] = "configuration_required"
            out["reason"] = "missing_torznab_base_url"
    elif adapter_family == "newznab_indexer":
        requests = newznab_search_requests(row, plan, wanted_item, limit=limit)
        expansion_requests = newznab_search_expansion_requests(row, plan, wanted_item, limit=limit) if requests else []
        if requests:
            out["payload_mode"] = "indexer_multi_search" if len(requests) > 1 or expansion_requests else "single_payload"
            out["requests"].extend(requests)
            out["query_variants"] = [request.get("params", {}).get("q") for request in requests]
            if expansion_requests:
                out["expansion_requests"] = expansion_requests
        else:
            out["payload_mode"] = "configuration_required"
            out["reason"] = "missing_newznab_base_url"
    elif adapter_family == "rss_feed":
        out["payload_mode"] = "single_payload"
        out["requests"].append(rss_feed_request(row, plan, wanted_item, limit=limit))
    elif adapter_family == "rss_direct_feed":
        request = rss_direct_feed_request(row, plan, wanted_item, limit=limit)
        if request:
            out["payload_mode"] = "single_payload"
            out["requests"].append(request)
        else:
            out["payload_mode"] = "configuration_required"
            out["reason"] = "missing_rss_direct_feed_url"
    elif adapter_family == "rss_detail_direct_feed":
        request = rss_detail_direct_feed_request(row, plan, wanted_item, limit=limit)
        if request:
            out["payload_mode"] = "rss_feed_then_direct_file_pages"
            out["requests"].append(request)
        else:
            out["payload_mode"] = "configuration_required"
            out["reason"] = "missing_rss_detail_direct_feed_url"
    elif adapter_family == "rss_detail_probe_feed":
        request = rss_detail_probe_feed_request(row, plan, wanted_item, limit=limit)
        if request:
            out["payload_mode"] = "rss_feed_then_direct_file_probes"
            out["requests"].append(request)
        else:
            out["payload_mode"] = "configuration_required"
            out["reason"] = "missing_rss_detail_probe_feed_url"
    elif adapter_family == "rss_reader_page_pack_feed":
        request = rss_reader_page_pack_feed_request(row, plan, wanted_item, limit=limit)
        if request:
            out["payload_mode"] = "rss_feed_then_reader_pages"
            out["requests"].append(request)
        else:
            out["payload_mode"] = "configuration_required"
            out["reason"] = "missing_rss_reader_page_pack_feed_url"
    elif adapter_family == "torrent_rss_feed":
        request = torrent_rss_feed_request(row, plan, wanted_item, limit=limit)
        if request:
            out["payload_mode"] = "single_payload"
            out["requests"].append(request)
        else:
            out["payload_mode"] = "configuration_required"
            out["reason"] = "missing_torrent_rss_feed_url"
    elif adapter_family == "torrent_detail_rss_feed":
        request = torrent_detail_rss_feed_request(row, plan, wanted_item, limit=limit)
        if request:
            out["payload_mode"] = "rss_feed_then_torrent_detail_pages"
            out["requests"].append(request)
        else:
            out["payload_mode"] = "configuration_required"
            out["reason"] = "missing_torrent_detail_rss_feed_url"
    elif adapter_family == "opds_catalog":
        request = opds_catalog_request(row, plan, wanted_item, limit=limit)
        if request:
            out["payload_mode"] = "single_payload"
            out["requests"].append(request)
        else:
            out["payload_mode"] = "configuration_required"
            out["reason"] = "missing_opds_catalog_url"
    elif adapter_family == "direct_file_html_search":
        out["payload_mode"] = "multi_payload"
        out["requests"].extend(direct_file_html_search_requests(row, plan, wanted_item, limit=limit))
        out["query_variants"] = list(dict.fromkeys(request.get("query_variant") for request in out["requests"] if request.get("query_variant")))
        if not out["requests"]:
            out["payload_mode"] = "configuration_required"
            out["reason"] = "missing_direct_file_search_url_templates"
    elif adapter_family == "direct_file_detail_search":
        out["payload_mode"] = "html_search_then_direct_file_pages"
        out["requests"].extend(direct_file_detail_search_requests(row, plan, wanted_item, limit=limit))
        out["query_variants"] = list(dict.fromkeys(request.get("query_variant") for request in out["requests"] if request.get("query_variant")))
        if not out["requests"]:
            out["payload_mode"] = "configuration_required"
            out["reason"] = "missing_direct_file_detail_search_url_templates"
    elif adapter_family == "direct_file_probe_source":
        out["payload_mode"] = "html_search_then_direct_file_probes"
        out["requests"].extend(direct_file_probe_search_requests(row, plan, wanted_item, limit=limit))
        out["query_variants"] = list(dict.fromkeys(request.get("query_variant") for request in out["requests"] if request.get("query_variant")))
        if not out["requests"]:
            out["payload_mode"] = "configuration_required"
            out["reason"] = "missing_direct_file_probe_search_url_templates"
    elif adapter_family == "reader_page_pack_source":
        out["payload_mode"] = "html_search_then_reader_pages"
        out["requests"].extend(reader_page_pack_search_requests(row, plan, wanted_item, limit=limit))
        if not out["requests"]:
            out["payload_mode"] = "configuration_required"
            out["reason"] = "missing_reader_page_pack_search_url_templates"
    elif adapter_family == "torrent_html_search":
        out["payload_mode"] = "multi_payload"
        out["requests"].extend(torrent_html_search_requests(row, plan, wanted_item, limit=limit))
        out["query_variants"] = list(dict.fromkeys(request.get("query_variant") for request in out["requests"] if request.get("query_variant")))
        if not out["requests"]:
            out["payload_mode"] = "configuration_required"
            out["reason"] = "missing_torrent_html_search_url_templates"
    elif adapter_family == "torrent_detail_search":
        out["payload_mode"] = "html_search_then_torrent_detail_pages"
        out["requests"].extend(torrent_detail_search_requests(row, plan, wanted_item, limit=limit))
        out["query_variants"] = list(dict.fromkeys(request.get("query_variant") for request in out["requests"] if request.get("query_variant")))
        if not out["requests"]:
            out["payload_mode"] = "configuration_required"
            out["reason"] = "missing_torrent_detail_search_url_templates"
    elif adapter_family == "json_direct_source":
        out["payload_mode"] = "multi_payload"
        out["requests"].extend(json_direct_source_requests(row, plan, wanted_item, limit=limit))
        if not out["requests"]:
            out["payload_mode"] = "configuration_required"
            out["reason"] = "missing_json_direct_source_url"
    elif provider_id == "comicscodes" or adapter_family == "feed_or_list_source":
        out["payload_mode"] = "multi_payload"
        out["requests"].extend(comicscodes_requests(row, plan, wanted_item, limit=limit))
    elif adapter_family == "html_search_source":
        out["payload_mode"] = "multi_payload"
        out["requests"].extend(html_search_requests(row, plan, wanted_item, limit=limit))
        if not out["requests"]:
            out["payload_mode"] = "configuration_required"
            out["reason"] = "missing_html_search_url_templates"
    elif adapter_family == "external_tool_bridge":
        out["command_plan"] = external_tool_command_plan(row, plan, wanted_item, limit=limit)
        out["requires_operator"] = bool(out["command_plan"].get("requires_operator"))
        if out["command_plan"].get("can_execute_with_tool_runner"):
            out["payload_mode"] = "external_tool_command"
            out["can_execute_with_tool_runner"] = True
        elif out["command_plan"].get("configuration_reason") and out["command_plan"].get("auto_stage_tool_output"):
            out["payload_mode"] = "configuration_required"
            out["reason"] = out["command_plan"].get("configuration_reason")
        else:
            out["payload_mode"] = "operator_tool_output"
    elif adapter_family == "manual_source_cards":
        out["payload_mode"] = "operator_manual_cards"
        out["manual_input_plan"] = manual_source_input_plan(row, plan, wanted_item, limit=limit)
        out["requires_operator"] = True
    else:
        out["payload_mode"] = "unsupported_adapter"
        out["reason"] = "adapter_fetch_plan_unimplemented"
    out["can_execute_with_http_client"] = bool(out["requests"])
    return out


def _response_payload(response):
    headers = {}
    status = None
    payload = response
    if isinstance(response, tuple):
        payload = response[0] if response else None
        if len(response) > 1 and isinstance(response[1], dict):
            headers = response[1]
    elif isinstance(response, dict) and any(key in response for key in ("json", "text", "body", "headers", "status_code", "status")):
        headers = response.get("headers") if isinstance(response.get("headers"), dict) else {}
        status = response.get("status_code", response.get("status"))
        final_url = str(response.get("final_url") or response.get("response_url") or response.get("url") or "").strip()
        if final_url and "X-InkDrop-Final-Url" not in headers:
            headers = dict(headers)
            headers["X-InkDrop-Final-Url"] = final_url
        if "json" in response:
            payload = response.get("json")
        elif "text" in response:
            payload = response.get("text")
        else:
            payload = response.get("body")
    return {"payload": payload, "headers": headers, "status": status}


def _html_payload_for_request(payload, request):
    if isinstance(payload, dict) and ("text" in payload or "body" in payload or "source_url" in payload):
        out = dict(payload)
        out.setdefault("source_url", request.get("url"))
        out.setdefault("request_url", request.get("url"))
        return out
    if isinstance(payload, (dict, list)):
        return {
            "json": payload,
            "source_url": request.get("url"),
            "request_url": request.get("url"),
        }
    return {
        "text": payload if isinstance(payload, str) else "",
        "source_url": request.get("url"),
        "request_url": request.get("url"),
    }


def _call_http_get(http_get, request):
    try:
        return http_get(request)
    except TypeError:
        return http_get(request["url"], params=request.get("params"), headers=request.get("headers"))


def _safe_response_payload(http_get, request):
    try:
        return _response_payload(_call_http_get(http_get, request)), ""
    except Exception as exc:
        reason = getattr(exc, "reason", "") or type(exc).__name__
        return {}, f"{type(exc).__name__}: {reason}"


def _indexer_request_query(request):
    params = request.get("params") if isinstance(request, dict) else {}
    params = params if isinstance(params, dict) else {}
    return str(params.get("query") or params.get("q") or "").strip()


def _text_has_non_ascii(value):
    return any(ord(char) > 127 for char in str(value or ""))


def _indexer_error_should_try_ascii_fallback(query, requests, request_offset):
    if not _text_has_non_ascii(query):
        return False
    for request in list(requests or [])[request_offset + 1 :]:
        next_query = _indexer_request_query(request)
        if next_query and not _text_has_non_ascii(next_query):
            return True
    return False


def _record_partial_fetch_error(fetch_result, payload, request, error, *, stage=""):
    if not error:
        return
    request = request if isinstance(request, dict) else {}
    row = {
        "stage": stage or request.get("purpose") or request.get("request_id") or "http_request",
        "request_id": request.get("request_id"),
        "purpose": request.get("purpose"),
        "url_hash": providers.url_hash(str(request.get("url") or "")),
        "error": providers.clipped_text(error, 300),
    }
    row = {key: value for key, value in row.items() if value not in (None, "", [], {})}
    fetch_result.setdefault("partial_errors", []).append(row)
    if isinstance(payload, dict):
        payload.setdefault("partial_errors", []).append(row)


def _attach_rss_feed_evidence(fetch_result, target_payload, feed_payload, wanted_item=None, *, source_site="", limit=5):
    evidence = providers.rss_feed_evidence_from_payload(
        feed_payload,
        wanted_item,
        source_site=source_site,
        limit=limit,
    )
    if not evidence:
        return {}
    fetch_result["feed_evidence"] = evidence
    if isinstance(target_payload, dict):
        target_payload["feed_evidence"] = evidence
    return evidence


def _call_tool_runner(tool_runner, command_plan):
    try:
        return tool_runner(command_plan)
    except TypeError:
        return tool_runner(command_plan.get("argv"), env=command_plan.get("command_env"), cwd=command_plan.get("working_directory"))


def _safe_tool_payload(tool_runner, command_plan):
    try:
        return _response_payload(_call_tool_runner(tool_runner, command_plan)), ""
    except Exception as exc:
        reason = getattr(exc, "reason", "") or type(exc).__name__
        return {}, f"{type(exc).__name__}: {reason}"


def _archive_identifiers(search_payload, limit=5):
    if isinstance(search_payload, dict):
        items = search_payload.get("items") if isinstance(search_payload.get("items"), list) else []
        out = []
        for item in items:
            if not isinstance(item, dict):
                continue
            identifier = str(item.get("identifier") or "").strip()
            if identifier:
                out.append(identifier)
            if len(out) >= max(0, int(limit or 0)):
                break
        return out
    return []


def _mangadex_manga_rows(search_payload, wanted_item=None, limit=3, *, policy=None):
    if not isinstance(search_payload, dict):
        return []
    rows = search_payload.get("data") if isinstance(search_payload.get("data"), list) else []
    out = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        manga_id = str(item.get("id") or "").strip()
        if manga_id and providers.mangadex_manga_matches_wanted(item, wanted_item, policy=policy):
            out.append(item)
        if len(out) >= max(0, int(limit or 0)):
            break
    return out


def _fetch_deadline_reached(deadline):
    try:
        deadline = float(deadline)
    except (TypeError, ValueError):
        return False
    if deadline <= 0:
        return False
    return time.time() >= deadline


def _mangadex_feed_deadline_reached(payload):
    if not isinstance(payload, dict):
        return False
    if payload.get("feed_deadline_reached"):
        return True
    feed = payload.get("feed")
    return bool(isinstance(feed, dict) and feed.get("feed_deadline_reached"))


def _mangadex_payload_with_at_home(row, plan, wanted_item, payload, http_get, result, *, limit=20, deadline=None):
    payload = dict(payload or {})
    if not (
        _policy_bool(row, "fetch_at_home_pages")
        or _policy_bool(row, "allow_at_home_page_pack")
        or _policy_bool(row, "enable_at_home_page_pack")
    ):
        return payload, ""
    volume_pack = (
        providers.volume_page_pack_enabled(row)
        and providers.wanted_item_is_volume_unit(wanted_item)
        and not _policy_bool(row, "allow_single_chapter_volume_page_pack", False)
    )
    if volume_pack:
        if _mangadex_feed_deadline_reached(payload):
            # The feed pagination stopped at the wall-clock deadline, so the
            # matched chapter set may be incomplete. Refuse to build a volume
            # pack from it; the recorded reason keeps the retry honest.
            payload["volume_pack_blocked_reason"] = "volume_pack_feed_pages_deadline_reached"
            return payload, ""
        max_chapters = providers.volume_page_pack_max_chapters(row)
        matches = _mangadex_matching_chapter_rows(
            payload,
            row,
            wanted_item,
            limit=max_chapters + 1,
            volume_pack=True,
        )
        min_chapters = providers.volume_page_pack_min_chapters(row)
        if len(matches) > max_chapters:
            payload["volume_pack_blocked_reason"] = "volume_pack_chapter_count_exceeds_max"
            payload["volume_pack_matching_chapter_count"] = len(matches)
            return payload, ""
        if len(matches) < min_chapters:
            payload["volume_pack_blocked_reason"] = "volume_pack_chapter_count_below_min"
            payload["volume_pack_matching_chapter_count"] = len(matches)
            return payload, ""
    else:
        max_chapters = providers.int_value(_policy_value(row, "max_at_home_chapters", 1), 1)
        max_chapters = max(1, min(max_chapters or 1, 10))
        matches = _mangadex_matching_chapter_rows(payload, row, wanted_item, limit=max_chapters)
    at_home = dict(payload.get("at_home") or {})
    seen = set(at_home)
    for match in matches:
        chapter_id = str(match.get("id") or "").strip()
        if not chapter_id or chapter_id in seen:
            continue
        if _fetch_deadline_reached(deadline):
            # Stop cleanly at the slot deadline and keep the chapters already
            # fetched. A partial at_home map can never produce a partial
            # volume pack: the volume candidate builder requires page URLs
            # for every matched chapter.
            payload["at_home_deadline_reached"] = True
            if volume_pack:
                payload["volume_pack_blocked_reason"] = "volume_pack_at_home_deadline_reached"
            _record_partial_fetch_error(
                result,
                payload,
                {"request_id": "mangadex_at_home_server", "purpose": "fetch_mangadex_at_home_pages"},
                "fetch_deadline_reached",
                stage="mangadex_at_home",
            )
            break
        seen.add(chapter_id)
        request = mangadex_at_home_request(chapter_id, row, plan)
        response, error = _safe_response_payload(http_get, request)
        result["requests_made"].append(request)
        if error:
            return payload, error
        at_home[chapter_id] = response["payload"]
        result["response_headers"][f"at_home_{chapter_id}"] = response["headers"]
    if at_home:
        payload["at_home"] = at_home
    return payload, ""


def _mangadex_number_matches(candidate_value, wanted_value):
    candidate = _mangadex_number_filter(candidate_value)
    wanted = _mangadex_number_filter(wanted_value)
    if not wanted:
        return True
    if not candidate:
        return False
    try:
        return float(candidate) == float(wanted)
    except Exception:
        return candidate.lower() == wanted.lower()


def _mangadex_matching_chapter_rows(payload, row, wanted_item, *, limit=20, volume_pack=False):
    if not isinstance(payload, dict):
        return []
    allowed_languages = set(_mangadex_languages(row, wanted_item))
    wanted_volume = _mangadex_wanted_volume_number(wanted_item)
    wanted_chapter = _mangadex_wanted_chapter_number(wanted_item)
    out = []
    for chapter_row in _mangadex_feed_rows(payload.get("feed") if isinstance(payload.get("feed"), dict) else payload):
        if not isinstance(chapter_row, dict):
            continue
        attributes = chapter_row.get("attributes") if isinstance(chapter_row.get("attributes"), dict) else {}
        language = str(attributes.get("translatedLanguage") or "").strip().lower()
        if allowed_languages and language and language not in allowed_languages:
            continue
        if volume_pack:
            if not (providers.volume_page_pack_enabled(row) and providers.wanted_item_is_volume_unit(wanted_item)):
                continue
        else:
            if providers.page_pack_chapter_blocks_volume_target(attributes.get("chapter"), wanted_item, row):
                continue
            if not _mangadex_number_matches(attributes.get("chapter"), wanted_chapter):
                continue
        if not _mangadex_number_matches(attributes.get("volume"), wanted_volume):
            continue
        out.append(chapter_row)
        if len(out) >= max(0, int(limit or 0)):
            break
    return out


def _mangadex_feed_payload_has_match(payload, row, wanted_item, *, limit=20):
    if (
        providers.volume_page_pack_enabled(row)
        and providers.wanted_item_is_volume_unit(wanted_item)
        and not _policy_bool(row, "allow_single_chapter_volume_page_pack", False)
    ):
        return bool(
            _mangadex_matching_chapter_rows(
                payload,
                row,
                wanted_item,
                limit=max(1, int(limit or 1)),
                volume_pack=True,
            )
        )
    return bool(_mangadex_matching_chapter_rows(payload, row, wanted_item, limit=max(1, int(limit or 1))))


def _mangadex_page_payload(manga_row, feed_payload):
    return {
        "manga": manga_row,
        "feed": feed_payload,
    }


def _fetch_mangadex_feed_pages(manga_id, manga_row, row, plan, wanted_item, http_get, result, *, limit=20, deadline=None):
    page_limit = _mangadex_feed_page_limit(row, fallback=100)
    max_pages = _mangadex_feed_max_pages(row)
    volume_pack = (
        providers.volume_page_pack_enabled(row)
        and providers.wanted_item_is_volume_unit(wanted_item)
        and not _policy_bool(row, "allow_single_chapter_volume_page_pack", False)
    )
    selected_payload = None
    selected_headers = {}
    last_payload = None
    last_headers = {}
    combined_feed = None
    deadline_stopped = False
    for page_index in range(max_pages):
        if _fetch_deadline_reached(deadline):
            # Wall-clock slot deadline: stop paging cleanly and keep the pages
            # already fetched instead of dying mid-slot with nothing recorded.
            deadline_stopped = True
            _record_partial_fetch_error(
                result,
                last_payload,
                {"request_id": "mangadex_manga_feed", "purpose": "fetch_mangadex_manga_feed"},
                "fetch_deadline_reached",
                stage="mangadex_feed_pages",
            )
            break
        offset = page_index * page_limit
        request = mangadex_feed_request(manga_id, row, plan, wanted_item, limit=page_limit, offset=offset)
        response, error = _safe_response_payload(http_get, request)
        result["requests_made"].append(request)
        if error:
            result["reason"] = "http_request_failed"
            result["error"] = error
            return None, {}, error
        feed_payload = response["payload"]
        if volume_pack:
            if combined_feed is None:
                combined_feed = dict(feed_payload or {})
                combined_feed["data"] = []
            combined_rows = combined_feed.get("data") if isinstance(combined_feed.get("data"), list) else []
            combined_rows.extend(_mangadex_feed_rows(feed_payload))
            combined_feed["data"] = combined_rows
            combined_feed["limit"] = page_limit
            combined_feed["offset"] = 0
            if _mangadex_feed_total(feed_payload):
                combined_feed["total"] = _mangadex_feed_total(feed_payload)
            payload = {
                "manga": manga_row,
                "feed": combined_feed,
            }
        else:
            payload = {
                "manga": manga_row,
                "feed": feed_payload,
            }
        last_payload = payload
        last_headers = response["headers"]
        if not volume_pack and _mangadex_feed_payload_has_match(payload, row, wanted_item, limit=limit):
            selected_payload = payload
            selected_headers = response["headers"]
            break
        rows = _mangadex_feed_rows(feed_payload)
        total = _mangadex_feed_total(feed_payload)
        if len(rows) < page_limit:
            break
        if total and offset + len(rows) >= total:
            break
    out_payload = selected_payload or last_payload
    if deadline_stopped and isinstance(out_payload, dict):
        out_payload["feed_deadline_reached"] = True
        if isinstance(out_payload.get("feed"), dict):
            out_payload["feed"]["feed_deadline_reached"] = True
    return out_payload, selected_headers or last_headers, ""


def _suwayomi_search_manga_rows(search_payload, wanted_item=None, limit=3, *, policy=None):
    if not isinstance(search_payload, dict):
        return []
    rows = search_payload.get("mangaList") if isinstance(search_payload.get("mangaList"), list) else []
    out = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        manga_id = str(item.get("id") or "").strip()
        if manga_id and providers.suwayomi_manga_matches_wanted(item, wanted_item, policy=policy):
            out.append(item)
        if len(out) >= max(0, int(limit or 0)):
            break
    return out


def _suwayomi_graphql_field(payload, field_name):
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    value = data.get(field_name)
    return value if isinstance(value, dict) else {}


def _suwayomi_chapter_rows(value):
    if isinstance(value, dict):
        rows = value.get("chapters")
    else:
        rows = value
    return [row for row in (rows or []) if isinstance(row, dict)] if isinstance(rows, list) else []


def _suwayomi_chapter_number(chapter_row):
    chapter_row = chapter_row if isinstance(chapter_row, dict) else {}
    value = providers.first_text(chapter_row.get("chapterNumber"), chapter_row.get("chapter"), chapter_row.get("number"))
    return _mangadex_number_filter(value)


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


def _suwayomi_volume_number(chapter_row):
    evidence = providers.suwayomi_explicit_volume_evidence(chapter_row)
    if evidence.get("conflict") or evidence.get("malformed"):
        return ""
    return _mangadex_number_filter(evidence.get("volume_number"))


def _suwayomi_chapter_matches_wanted(chapter_row, wanted_item=None, registry_row=None, *, volume_pack=False):
    return bool(
        providers.suwayomi_chapter_membership(
            chapter_row,
            wanted_item,
            registry_row,
            volume_pack=volume_pack,
        ).get("matches")
    )


def _suwayomi_matching_chapter_rows(chapters, wanted_item=None, *, registry_row=None, limit=20, volume_pack=False):
    out = []
    for chapter_row in _suwayomi_chapter_rows(chapters):
        if not _suwayomi_chapter_matches_wanted(chapter_row, wanted_item, registry_row, volume_pack=volume_pack):
            continue
        out.append(chapter_row)
        if len(out) >= max(0, int(limit or 0)):
            break
    return out


def _suwayomi_payload_has_match(payload, wanted_item=None, registry_row=None):
    payload = payload if isinstance(payload, dict) else {}
    if providers.suwayomi_volume_page_pack_candidates_from_payload(payload, registry_row, wanted_item, limit=1):
        return True
    pages_by_chapter = payload.get("pages_by_chapter") if isinstance(payload.get("pages_by_chapter"), dict) else {}
    for chapter_row in _suwayomi_matching_chapter_rows(
        payload.get("chapters"),
        wanted_item,
        registry_row=registry_row,
        limit=20,
    ):
        chapter_id = str(chapter_row.get("id") or "").strip()
        page_payload = pages_by_chapter.get(chapter_id) if chapter_id else None
        pages = page_payload.get("pages") if isinstance(page_payload, dict) else []
        if isinstance(pages, list) and pages:
            return True
    return False


def _suwayomi_payload_from_fetch(source_row, search_manga_row, fetch_payload, row):
    data = _suwayomi_graphql_field(fetch_payload, "fetchMangaAndChapters")
    fetched_manga = data.get("manga") if isinstance(data.get("manga"), dict) else {}
    manga = dict(search_manga_row or {})
    manga.update({key: value for key, value in fetched_manga.items() if value not in (None, "", [], {})})
    return {
        "source": dict(source_row or {}),
        "manga": manga,
        "chapters": _suwayomi_chapter_rows(data),
        "pages_by_chapter": {},
        "base_url": _suwayomi_base_url(row),
    }


def _suwayomi_payload_from_rest_chapters(source_row, search_manga_row, chapters_payload, row):
    return {
        "source": dict(source_row or {}),
        "manga": dict(search_manga_row or {}),
        "chapters": _suwayomi_chapter_rows(chapters_payload),
        "pages_by_chapter": {},
        "base_url": _suwayomi_base_url(row),
        "chapter_fetch_fallback": "rest_chapters",
    }


def _fetch_suwayomi_source_payloads(row, plan, wanted_item, http_get, result, *, limit=20):
    planned_requests = [request for request in result["fetch_plan"].get("requests") or [] if isinstance(request, dict)]
    first_request = next((request for request in planned_requests if request.get("request_id") == "suwayomi_source_list"), None)
    first_request = first_request or (planned_requests[0] if planned_requests else None)
    extension_request = next((request for request in planned_requests if request.get("request_id") == "suwayomi_extension_list"), None)
    if not first_request:
        result["reason"] = "missing_suwayomi_request"
        return result
    variant_counts = []
    partial_errors = []
    meta_fallbacks = []
    source_response, error = _safe_response_payload(http_get, first_request)
    result["requests_made"].append(first_request)
    if error:
        result["reason"] = "http_request_failed"
        result["error"] = error
        return result
    source_payload = source_response["payload"]
    result["response_headers"]["source_list"] = source_response["headers"]
    extension_payload = None
    if extension_request:
        extension_response, extension_error = _safe_response_payload(http_get, extension_request)
        result["requests_made"].append(extension_request)
        if extension_error:
            partial_errors.append({"stage": "extension_list", "error": extension_error})
        else:
            extension_payload = extension_response["payload"]
            result["response_headers"]["extensions"] = extension_response["headers"]
    annotated_sources = _suwayomi_annotate_source_rows(source_payload, extension_payload)
    sources = _suwayomi_selected_sources(row, annotated_sources, wanted_item)
    source_selection = _suwayomi_source_selection_summary(row, annotated_sources, sources, wanted_item)
    if source_selection:
        result["suwayomi_source_selection"] = source_selection
    extension_health = _suwayomi_extension_health_summary(extension_payload, sources)
    if extension_health:
        result["suwayomi_extension_health"] = extension_health
    max_manga_matches = providers.int_value(_policy_value(row, "suwayomi_max_manga_matches", 3), 3)
    max_manga_matches = max(1, min(max_manga_matches or 3, 10))
    volume_pack = (
        providers.volume_page_pack_enabled(row)
        and providers.wanted_item_is_volume_unit(wanted_item)
        and not _policy_bool(row, "allow_single_chapter_volume_page_pack", False)
    )
    max_chapters = providers.int_value(_policy_value(row, "suwayomi_max_chapters", 3), 3)
    max_chapters = max(1, min(max_chapters or 3, 20))
    max_volume_pack_chapters = providers.volume_page_pack_max_chapters(row)
    min_volume_pack_chapters = providers.volume_page_pack_min_chapters(row)
    seen_manga = set()
    source_search_cooldowns = {}
    source_runtime_skips = []
    skip_after_search_error = _policy_bool(row, "suwayomi_skip_source_after_search_error", True)
    search_requests = suwayomi_search_requests(row, plan, wanted_item, limit=limit, sources=sources)
    for search_index, search_request in enumerate(search_requests):
        query = search_request.get("query_variant") or (search_request.get("params") or {}).get("searchTerm")
        source_id = search_request.get("source_id")
        source_display_name = search_request.get("source_display_name")
        if skip_after_search_error and source_id in source_search_cooldowns:
            source_runtime_skips.append(
                {
                    "stage": "source_search_skipped_after_error",
                    "query": query,
                    "source_id": source_id,
                    "source_display_name": source_display_name,
                    "reason": "prior_source_search_error",
                    "previous_error": source_search_cooldowns.get(source_id),
                }
            )
            continue
        search_response, search_error = _safe_response_payload(http_get, search_request)
        result["requests_made"].append(search_request)
        if search_error:
            partial_errors.append(
                {
                    "stage": "source_search",
                    "query": query,
                    "source_id": source_id,
                    "source_display_name": source_display_name,
                    "error": search_error,
                }
            )
            if skip_after_search_error and source_id:
                source_search_cooldowns[source_id] = providers.clipped_text(search_error, 300)
            continue
        search_payload = search_response["payload"]
        rows = search_payload.get("mangaList") if isinstance(search_payload, dict) and isinstance(search_payload.get("mangaList"), list) else []
        manga_rows = _suwayomi_search_manga_rows(
            search_payload,
            wanted_item=wanted_item,
            limit=max_manga_matches,
            policy=_source_query_alias_policy(row),
        )
        variant_counts.append(
            {
                "query": query,
                "source_id": source_id,
                "source_display_name": search_request.get("source_display_name"),
                "source_extension_pkg_name": search_request.get("source_extension_pkg_name"),
                "source_extension_obsolete": search_request.get("source_extension_obsolete"),
                "source_extension_has_update": search_request.get("source_extension_has_update"),
                "results": len(rows),
                "matching_manga": len(manga_rows),
            }
        )
        result["response_headers"][f"search_{search_index}"] = search_response["headers"]
        for manga_row in manga_rows:
            manga_id = str(manga_row.get("id") or "").strip()
            source_key = f"{source_id}:{manga_id}"
            if not manga_id or source_key in seen_manga:
                continue
            seen_manga.add(source_key)
            manga_request = suwayomi_fetch_manga_and_chapters_request(manga_id, row, plan)
            if not manga_request:
                continue
            manga_response, manga_error = _safe_response_payload(http_get, manga_request)
            manga_request["source_id"] = source_id
            manga_request["source_display_name"] = search_request.get("source_display_name")
            manga_request["manga_id"] = manga_id
            result["requests_made"].append(manga_request)
            rest_chapters_payload = None
            rest_chapters_headers = {}
            if manga_error:
                if _policy_bool(row, "suwayomi_rest_chapter_fallback_enabled", True):
                    rest_request = suwayomi_fetch_manga_chapters_rest_request(manga_id, row, plan)
                    if rest_request:
                        rest_request["source_id"] = source_id
                        rest_request["source_display_name"] = search_request.get("source_display_name")
                        rest_request["manga_id"] = manga_id
                        rest_response, rest_error = _safe_response_payload(http_get, rest_request)
                        result["requests_made"].append(rest_request)
                        if rest_error:
                            partial_errors.append({"stage": "manga_chapters_rest_fallback", "query": query, "source_id": source_id, "manga_id": manga_id, "error": rest_error})
                        elif _suwayomi_chapter_rows(rest_response["payload"]):
                            rest_chapters_payload = rest_response["payload"]
                            rest_chapters_headers = rest_response["headers"]
                            meta_fallbacks.append({"stage": "manga_chapters_rest", "query": query, "source_id": source_id, "manga_id": manga_id, "previous_error": providers.clipped_text(manga_error, 120)})
                        else:
                            partial_errors.append({"stage": "manga_chapters_rest_fallback", "query": query, "source_id": source_id, "manga_id": manga_id, "error": "no_chapters"})
                if rest_chapters_payload is None:
                    partial_errors.append({"stage": "manga_chapters", "query": query, "source_id": source_id, "manga_id": manga_id, "error": manga_error})
                    continue
            if rest_chapters_payload is None and isinstance(manga_response.get("payload"), dict) and manga_response["payload"].get("errors"):
                fallback_request = suwayomi_fetch_manga_and_chapters_request(manga_id, row, plan, include_meta=False)
                if not fallback_request:
                    partial_errors.append({"stage": "manga_chapters_meta", "query": query, "source_id": source_id, "manga_id": manga_id, "error": "graphql_errors"})
                    continue
                fallback_request["source_id"] = source_id
                fallback_request["source_display_name"] = search_request.get("source_display_name")
                fallback_request["manga_id"] = manga_id
                fallback_response, fallback_error = _safe_response_payload(http_get, fallback_request)
                result["requests_made"].append(fallback_request)
                if fallback_error:
                    if _policy_bool(row, "suwayomi_rest_chapter_fallback_enabled", True):
                        rest_request = suwayomi_fetch_manga_chapters_rest_request(manga_id, row, plan)
                        if rest_request:
                            rest_request["source_id"] = source_id
                            rest_request["source_display_name"] = search_request.get("source_display_name")
                            rest_request["manga_id"] = manga_id
                            rest_response, rest_error = _safe_response_payload(http_get, rest_request)
                            result["requests_made"].append(rest_request)
                            if rest_error:
                                partial_errors.append({"stage": "manga_chapters_rest_fallback", "query": query, "source_id": source_id, "manga_id": manga_id, "error": rest_error})
                            elif _suwayomi_chapter_rows(rest_response["payload"]):
                                rest_chapters_payload = rest_response["payload"]
                                rest_chapters_headers = rest_response["headers"]
                                meta_fallbacks.append({"stage": "manga_chapters_rest", "query": query, "source_id": source_id, "manga_id": manga_id, "previous_error": providers.clipped_text(fallback_error, 120)})
                            else:
                                partial_errors.append({"stage": "manga_chapters_rest_fallback", "query": query, "source_id": source_id, "manga_id": manga_id, "error": "no_chapters"})
                    if rest_chapters_payload is None:
                        partial_errors.append({"stage": "manga_chapters_no_meta_fallback", "query": query, "source_id": source_id, "manga_id": manga_id, "error": fallback_error})
                        continue
                if rest_chapters_payload is None and isinstance(fallback_response.get("payload"), dict) and fallback_response["payload"].get("errors"):
                    if _policy_bool(row, "suwayomi_rest_chapter_fallback_enabled", True):
                        rest_request = suwayomi_fetch_manga_chapters_rest_request(manga_id, row, plan)
                        if rest_request:
                            rest_request["source_id"] = source_id
                            rest_request["source_display_name"] = search_request.get("source_display_name")
                            rest_request["manga_id"] = manga_id
                            rest_response, rest_error = _safe_response_payload(http_get, rest_request)
                            result["requests_made"].append(rest_request)
                            if rest_error:
                                partial_errors.append({"stage": "manga_chapters_rest_fallback", "query": query, "source_id": source_id, "manga_id": manga_id, "error": rest_error})
                            elif _suwayomi_chapter_rows(rest_response["payload"]):
                                rest_chapters_payload = rest_response["payload"]
                                rest_chapters_headers = rest_response["headers"]
                                meta_fallbacks.append({"stage": "manga_chapters_rest", "query": query, "source_id": source_id, "manga_id": manga_id, "previous_error": "graphql_errors"})
                            else:
                                partial_errors.append({"stage": "manga_chapters_rest_fallback", "query": query, "source_id": source_id, "manga_id": manga_id, "error": "no_chapters"})
                    if rest_chapters_payload is None:
                        partial_errors.append({"stage": "manga_chapters_no_meta_fallback", "query": query, "source_id": source_id, "manga_id": manga_id, "error": "graphql_errors"})
                        continue
                if rest_chapters_payload is None:
                    manga_response = fallback_response
                    meta_fallbacks.append({"stage": "manga_chapters", "query": query, "source_id": source_id, "manga_id": manga_id})
            source_row = next((source for source in sources if str(source.get("id") or "") == str(source_id)), {})
            if rest_chapters_payload is not None:
                payload = _suwayomi_payload_from_rest_chapters(source_row, manga_row, rest_chapters_payload, row)
                manga_response = {"payload": rest_chapters_payload, "headers": rest_chapters_headers}
            else:
                payload = _suwayomi_payload_from_fetch(source_row, manga_row, manga_response["payload"], row)
            payload["query_variants"] = list(result["fetch_plan"].get("query_variants") or [])
            payload["variant_result_counts"] = variant_counts
            if extension_health:
                payload["suwayomi_extension_health"] = extension_health
            if source_selection:
                payload["suwayomi_source_selection"] = source_selection
            if source_runtime_skips:
                payload["source_runtime_skips"] = list(source_runtime_skips)
            if meta_fallbacks:
                payload["meta_fallbacks"] = list(meta_fallbacks)
            payload["search"] = search_payload
            payload["source_search_query"] = query
            matching_chapters = _suwayomi_matching_chapter_rows(
                payload.get("chapters"),
                wanted_item,
                registry_row=row,
                limit=(max_volume_pack_chapters + 1) if volume_pack else max_chapters,
                volume_pack=volume_pack,
            )
            if (
                volume_pack
                and not providers.single_chapter_volume_page_pack_allowed(row)
                and len(matching_chapters) < min_volume_pack_chapters
                and not (
                    len(matching_chapters) == 1
                    and providers.suwayomi_chapter_is_single_volume_artifact(matching_chapters[0], wanted_item)
                )
            ):
                payload["volume_pack_blocked_reason"] = "volume_pack_chapter_count_below_min"
                payload["volume_pack_matching_chapter_count"] = len(matching_chapters)
                matching_chapters = []
            if volume_pack and len(matching_chapters) > max_volume_pack_chapters:
                payload["volume_pack_blocked_reason"] = "volume_pack_chapter_count_exceeds_max"
                payload["volume_pack_matching_chapter_count"] = len(matching_chapters)
                matching_chapters = []
            for chapter_row in matching_chapters:
                chapter_id = str(chapter_row.get("id") or "").strip()
                pages_request = suwayomi_fetch_chapter_pages_request(chapter_id, row, plan)
                if not pages_request:
                    continue
                pages_request["source_id"] = source_id
                pages_request["source_display_name"] = search_request.get("source_display_name")
                pages_request["manga_id"] = manga_id
                pages_request["chapter_id"] = chapter_id
                pages_response, pages_error = _safe_response_payload(http_get, pages_request)
                result["requests_made"].append(pages_request)
                if pages_error:
                    partial_errors.append({"stage": "chapter_pages", "query": query, "source_id": source_id, "manga_id": manga_id, "chapter_id": chapter_id, "error": pages_error})
                    continue
                if isinstance(pages_response["payload"], dict) and pages_response["payload"].get("errors"):
                    fallback_pages_request = suwayomi_fetch_chapter_pages_request(chapter_id, row, plan, include_meta=False)
                    if not fallback_pages_request:
                        partial_errors.append({"stage": "chapter_pages_meta", "query": query, "source_id": source_id, "manga_id": manga_id, "chapter_id": chapter_id, "error": "graphql_errors"})
                        continue
                    fallback_pages_request["source_id"] = source_id
                    fallback_pages_request["source_display_name"] = search_request.get("source_display_name")
                    fallback_pages_request["manga_id"] = manga_id
                    fallback_pages_request["chapter_id"] = chapter_id
                    fallback_pages_response, fallback_pages_error = _safe_response_payload(http_get, fallback_pages_request)
                    result["requests_made"].append(fallback_pages_request)
                    if fallback_pages_error:
                        partial_errors.append({"stage": "chapter_pages_no_meta_fallback", "query": query, "source_id": source_id, "manga_id": manga_id, "chapter_id": chapter_id, "error": fallback_pages_error})
                        continue
                    if isinstance(fallback_pages_response["payload"], dict) and fallback_pages_response["payload"].get("errors"):
                        partial_errors.append({"stage": "chapter_pages_no_meta_fallback", "query": query, "source_id": source_id, "manga_id": manga_id, "chapter_id": chapter_id, "error": "graphql_errors"})
                        continue
                    pages_response = fallback_pages_response
                    meta_fallbacks.append({"stage": "chapter_pages", "query": query, "source_id": source_id, "manga_id": manga_id, "chapter_id": chapter_id})
                    payload["meta_fallbacks"] = list(meta_fallbacks)
                page_payload = _suwayomi_graphql_field(pages_response["payload"], "fetchChapterPages")
                if page_payload:
                    payload.setdefault("pages_by_chapter", {})[chapter_id] = page_payload
                    result["response_headers"][f"pages_{chapter_id}"] = pages_response["headers"]
            result["payloads"].append(payload)
            result["response_headers"][str(len(result["payloads"]) - 1)] = manga_response["headers"]
            if _suwayomi_payload_has_match(payload, wanted_item, registry_row=row):
                result["query_variants"] = list(result["fetch_plan"].get("query_variants") or [])
                result["variant_result_counts"] = variant_counts
                if meta_fallbacks:
                    result["meta_fallbacks"] = list(meta_fallbacks)
                if partial_errors:
                    result["partial_errors"] = partial_errors
                if source_runtime_skips:
                    result["source_runtime_skips"] = source_runtime_skips
                result["ok"] = True
                return result
    result["query_variants"] = list(result["fetch_plan"].get("query_variants") or [])
    result["variant_result_counts"] = variant_counts
    if meta_fallbacks:
        result["meta_fallbacks"] = list(meta_fallbacks)
    if partial_errors:
        result["partial_errors"] = partial_errors
    if source_runtime_skips:
        result["source_runtime_skips"] = source_runtime_skips
    result["ok"] = True
    return result


def _response_payload_bytes(payload):
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, bytearray):
        return bytes(payload)
    if isinstance(payload, str):
        return payload.encode("utf-8", errors="replace")
    if isinstance(payload, (dict, list)):
        return json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
    return str(payload or "").encode("utf-8", errors="replace")


def _headers_truncated(headers):
    headers = headers if isinstance(headers, dict) else {}
    return str(headers.get("X-InkDrop-Truncated") or headers.get("x-inkdrop-truncated") or "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _http_url(value):
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    if str(parsed.scheme or "").lower() not in {"http", "https"}:
        return ""
    return text


def _indexer_pack_detail_urls(result):
    result = result if isinstance(result, dict) else {}
    urls = []
    seen = set()

    def add(source, value):
        url = _http_url(value)
        if not url or url in seen:
            return
        seen.add(url)
        urls.append((source, url))

    add("download_metadata", providers.first_text(result.get("downloadUrl"), result.get("download_url"), result.get("download")))
    add("detail_page", providers.first_text(result.get("infoUrl"), result.get("info_url"), result.get("detailsUrl")))
    return urls[:2]


def _indexer_pack_detail_sidecar_urls(headers, result):
    headers = headers if isinstance(headers, dict) else {}
    result = result if isinstance(result, dict) else {}
    lowered = {str(key or "").strip().lower(): str(value or "").strip() for key, value in headers.items()}
    urls = []
    seen = set()

    def add(value):
        url = _http_url(value)
        if not url or url in seen:
            return
        seen.add(url)
        urls.append(url)

    for key in INDEXER_PACK_DETAIL_HEADER_KEYS:
        add(lowered.get(key))
    download_url = providers.first_text(result.get("downloadUrl"), result.get("download_url"), result.get("download"))
    for key in ("infoUrl", "info_url", "detailsUrl"):
        value = providers.first_text(result.get(key))
        if value and value != download_url:
            add(value)
    return urls[:3]


def _indexer_detail_status(entries, truncated, had_error=False):
    if entries:
        return "partial_ok" if truncated else "ok"
    if had_error:
        return "error"
    return "partial_no_entries" if truncated else "no_entries"


def _indexer_pack_detail_allowed_hosts(row):
    return _policy_host_list(
        row,
        (
            "pack_detail_allowed_hosts",
            "allowed_pack_detail_hosts",
            "indexer_pack_detail_allowed_hosts",
            "indexer_allowed_hosts",
            "source_allowed_hosts",
        ),
    )


def _should_fetch_indexer_pack_detail(row, result):
    if not isinstance(result, dict):
        return False
    if not _policy_bool(row, "pack_detail_fetch", True) or _policy_bool(row, "disable_pack_detail_fetch", False):
        return False
    if providers.indexer_pack_manifest_entries(result):
        return False
    title = providers.first_text(result.get("title"), result.get("releaseTitle"), result.get("release_title"), result.get("name"))
    if not providers.looks_pack_like(title):
        return False
    protocol = providers.normalize_protocol(providers.first_text(result.get("protocol"), result.get("downloadProtocol")))
    locator_ext = providers.normalize_extension(providers.first_text(result.get("downloadUrl"), result.get("download_url"), title))
    if not protocol and locator_ext == ".nzb":
        protocol = "usenet"
    elif not protocol and locator_ext == ".torrent":
        protocol = "torrent"
    return bool(protocol in {"torrent", "usenet"} and _indexer_pack_detail_urls(result))


def _indexer_pack_detail_priority(row, result, wanted_item=None, index=0):
    result = result if isinstance(result, dict) else {}
    if not _should_fetch_indexer_pack_detail(row, result):
        return (100, int(index or 0))
    title = providers.normalized_query(
        providers.first_text(result.get("title"), result.get("releaseTitle"), result.get("release_title"), result.get("name"))
    ).lower()
    priority = 40
    if re.search(r"\bweekly[\W_]+(?:comics?|releases?)\b", title):
        priority = 0
    elif re.search(
        r"\b(?:dc|marvel|image|dark[\W_]+horse|idw|boom)(?:[\W_]+comics?)?[\W_]+weekly[\W_]+releases\b",
        title,
    ):
        priority = 0
    elif re.search(r"\b(?:dc|marvel|image|dark[\W_]+horse|idw|boom)(?:[\W_]+comics?)?[\W_]+week\b", title):
        priority = 20
    elif "pack" in title or "batch" in title or "complete" in title:
        priority = 30
    current_year = time.localtime().tm_year
    years = {str(year) for year in _wanted_years(wanted_item, include_current=True)}
    years.add(str(current_year - 1))
    if years and any(year in title for year in years):
        priority -= 5
    query_result_index = providers.int_value(result.get("_inkdrop_query_result_index"), int(index or 0))
    query_index = providers.int_value(result.get("_inkdrop_query_index"), int(index or 0))
    return (priority, query_result_index, query_index, int(index or 0))


def _merge_indexer_pack_detail(result, *, entries, source, status, truncated=False, headers=None, sidecar_results=None, error=""):
    merged = dict(result or {})
    merged["pack_detail_entries"] = entries
    merged["pack_detail_source"] = source
    merged["pack_detail_status"] = status
    merged["pack_detail_entry_count"] = len(entries or [])
    merged["pack_detail_truncated"] = bool(truncated)
    if headers:
        merged["pack_detail_content_type"] = (headers or {}).get("Content-Type") or (headers or {}).get("content-type")
    if sidecar_results:
        merged["pack_detail_sidecar_results"] = sidecar_results
    if error:
        merged["pack_detail_error"] = error
    return merged


def _indexer_pack_detail_matches_wanted(row, result_row, wanted_item=None):
    if not isinstance(result_row, dict) or not isinstance(wanted_item, dict):
        return False
    if not result_row.get("pack_detail_entries"):
        return False
    try:
        candidate = providers.prowlarr_candidate_from_result(result_row, row, wanted_item)
    except Exception:
        return False
    return bool(candidate.get("pack_contents_match"))


def _enrich_indexer_result_with_pack_detail(row, result_row, http_get, fetch_result, fetch_state):
    if not _should_fetch_indexer_pack_detail(row, result_row):
        return result_row
    max_fetches = providers.int_value(
        _policy_value(row, "pack_detail_max_fetches", _policy_value(row, "max_pack_detail_fetches", 3)),
        3,
    )
    max_fetches = max(0, min(max_fetches, 20))
    if int(fetch_state.get("count") or 0) >= max_fetches:
        return result_row
    protocol = providers.normalize_protocol(
        providers.first_text(result_row.get("protocol"), result_row.get("downloadProtocol"))
    )
    download_ext = providers.normalize_extension(
        providers.first_text(result_row.get("downloadUrl"), result_row.get("download_url"), result_row.get("title"))
    )
    if not protocol and download_ext == ".nzb":
        protocol = "usenet"
    elif not protocol and download_ext == ".torrent":
        protocol = "torrent"
    detail_max_bytes = providers.int_value(
        _policy_value(row, "pack_detail_max_bytes", INDEXER_PACK_DETAIL_MAX_BYTES),
        INDEXER_PACK_DETAIL_MAX_BYTES,
    )
    detail_max_bytes = max(1024, min(detail_max_bytes, 50 * 1024 * 1024))
    sidecar_max_bytes = providers.int_value(
        _policy_value(row, "pack_detail_sidecar_max_bytes", INDEXER_PACK_DETAIL_SIDECAR_MAX_BYTES),
        INDEXER_PACK_DETAIL_SIDECAR_MAX_BYTES,
    )
    sidecar_max_bytes = max(1024, min(sidecar_max_bytes, 16 * 1024 * 1024))
    allowed_hosts = _indexer_pack_detail_allowed_hosts(row)
    last_error = ""
    for source, detail_url in _indexer_pack_detail_urls(result_row):
        if int(fetch_state.get("count") or 0) >= max_fetches:
            break
        request = indexer_pack_detail_request(
            detail_url,
            int(fetch_state.get("count") or 0),
            source=source,
            max_bytes=detail_max_bytes,
            allowed_hosts=allowed_hosts,
        )
        fetch_state["count"] = int(fetch_state.get("count") or 0) + 1
        response, error = _safe_response_payload(http_get, request)
        fetch_result["requests_made"].append(request)
        if error:
            last_error = error
            continue
        response_key = f"pack_detail_{fetch_state['count'] - 1}"
        fetch_result["response_headers"][response_key] = response["headers"]
        body = _response_payload_bytes(response.get("payload"))
        entries = providers.indexer_pack_detail_entries_from_bytes(body, protocol=protocol)
        truncated = _headers_truncated(response.get("headers"))
        sidecar_results = []
        if not entries:
            for sidecar_url in _indexer_pack_detail_sidecar_urls(response.get("headers"), result_row):
                if int(fetch_state.get("count") or 0) >= max_fetches:
                    break
                sidecar_record = {"url_hash": providers.url_hash(sidecar_url)}
                sidecar_request = indexer_pack_detail_request(
                    sidecar_url,
                    int(fetch_state.get("count") or 0),
                    source="download_metadata_sidecar",
                    max_bytes=sidecar_max_bytes,
                    allowed_hosts=allowed_hosts,
                )
                fetch_state["count"] = int(fetch_state.get("count") or 0) + 1
                sidecar_response, sidecar_error = _safe_response_payload(http_get, sidecar_request)
                fetch_result["requests_made"].append(sidecar_request)
                if sidecar_error:
                    sidecar_record.update({"status": "error", "error": sidecar_error})
                    sidecar_results.append(sidecar_record)
                    continue
                sidecar_key = f"pack_detail_sidecar_{fetch_state['count'] - 1}"
                fetch_result["response_headers"][sidecar_key] = sidecar_response["headers"]
                sidecar_entries = providers.indexer_pack_detail_entries_from_bytes(
                    _response_payload_bytes(sidecar_response.get("payload")),
                    protocol=protocol,
                )
                sidecar_truncated = _headers_truncated(sidecar_response.get("headers"))
                sidecar_record.update(
                    {
                        "status": "ok" if sidecar_entries else "no_entries",
                        "entries": len(sidecar_entries),
                        "truncated": sidecar_truncated,
                        "content_type": sidecar_response["headers"].get("Content-Type")
                        or sidecar_response["headers"].get("content-type"),
                    }
                )
                sidecar_results.append(sidecar_record)
                if sidecar_entries:
                    entries = sidecar_entries
                    truncated = sidecar_truncated
                    source = "download_metadata_sidecar"
                    break
        status = _indexer_detail_status(entries, truncated)
        return _merge_indexer_pack_detail(
            result_row,
            entries=entries,
            source=source,
            status=status,
            truncated=truncated,
            headers=response.get("headers"),
            sidecar_results=sidecar_results,
        )
    if last_error:
        return _merge_indexer_pack_detail(
            result_row,
            entries=[],
            source="download_metadata",
            status="error",
            error=last_error,
        )
    return result_row


def _indexer_payload_with_pack_details(row, plan, wanted_item, payload, http_get, fetch_result, *, limit=20, fetch_state=None):
    if not payload:
        return payload
    results = payload.get("results") if isinstance(payload, dict) and isinstance(payload.get("results"), list) else payload
    if not isinstance(results, list):
        return payload
    if not isinstance(fetch_state, dict):
        fetch_state = {"count": 0}
    fetch_state["count"] = int(fetch_state.get("count") or 0)
    enriched_by_index = {}
    changed = False
    detail_candidates = [
        (index, item)
        for index, item in enumerate(results)
        if isinstance(item, dict) and _should_fetch_indexer_pack_detail(row, item)
    ]
    detail_candidates.sort(
        key=lambda pair: _indexer_pack_detail_priority(row, pair[1], wanted_item=wanted_item, index=pair[0])
    )
    for index, item in detail_candidates:
        enriched = _enrich_indexer_result_with_pack_detail(row, item, http_get, fetch_result, fetch_state)
        if enriched is not item:
            enriched_by_index[index] = enriched
            changed = True
            if _indexer_pack_detail_matches_wanted(row, enriched, wanted_item):
                break
    enriched_results = [
        enriched_by_index.get(index, item)
        for index, item in enumerate(results)
    ]
    if not changed:
        if isinstance(payload, dict):
            out = dict(payload)
            out.setdefault("pack_detail_fetch_count", int(fetch_state.get("count") or 0))
            return out
        return payload
    if isinstance(payload, dict):
        out = dict(payload)
        out["results"] = enriched_results
        out["pack_detail_fetch_count"] = int(fetch_state.get("count") or 0)
        return out
    return enriched_results


def _indexer_payload_results(payload):
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        return payload.get("results") or []
    if isinstance(payload, list):
        return payload
    return []


def _indexer_payload_has_pack_detail_match(row, payload, wanted_item=None):
    return any(
        _indexer_pack_detail_matches_wanted(row, item, wanted_item)
        for item in _indexer_payload_results(payload)
        if isinstance(item, dict)
    )


def _indexer_payload_results_for_adapter(adapter_family, payload, row=None):
    rows = _indexer_payload_results(payload)
    if rows:
        return rows
    if str(adapter_family or "").strip() in INDEXER_RESULT_ADAPTER_FAMILIES:
        return providers.indexer_result_rows_from_payload(payload, row)
    return []


def _indexer_result_identity(result):
    result = result if isinstance(result, dict) else {}
    for key in ("guid", "downloadUrl", "download_url", "magnetUrl", "infoHash", "infoUrl", "detailsUrl", "title"):
        value = str(result.get(key) or "").strip()
        if value:
            return f"{key}:{value.lower()}"
    return json.dumps(result, sort_keys=True, ensure_ascii=True)


def fetch_payloads(row, plan, wanted_item=None, *, http_get=None, tool_runner=None, limit=20, deadline=None):
    fetch_plan = adapter_fetch_plan(row, plan, wanted_item, limit=limit)
    result = {
        "fetch_contract_version": CONTRACT_VERSION,
        "ok": False,
        "provider_id": fetch_plan.get("provider_id"),
        "fetch_plan": fetch_plan,
        "payloads": [],
        "response_headers": {},
        "requests_made": [],
    }
    if fetch_plan.get("payload_mode") == "configuration_required":
        result["reason"] = fetch_plan.get("reason") or "source_configuration_required"
        return result
    if fetch_plan.get("requires_operator") and not fetch_plan.get("requests"):
        result["reason"] = "operator_payload_required"
        return result
    if fetch_plan.get("payload_mode") == "external_tool_command":
        command_plan = fetch_plan.get("command_plan") if isinstance(fetch_plan.get("command_plan"), dict) else {}
        if not tool_runner:
            result["reason"] = "tool_runner_required"
            return result
        response, error = _safe_tool_payload(tool_runner, command_plan)
        result["tool_command"] = command_plan
        if error:
            result["reason"] = "external_tool_failed"
            result["error"] = error
            return result
        result["payloads"].append(response["payload"])
        result["response_headers"]["0"] = response["headers"]
        result["ok"] = True
        return result
    if not http_get:
        result["reason"] = "http_client_required"
        return result
    if fetch_plan.get("payload_mode") in {"prowlarr_multi_search", "indexer_multi_search"}:
        combined_results = []
        seen_results = set()
        variant_counts = []
        partial_errors = []
        pack_detail_fetch_state = {"count": 0}
        adapter_family = fetch_plan.get("adapter_family")
        abort_after_request_error_count = providers.int_value(
            _policy_value(row, "indexer_abort_after_request_error_count", 0),
            0,
        ) or 0
        abort_after_request_error_count = max(0, min(abort_after_request_error_count, 10))
        consecutive_request_errors = 0
        requests = list(fetch_plan.get("requests") or [])
        for request_offset, request in enumerate(requests):
            request_index = len(variant_counts)
            response, error = _safe_response_payload(http_get, request)
            if (
                not error
                and adapter_family == "prowlarr_indexer"
                and not (
                    isinstance(response.get("payload"), list)
                    or (
                        isinstance(response.get("payload"), dict)
                        and isinstance(response.get("payload", {}).get("results"), list)
                    )
                )
            ):
                error = "malformed_provider_response"
            result["requests_made"].append(request)
            params = request.get("params") or {}
            query = _indexer_request_query(request)
            if error:
                consecutive_request_errors += 1
                partial_errors.append(
                    {
                        "stage": "indexer_multi_search",
                        "request_id": request.get("request_id"),
                        "purpose": request.get("purpose"),
                        "url_hash": providers.url_hash(str(request.get("url") or "")),
                        "query": query,
                        "error": error,
                    }
                )
                defer_abort_for_fallback = _indexer_error_should_try_ascii_fallback(
                    query,
                    requests,
                    request_offset,
                )
                if (
                    abort_after_request_error_count
                    and consecutive_request_errors >= abort_after_request_error_count
                    and not variant_counts
                    and not defer_abort_for_fallback
                ):
                    remaining = max(0, len(requests) - request_offset - 1)
                    partial_errors.append(
                        {
                            "stage": "indexer_multi_search_aborted",
                            "request_id": request.get("request_id"),
                            "purpose": request.get("purpose"),
                            "url_hash": providers.url_hash(str(request.get("url") or "")),
                            "query": query,
                            "error": f"request_error_threshold_after_{consecutive_request_errors}_error(s); skipped {remaining} remaining query variant(s)",
                        }
                    )
                    break
                if (
                    abort_after_request_error_count
                    and consecutive_request_errors >= abort_after_request_error_count
                    and not variant_counts
                    and defer_abort_for_fallback
                ):
                    partial_errors.append(
                        {
                            "stage": "indexer_multi_search_abort_deferred",
                            "request_id": request.get("request_id"),
                            "purpose": request.get("purpose"),
                            "url_hash": providers.url_hash(str(request.get("url") or "")),
                            "query": query,
                            "reason": "non_ascii_query_has_ascii_fallback",
                        }
                    )
                continue
            consecutive_request_errors = 0
            response_key = f"query_{len(variant_counts)}"
            result["response_headers"][response_key] = response["headers"]
            rows = _indexer_payload_results_for_adapter(adapter_family, response.get("payload"), row)
            variant_counts.append({"query": query, "results": len(rows)})
            for result_index, item in enumerate(rows):
                if not isinstance(item, dict):
                    continue
                key = _indexer_result_identity(item)
                if key in seen_results:
                    continue
                seen_results.add(key)
                item = dict(item)
                item["_inkdrop_query_variant"] = query
                item["_inkdrop_query_group"] = request.get("query_group") or ""
                item["_inkdrop_request_id"] = request.get("request_id") or ""
                item["_inkdrop_pack_query"] = bool(request.get("pack_query"))
                item["_inkdrop_query_index"] = request_index
                item["_inkdrop_query_result_index"] = result_index
                combined_results.append(item)
            if (
                request.get("pack_query")
                and fetch_plan.get("adapter_family") in INDEXER_RESULT_ADAPTER_FAMILIES
                and combined_results
            ):
                partial_payload = {
                    "results": combined_results,
                    "query_variants": list(fetch_plan.get("query_variants") or []),
                    "variant_result_counts": variant_counts,
                    "short_circuit_candidate_query": query,
                }
                if partial_errors:
                    partial_payload["partial_errors"] = partial_errors
                partial_payload = _indexer_payload_with_pack_details(
                    row,
                    plan,
                    wanted_item,
                    partial_payload,
                    http_get,
                    result,
                    limit=limit,
                    fetch_state=pack_detail_fetch_state,
                )
                combined_results = list(_indexer_payload_results(partial_payload))
                if _indexer_payload_has_pack_detail_match(row, partial_payload, wanted_item):
                    partial_payload["short_circuit_reason"] = "pack_detail_match"
                    result["payloads"].append(partial_payload)
                    result["ok"] = True
                    return result
        if (
            fetch_plan.get("adapter_family") == "prowlarr_indexer"
            and not combined_results
            and variant_counts
            and fetch_plan.get("categoryless_fallback_requests")
        ):
            for request in fetch_plan.get("categoryless_fallback_requests") or []:
                request_index = len(variant_counts)
                response, error = _safe_response_payload(http_get, request)
                result["requests_made"].append(request)
                params = request.get("params") or {}
                query = params.get("query") or params.get("q")
                if error:
                    partial_errors.append(
                        {
                            "stage": "prowlarr_categoryless_fallback",
                            "request_id": request.get("request_id"),
                            "purpose": request.get("purpose"),
                            "url_hash": providers.url_hash(str(request.get("url") or "")),
                            "query": query,
                            "error": error,
                        }
                    )
                    continue
                response_key = f"query_{len(variant_counts)}"
                result["response_headers"][response_key] = response["headers"]
                rows = _indexer_payload_results_for_adapter(adapter_family, response.get("payload"), row)
                variant_counts.append(
                    {
                        "query": query,
                        "results": len(rows),
                        "categoryless_fallback": True,
                        "indexerId": params.get("indexerId"),
                    }
                )
                for result_index, item in enumerate(rows):
                    if not isinstance(item, dict):
                        continue
                    key = _indexer_result_identity(item)
                    if key in seen_results:
                        continue
                    seen_results.add(key)
                    item = dict(item)
                    item["_inkdrop_query_variant"] = query
                    item["_inkdrop_query_group"] = request.get("query_group") or ""
                    item["_inkdrop_request_id"] = request.get("request_id") or ""
                    item["_inkdrop_pack_query"] = bool(request.get("pack_query"))
                    item["_inkdrop_query_index"] = request_index
                    item["_inkdrop_query_result_index"] = result_index
                    item["_inkdrop_categoryless_fallback"] = True
                    item["_inkdrop_categoryless_fallback_primary_request_id"] = request.get("primary_request_id") or ""
                    combined_results.append(item)
        query_expansion_count = 0
        if (
            not combined_results
            and variant_counts
            and fetch_plan.get("expansion_requests")
        ):
            # The planned batch (and, for Prowlarr, its categoryless fallback)
            # executed cleanly -- every request that ran returned a response,
            # combined across all of them is still zero -- so reach into the
            # untried tail of the same query pool before finalizing the zero.
            # Same clean-zero gate the categoryless-fallback step above
            # already uses in this function, so behavior stays consistent
            # within this file rather than introducing a stricter/different
            # bar for what counts as "genuinely nothing found."
            for request in fetch_plan.get("expansion_requests") or []:
                request_index = len(variant_counts)
                response, error = _safe_response_payload(http_get, request)
                result["requests_made"].append(request)
                query = _indexer_request_query(request)
                if error:
                    partial_errors.append(
                        {
                            "stage": "indexer_query_expansion",
                            "request_id": request.get("request_id"),
                            "purpose": request.get("purpose"),
                            "url_hash": providers.url_hash(str(request.get("url") or "")),
                            "query": query,
                            "error": error,
                        }
                    )
                    continue
                response_key = f"query_{len(variant_counts)}"
                result["response_headers"][response_key] = response["headers"]
                rows = _indexer_payload_results_for_adapter(adapter_family, response.get("payload"), row)
                variant_counts.append({"query": query, "results": len(rows), "query_expansion": True})
                query_expansion_count += 1
                for result_index, item in enumerate(rows):
                    if not isinstance(item, dict):
                        continue
                    key = _indexer_result_identity(item)
                    if key in seen_results:
                        continue
                    seen_results.add(key)
                    item = dict(item)
                    item["_inkdrop_query_variant"] = query
                    item["_inkdrop_query_group"] = request.get("query_group") or ""
                    item["_inkdrop_request_id"] = request.get("request_id") or ""
                    item["_inkdrop_pack_query"] = False
                    item["_inkdrop_query_index"] = request_index
                    item["_inkdrop_query_result_index"] = result_index
                    item["_inkdrop_query_expansion"] = True
                    combined_results.append(item)
        if not variant_counts and partial_errors:
            result["reason"] = "http_request_failed"
            result["error"] = partial_errors[0].get("error")
            result["partial_errors"] = partial_errors
            return result
        payload = {
            "results": combined_results,
            "query_variants": list(fetch_plan.get("query_variants") or []),
            "variant_result_counts": variant_counts,
        }
        if fetch_plan.get("categoryless_fallback_requests"):
            payload["categoryless_fallback_indexer_ids"] = list(fetch_plan.get("categoryless_fallback_indexer_ids") or [])
            payload["categoryless_fallback_request_count"] = len(fetch_plan.get("categoryless_fallback_requests") or [])
        if fetch_plan.get("expansion_requests"):
            payload["query_expansion_count"] = query_expansion_count
        if partial_errors:
            payload["partial_errors"] = partial_errors
        if fetch_plan.get("adapter_family") in INDEXER_RESULT_ADAPTER_FAMILIES:
            payload = _indexer_payload_with_pack_details(
                row,
                plan,
                wanted_item,
                payload,
                http_get,
                result,
                limit=limit,
                fetch_state=pack_detail_fetch_state,
            )
        result["payloads"].append(payload)
        result["ok"] = True
        return result
    if fetch_plan.get("payload_mode") == "archive_search_then_metadata":
        first_request = fetch_plan["requests"][0] if fetch_plan.get("requests") else None
        if not first_request:
            result["reason"] = "missing_archive_request"
            return result
        first_response, error = _safe_response_payload(http_get, first_request)
        if error:
            result["reason"] = "http_request_failed"
            result["error"] = error
            result["requests_made"].append(first_request)
            return result
        result["requests_made"].append(first_request)
        if first_request["request_id"] == "internet_archive_metadata":
            result["payloads"].append(first_response["payload"])
            result["response_headers"]["0"] = first_response["headers"]
            result["ok"] = True
            return result
        identifiers = _archive_identifiers(first_response["payload"], limit=limit)
        if not identifiers:
            result["ok"] = True
            return result
        for index, identifier in enumerate(identifiers):
            request = internet_archive_metadata_request(identifier)
            response, error = _safe_response_payload(http_get, request)
            if error:
                result["reason"] = "http_request_failed"
                result["error"] = error
                result["requests_made"].append(request)
                return result
            result["requests_made"].append(request)
            payload = response["payload"]
            if request.get("purpose") == "search_html_source":
                payload = {"text": payload if isinstance(payload, str) else "", "source_url": request.get("url"), "request_url": request.get("url")}
            result["payloads"].append(payload)
            result["response_headers"][str(index)] = response["headers"]
        result["ok"] = True
        return result
    if fetch_plan.get("payload_mode") == "suwayomi_search_then_chapters":
        return _fetch_suwayomi_source_payloads(row, plan, wanted_item, http_get, result, limit=limit)
    if fetch_plan.get("payload_mode") == "mangadex_search_then_feed":
        first_request = fetch_plan["requests"][0] if fetch_plan.get("requests") else None
        if not first_request:
            result["reason"] = "missing_mangadex_request"
            return result
        first_response, error = _safe_response_payload(http_get, first_request)
        result["requests_made"].append(first_request)
        if error:
            result["reason"] = "http_request_failed"
            result["error"] = error
            return result
        if first_request["request_id"] == "mangadex_manga_feed":
            manga_id = _known_mangadex_manga_id(wanted_item)
            feed_payload, feed_headers, feed_error = _fetch_mangadex_feed_pages(
                manga_id,
                {},
                row,
                plan,
                wanted_item,
                http_get,
                result,
                limit=limit,
                deadline=deadline,
            )
            if feed_error:
                return result
            payload, at_home_error = _mangadex_payload_with_at_home(
                row,
                plan,
                wanted_item,
                feed_payload or {"manga": {}, "feed": first_response["payload"]},
                http_get,
                result,
                limit=limit,
                deadline=deadline,
            )
            if at_home_error:
                result["reason"] = "http_request_failed"
                result["error"] = at_home_error
                return result
            result["payloads"].append(payload)
            result["response_headers"]["0"] = feed_headers or first_response["headers"]
            result["ok"] = True
            return result
        variant_counts = []
        seen_manga_ids = set()
        search_requests = [
            request
            for request in fetch_plan.get("requests") or []
            if (request or {}).get("request_id") != "mangadex_manga_feed"
        ]
        for search_index, search_request in enumerate(search_requests):
            if search_index == 0:
                search_response = first_response
            else:
                if _fetch_deadline_reached(deadline):
                    _record_partial_fetch_error(
                        result,
                        None,
                        search_request,
                        "fetch_deadline_reached",
                        stage="mangadex_search",
                    )
                    break
                search_response, error = _safe_response_payload(http_get, search_request)
                result["requests_made"].append(search_request)
                if error:
                    result["reason"] = "http_request_failed"
                    result["error"] = error
                    return result
            search_payload = search_response["payload"]
            rows = (
                search_payload.get("data")
                if isinstance(search_payload, dict) and isinstance(search_payload.get("data"), list)
                else []
            )
            query = (search_request.get("params") or {}).get("title")
            match_wanted_item = dict(wanted_item or {})
            # Stash the creator name a broad alias search strips (e.g. "Naoki
            # Urasawa" from "Naoki Urasawa's Monster") before series_title/
            # query below get overwritten with the query variant itself --
            # mangadex_manga_matches_wanted uses this to reject a same-title
            # work whose real MangaDex author/artist disagrees, even though
            # the alias-driven title check alone would accept it.
            expected_creator = ""
            for identity_key in ("series_title", "series", "manga_title", "title"):
                expected_creator = _leading_creator_possessive_name((wanted_item or {}).get(identity_key))
                if expected_creator:
                    break
            if expected_creator:
                match_wanted_item["_mangadex_expected_creator"] = expected_creator
            if query:
                match_wanted_item["series_title"] = query
                match_wanted_item["query"] = query
            manga_rows = _mangadex_manga_rows(
                search_payload,
                wanted_item=match_wanted_item,
                limit=limit,
                policy=_source_query_alias_policy(row),
            )
            variant_counts.append({"query": query, "results": len(rows), "matching_manga": len(manga_rows)})
            result["response_headers"][f"search_{search_index}"] = search_response["headers"]
            for manga_row in manga_rows:
                manga_id = str(manga_row.get("id") or "").strip()
                if not manga_id or manga_id in seen_manga_ids:
                    continue
                seen_manga_ids.add(manga_id)
                feed_payload, feed_headers, feed_error = _fetch_mangadex_feed_pages(
                    manga_id,
                    manga_row,
                    row,
                    plan,
                    wanted_item,
                    http_get,
                    result,
                    limit=limit,
                    deadline=deadline,
                )
                if feed_error:
                    return result
                payload, at_home_error = _mangadex_payload_with_at_home(
                    row,
                    plan,
                    wanted_item,
                    {
                        "manga": manga_row,
                        "feed": (feed_payload or {}).get("feed") if isinstance(feed_payload, dict) else {},
                        "search": search_payload,
                        "query_variants": list(fetch_plan.get("query_variants") or []),
                        "variant_result_counts": variant_counts,
                    },
                    http_get,
                    result,
                    limit=limit,
                    deadline=deadline,
                )
                if at_home_error:
                    result["reason"] = "http_request_failed"
                    result["error"] = at_home_error
                    return result
                result["payloads"].append(payload)
                result["response_headers"][str(len(result["payloads"]) - 1)] = feed_headers
                if _mangadex_feed_payload_has_match(payload, row, wanted_item, limit=limit):
                    result["query_variants"] = list(fetch_plan.get("query_variants") or [])
                    result["variant_result_counts"] = variant_counts
                    result["ok"] = True
                    return result
        result["query_variants"] = list(fetch_plan.get("query_variants") or [])
        result["variant_result_counts"] = variant_counts
        result["ok"] = True
        return result
    if fetch_plan.get("payload_mode") == "rss_feed_then_direct_file_pages":
        first_request = fetch_plan["requests"][0] if fetch_plan.get("requests") else None
        if not first_request:
            result["reason"] = "missing_rss_detail_direct_feed_request"
            return result
        response, error = _safe_response_payload(http_get, first_request)
        result["requests_made"].append(first_request)
        if error:
            result["reason"] = "http_request_failed"
            result["error"] = error
            return result
        policy = (row or {}).get("policy") if isinstance((row or {}).get("policy"), dict) else {}
        max_detail_pages = providers.int_value(policy.get("max_detail_pages"), 5)
        max_detail_pages = max(0, min(max_detail_pages, int(limit or 20)))
        source_site = providers.first_text(
            policy.get("source_site_label"),
            (row or {}).get("display_name"),
            (row or {}).get("provider_id"),
            "RSS detail direct feed",
        )
        feed_payload = _html_payload_for_request(response["payload"], first_request)
        combined = {"source_url": first_request.get("url"), "search_pages": [feed_payload], "detail_pages": []}
        _attach_rss_feed_evidence(result, combined, feed_payload, wanted_item, source_site=source_site)
        result["response_headers"]["feed"] = response["headers"]
        seen_detail_urls = set()
        discovery_hosts = _rss_discovery_allowed_hosts(row)
        links = providers.rss_item_links_from_payload(feed_payload, wanted_item, source_site=source_site, limit=limit)
        for link in links:
            if len(combined["detail_pages"]) >= max_detail_pages:
                break
            detail_url = str(link.get("url") or "").strip()
            if not detail_url or detail_url in seen_detail_urls:
                continue
            parsed = urlparse(detail_url)
            if str(parsed.scheme or "").lower() not in {"http", "https"}:
                continue
            if not _url_host_allowed(detail_url, discovery_hosts):
                _record_partial_fetch_error(
                    result,
                    combined,
                    {"request_id": "rss_detail_direct_page", "url": detail_url},
                    "disallowed_detail_host",
                    stage="rss_detail_direct_page",
                )
                continue
            if providers.normalize_extension(detail_url):
                continue
            seen_detail_urls.add(detail_url)
            detail_request = direct_file_detail_page_request(
                detail_url,
                len(combined["detail_pages"]),
                allowed_hosts=discovery_hosts,
            )
            detail_response, detail_error = _safe_response_payload(http_get, detail_request)
            result["requests_made"].append(detail_request)
            if detail_error:
                _record_partial_fetch_error(
                    result,
                    combined,
                    detail_request,
                    detail_error,
                    stage="rss_detail_direct_page",
                )
                continue
            detail_payload = _html_payload_for_request(detail_response["payload"], detail_request)
            detail_payload.setdefault("title", link.get("title"))
            combined["detail_pages"].append(detail_payload)
            result["response_headers"][f"detail_{len(combined['detail_pages']) - 1}"] = detail_response["headers"]
            if len(combined["detail_pages"]) >= max_detail_pages:
                break
        result["payloads"].append(combined)
        result["ok"] = True
        return result
    if fetch_plan.get("payload_mode") == "rss_feed_then_direct_file_probes":
        first_request = fetch_plan["requests"][0] if fetch_plan.get("requests") else None
        if not first_request:
            result["reason"] = "missing_rss_detail_probe_feed_request"
            return result
        response, error = _safe_response_payload(http_get, first_request)
        result["requests_made"].append(first_request)
        if error:
            result["reason"] = "http_request_failed"
            result["error"] = error
            return result
        policy = (row or {}).get("policy") if isinstance((row or {}).get("policy"), dict) else {}
        max_detail_pages = providers.int_value(policy.get("max_detail_pages"), 5)
        max_detail_pages = max(0, min(max_detail_pages, int(limit or 20)))
        max_probe_links = providers.int_value(policy.get("max_probe_links"), 5)
        max_probe_links = max(0, min(max_probe_links, int(limit or 20)))
        probe_method = providers.first_text(policy.get("probe_method"), "HEAD").upper()
        if probe_method not in {"GET", "HEAD"}:
            probe_method = "HEAD"
        source_site = providers.first_text(
            policy.get("source_site_label"),
            (row or {}).get("display_name"),
            (row or {}).get("provider_id"),
            "RSS detail probe feed",
        )
        feed_payload = _html_payload_for_request(response["payload"], first_request)
        combined = {
            "source_url": first_request.get("url"),
            "search_pages": [feed_payload],
            "detail_pages": [],
            "probe_headers": {},
            "probe_status": {},
        }
        _attach_rss_feed_evidence(result, combined, feed_payload, wanted_item, source_site=source_site)
        result["response_headers"]["feed"] = response["headers"]
        seen_detail_urls = set()
        discovery_hosts = _rss_discovery_allowed_hosts(row)
        transport_hosts = _direct_transport_allowed_hosts(row)
        links = providers.rss_item_links_from_payload(feed_payload, wanted_item, source_site=source_site, limit=limit)
        for link in links:
            if len(combined["detail_pages"]) >= max_detail_pages:
                break
            detail_url = str(link.get("url") or "").strip()
            if not detail_url or detail_url in seen_detail_urls:
                continue
            parsed = urlparse(detail_url)
            if str(parsed.scheme or "").lower() not in {"http", "https"}:
                continue
            if not _url_host_allowed(detail_url, discovery_hosts):
                _record_partial_fetch_error(
                    result,
                    combined,
                    {"request_id": "rss_detail_probe_page", "url": detail_url},
                    "disallowed_detail_host",
                    stage="rss_detail_probe_page",
                )
                continue
            if providers.normalize_extension(detail_url):
                continue
            seen_detail_urls.add(detail_url)
            detail_request = direct_file_detail_page_request(
                detail_url,
                len(combined["detail_pages"]),
                allowed_hosts=discovery_hosts,
            )
            detail_response, detail_error = _safe_response_payload(http_get, detail_request)
            result["requests_made"].append(detail_request)
            if detail_error:
                _record_partial_fetch_error(
                    result,
                    combined,
                    detail_request,
                    detail_error,
                    stage="rss_detail_probe_page",
                )
                continue
            detail_payload = _html_payload_for_request(detail_response["payload"], detail_request)
            detail_payload.setdefault("title", link.get("title"))
            combined["detail_pages"].append(detail_payload)
            result["response_headers"][f"detail_{len(combined['detail_pages']) - 1}"] = detail_response["headers"]
        probe_candidates = providers.direct_file_probe_candidates_from_payload(
            combined,
            row,
            wanted_item,
            limit=max_probe_links,
        )
        seen_probe_urls = set()
        for candidate in probe_candidates:
            probe_url = str(candidate.get("download_url") or "").strip()
            probe_hash = providers.url_hash(probe_url)
            if not probe_url or probe_hash in seen_probe_urls:
                continue
            if not _url_host_allowed(probe_url, transport_hosts):
                _record_partial_fetch_error(
                    result,
                    combined,
                    {"request_id": "rss_detail_probe_head", "url": probe_url},
                    "disallowed_transport_host",
                    stage="rss_detail_probe_head",
                )
                continue
            seen_probe_urls.add(probe_hash)
            probe_request = direct_file_probe_request(
                probe_url,
                len(seen_probe_urls) - 1,
                method=probe_method,
                allowed_hosts=transport_hosts,
            )
            probe_response, probe_error = _safe_response_payload(http_get, probe_request)
            result["requests_made"].append(probe_request)
            if probe_error:
                _record_partial_fetch_error(
                    result,
                    combined,
                    probe_request,
                    probe_error,
                    stage="rss_detail_probe_head",
                )
                continue
            combined["probe_headers"][probe_hash] = probe_response["headers"]
            combined["probe_status"][probe_hash] = probe_response.get("status")
            result["response_headers"][f"probe_{len(seen_probe_urls) - 1}"] = probe_response["headers"]
        result["payloads"].append(combined)
        result["ok"] = True
        return result
    if fetch_plan.get("payload_mode") == "rss_feed_then_reader_pages":
        first_request = fetch_plan["requests"][0] if fetch_plan.get("requests") else None
        if not first_request:
            result["reason"] = "missing_rss_reader_page_pack_feed_request"
            return result
        response, error = _safe_response_payload(http_get, first_request)
        result["requests_made"].append(first_request)
        if error:
            result["reason"] = "http_request_failed"
            result["error"] = error
            return result
        policy = (row or {}).get("policy") if isinstance((row or {}).get("policy"), dict) else {}
        max_reader_pages = providers.int_value(policy.get("max_reader_pages"), 5)
        max_reader_pages = max(0, min(max_reader_pages, int(limit or 20)))
        max_series_pages = providers.int_value(policy.get("max_series_pages"), 3)
        max_series_pages = max(0, min(max_series_pages, int(limit or 20)))
        source_site = providers.first_text(
            policy.get("source_site_label"),
            (row or {}).get("display_name"),
            (row or {}).get("provider_id"),
            "RSS reader page-pack feed",
        )
        feed_payload = _html_payload_for_request(response["payload"], first_request)
        combined = {"source_url": first_request.get("url"), "search_pages": [feed_payload], "series_pages": [], "reader_pages": []}
        _attach_rss_feed_evidence(result, combined, feed_payload, wanted_item, source_site=source_site)
        result["response_headers"]["feed"] = response["headers"]
        seen_reader_urls = set()
        seen_series_urls = set()
        links = providers.rss_item_links_from_payload(feed_payload, wanted_item, source_site=source_site, limit=limit)
        for link in links:
            if len(combined["reader_pages"]) >= max_reader_pages:
                break
            reader_url = str(link.get("url") or "").strip()
            if not reader_url or reader_url in seen_reader_urls:
                continue
            parsed = urlparse(reader_url)
            if str(parsed.scheme or "").lower() not in {"http", "https"}:
                continue
            if providers.normalize_extension(reader_url):
                continue
            seen_reader_urls.add(reader_url)
            reader_request = reader_page_pack_page_request(reader_url, len(combined["reader_pages"]))
            reader_response, reader_error = _safe_response_payload(http_get, reader_request)
            result["requests_made"].append(reader_request)
            if reader_error:
                _record_partial_fetch_error(
                    result,
                    combined,
                    reader_request,
                    reader_error,
                    stage="rss_reader_page",
                )
                continue
            reader_payload = _html_payload_for_request(reader_response["payload"], reader_request)
            reader_payload.setdefault("title", link.get("title"))
            if providers.reader_page_pack_candidates_from_payload({"reader_pages": [reader_payload]}, row, wanted_item, limit=1):
                combined["reader_pages"].append(reader_payload)
                result["response_headers"][f"reader_{len(combined['reader_pages']) - 1}"] = reader_response["headers"]
                continue
            if max_series_pages and len(combined["series_pages"]) < max_series_pages and reader_url not in seen_series_urls:
                seen_series_urls.add(reader_url)
                combined["series_pages"].append(reader_payload)
                result["response_headers"][f"series_{len(combined['series_pages']) - 1}"] = reader_response["headers"]
                chapter_links = providers.reader_chapter_links_from_payload(
                    reader_payload,
                    wanted_item,
                    source_site=source_site,
                    limit=limit,
                    policy=policy,
                )
                for chapter_link in chapter_links:
                    chapter_url = str(chapter_link.get("url") or "").strip()
                    if not chapter_url or chapter_url in seen_reader_urls:
                        continue
                    chapter_parsed = urlparse(chapter_url)
                    if str(chapter_parsed.scheme or "").lower() not in {"http", "https"}:
                        continue
                    if providers.normalize_extension(chapter_url):
                        continue
                    seen_reader_urls.add(chapter_url)
                    chapter_request = reader_page_pack_chapter_page_request(chapter_url, len(combined["reader_pages"]))
                    chapter_response, chapter_error = _safe_response_payload(http_get, chapter_request)
                    result["requests_made"].append(chapter_request)
                    if chapter_error:
                        _record_partial_fetch_error(
                            result,
                            combined,
                            chapter_request,
                            chapter_error,
                            stage="rss_reader_chapter_page",
                        )
                        continue
                    chapter_payload = _html_payload_for_request(chapter_response["payload"], chapter_request)
                    chapter_title = providers.normalized_query(
                        " ".join(
                            part
                            for part in (reader_payload.get("title"), chapter_link.get("title"))
                            if part
                        )
                    )
                    chapter_payload.setdefault("title", providers.first_text(chapter_title, chapter_link.get("title")))
                    if providers.reader_page_pack_candidates_from_payload({"reader_pages": [chapter_payload]}, row, wanted_item, limit=1):
                        combined["reader_pages"].append(chapter_payload)
                        result["response_headers"][f"reader_{len(combined['reader_pages']) - 1}"] = chapter_response["headers"]
                    if len(combined["reader_pages"]) >= max_reader_pages:
                        break
            if len(combined["reader_pages"]) >= max_reader_pages:
                break
        result["payloads"].append(combined)
        result["ok"] = True
        return result
    if fetch_plan.get("payload_mode") == "rss_feed_then_torrent_detail_pages":
        first_request = fetch_plan["requests"][0] if fetch_plan.get("requests") else None
        if not first_request:
            result["reason"] = "missing_torrent_detail_rss_feed_request"
            return result
        response, error = _safe_response_payload(http_get, first_request)
        result["requests_made"].append(first_request)
        if error:
            result["reason"] = "http_request_failed"
            result["error"] = error
            return result
        policy = (row or {}).get("policy") if isinstance((row or {}).get("policy"), dict) else {}
        max_detail_pages = providers.int_value(policy.get("max_detail_pages"), 5)
        max_detail_pages = max(0, min(max_detail_pages, int(limit or 20)))
        source_site = providers.first_text(
            policy.get("source_site_label"),
            (row or {}).get("display_name"),
            (row or {}).get("provider_id"),
            "Torrent detail RSS feed",
        )
        feed_payload = _html_payload_for_request(response["payload"], first_request)
        combined = {"source_url": first_request.get("url"), "search_pages": [feed_payload], "detail_pages": []}
        _attach_rss_feed_evidence(result, combined, feed_payload, wanted_item, source_site=source_site)
        result["response_headers"]["feed"] = response["headers"]
        seen_detail_urls = set()
        links = providers.rss_item_links_from_payload(feed_payload, wanted_item, source_site=source_site, limit=limit)
        for link in links:
            if len(combined["detail_pages"]) >= max_detail_pages:
                break
            detail_url = str(link.get("url") or "").strip()
            if not detail_url or detail_url in seen_detail_urls:
                continue
            parsed = urlparse(detail_url)
            if str(parsed.scheme or "").lower() not in {"http", "https"}:
                continue
            if providers.normalize_extension(detail_url) == ".torrent":
                continue
            seen_detail_urls.add(detail_url)
            detail_request = torrent_detail_page_request(detail_url, len(combined["detail_pages"]))
            detail_response, detail_error = _safe_response_payload(http_get, detail_request)
            result["requests_made"].append(detail_request)
            if detail_error:
                _record_partial_fetch_error(
                    result,
                    combined,
                    detail_request,
                    detail_error,
                    stage="rss_torrent_detail_page",
                )
                continue
            detail_payload = _html_payload_for_request(detail_response["payload"], detail_request)
            detail_payload.setdefault("title", link.get("title"))
            combined["detail_pages"].append(detail_payload)
            result["response_headers"][f"detail_{len(combined['detail_pages']) - 1}"] = detail_response["headers"]
        result["payloads"].append(combined)
        result["ok"] = True
        return result
    if fetch_plan.get("payload_mode") == "html_search_then_direct_file_pages":
        policy = (row or {}).get("policy") if isinstance((row or {}).get("policy"), dict) else {}
        max_detail_pages = providers.int_value(policy.get("max_detail_pages"), 5)
        max_detail_pages = max(0, min(max_detail_pages, int(limit or 20)))
        source_site = providers.first_text(
            policy.get("source_site_label"),
            (row or {}).get("display_name"),
            (row or {}).get("provider_id"),
            "Direct file detail source",
        )
        combined = {"source_url": "", "search_pages": [], "detail_pages": []}
        seen_detail_urls = set()
        for search_index, request in enumerate(fetch_plan.get("requests") or []):
            response, error = _safe_response_payload(http_get, request)
            result["requests_made"].append(request)
            if error:
                result["reason"] = "http_request_failed"
                result["error"] = error
                return result
            search_payload = _html_payload_for_request(response["payload"], request)
            if not combined["source_url"]:
                combined["source_url"] = request.get("url")
            combined["search_pages"].append(search_payload)
            result["response_headers"][f"search_{search_index}"] = response["headers"]
            if len(combined["detail_pages"]) >= max_detail_pages:
                continue
            links = providers.html_search_result_links_from_payload(search_payload, source_site=source_site, limit=limit, policy=policy)
            links.extend(providers.json_search_result_links_from_payload(search_payload, source_site=source_site, limit=limit, policy=policy))
            for link in links:
                detail_url = str(link.get("url") or "").strip()
                if not detail_url or detail_url in seen_detail_urls:
                    continue
                parsed = urlparse(detail_url)
                if str(parsed.scheme or "").lower() not in {"http", "https"}:
                    continue
                if providers.normalize_extension(detail_url):
                    continue
                seen_detail_urls.add(detail_url)
                detail_request = direct_file_detail_page_request(detail_url, len(combined["detail_pages"]))
                detail_response, detail_error = _safe_response_payload(http_get, detail_request)
                result["requests_made"].append(detail_request)
                if detail_error:
                    result["reason"] = "http_request_failed"
                    result["error"] = detail_error
                    return result
                combined["detail_pages"].append(_html_payload_for_request(detail_response["payload"], detail_request))
                result["response_headers"][f"detail_{len(combined['detail_pages']) - 1}"] = detail_response["headers"]
                if len(combined["detail_pages"]) >= max_detail_pages:
                    break
        result["payloads"].append(combined)
        result["ok"] = True
        return result
    if fetch_plan.get("payload_mode") == "html_search_then_direct_file_probes":
        policy = (row or {}).get("policy") if isinstance((row or {}).get("policy"), dict) else {}
        max_detail_pages = providers.int_value(policy.get("max_detail_pages"), 5)
        max_detail_pages = max(0, min(max_detail_pages, int(limit or 20)))
        max_probe_links = providers.int_value(policy.get("max_probe_links"), 5)
        max_probe_links = max(0, min(max_probe_links, int(limit or 20)))
        probe_method = providers.first_text(policy.get("probe_method"), "HEAD").upper()
        if probe_method not in {"GET", "HEAD"}:
            probe_method = "HEAD"
        source_site = providers.first_text(
            policy.get("source_site_label"),
            (row or {}).get("display_name"),
            (row or {}).get("provider_id"),
            "Direct file probe source",
        )
        combined = {
            "source_url": "",
            "search_pages": [],
            "detail_pages": [],
            "probe_headers": {},
            "probe_status": {},
        }
        seen_detail_urls = set()
        for search_index, request in enumerate(fetch_plan.get("requests") or []):
            response, error = _safe_response_payload(http_get, request)
            result["requests_made"].append(request)
            if error:
                result["reason"] = "http_request_failed"
                result["error"] = error
                return result
            search_payload = _html_payload_for_request(response["payload"], request)
            if not combined["source_url"]:
                combined["source_url"] = request.get("url")
            combined["search_pages"].append(search_payload)
            result["response_headers"][f"search_{search_index}"] = response["headers"]
            if len(combined["detail_pages"]) >= max_detail_pages:
                continue
            links = providers.html_search_result_links_from_payload(search_payload, source_site=source_site, limit=limit, policy=policy)
            links.extend(providers.json_search_result_links_from_payload(search_payload, source_site=source_site, limit=limit, policy=policy))
            for link in links:
                detail_url = str(link.get("url") or "").strip()
                if not detail_url or detail_url in seen_detail_urls:
                    continue
                parsed = urlparse(detail_url)
                if str(parsed.scheme or "").lower() not in {"http", "https"}:
                    continue
                if providers.normalize_extension(detail_url):
                    continue
                seen_detail_urls.add(detail_url)
                detail_request = direct_file_detail_page_request(detail_url, len(combined["detail_pages"]))
                detail_response, detail_error = _safe_response_payload(http_get, detail_request)
                result["requests_made"].append(detail_request)
                if detail_error:
                    result["reason"] = "http_request_failed"
                    result["error"] = detail_error
                    return result
                detail_payload = _html_payload_for_request(detail_response["payload"], detail_request)
                detail_payload.setdefault("title", link.get("title"))
                combined["detail_pages"].append(detail_payload)
                result["response_headers"][f"detail_{len(combined['detail_pages']) - 1}"] = detail_response["headers"]
                if len(combined["detail_pages"]) >= max_detail_pages:
                    break
        probe_candidates = providers.direct_file_probe_candidates_from_payload(
            combined,
            row,
            wanted_item,
            limit=max_probe_links,
        )
        seen_probe_urls = set()
        for candidate in probe_candidates:
            probe_url = str(candidate.get("download_url") or "").strip()
            probe_hash = providers.url_hash(probe_url)
            if not probe_url or probe_hash in seen_probe_urls:
                continue
            seen_probe_urls.add(probe_hash)
            probe_request = direct_file_probe_request(probe_url, len(seen_probe_urls) - 1, method=probe_method)
            probe_response, probe_error = _safe_response_payload(http_get, probe_request)
            result["requests_made"].append(probe_request)
            if probe_error:
                result["reason"] = "http_request_failed"
                result["error"] = probe_error
                return result
            combined["probe_headers"][probe_hash] = probe_response["headers"]
            combined["probe_status"][probe_hash] = probe_response.get("status")
            result["response_headers"][f"probe_{len(seen_probe_urls) - 1}"] = probe_response["headers"]
        result["payloads"].append(combined)
        result["ok"] = True
        return result
    if fetch_plan.get("payload_mode") == "html_search_then_torrent_detail_pages":
        policy = (row or {}).get("policy") if isinstance((row or {}).get("policy"), dict) else {}
        max_detail_pages = providers.int_value(policy.get("max_detail_pages"), 5)
        max_detail_pages = max(0, min(max_detail_pages, int(limit or 20)))
        source_site = providers.first_text(
            policy.get("source_site_label"),
            (row or {}).get("display_name"),
            (row or {}).get("provider_id"),
            "Torrent detail source",
        )
        combined = {"source_url": "", "search_pages": [], "detail_pages": []}
        seen_detail_urls = set()
        for search_index, request in enumerate(fetch_plan.get("requests") or []):
            response, error = _safe_response_payload(http_get, request)
            result["requests_made"].append(request)
            if error:
                result["reason"] = "http_request_failed"
                result["error"] = error
                return result
            search_payload = _html_payload_for_request(response["payload"], request)
            if not combined["source_url"]:
                combined["source_url"] = request.get("url")
            combined["search_pages"].append(search_payload)
            result["response_headers"][f"search_{search_index}"] = response["headers"]
            if len(combined["detail_pages"]) >= max_detail_pages:
                continue
            links = providers.html_search_result_links_from_payload(search_payload, source_site=source_site, limit=limit, policy=policy)
            links.extend(providers.json_search_result_links_from_payload(search_payload, source_site=source_site, limit=limit, policy=policy))
            for link in links:
                detail_url = str(link.get("url") or "").strip()
                if not detail_url or detail_url in seen_detail_urls:
                    continue
                parsed = urlparse(detail_url)
                if str(parsed.scheme or "").lower() not in {"http", "https"}:
                    continue
                if providers.normalize_extension(detail_url) == ".torrent":
                    continue
                seen_detail_urls.add(detail_url)
                detail_request = torrent_detail_page_request(detail_url, len(combined["detail_pages"]))
                detail_response, detail_error = _safe_response_payload(http_get, detail_request)
                result["requests_made"].append(detail_request)
                if detail_error:
                    result["reason"] = "http_request_failed"
                    result["error"] = detail_error
                    return result
                detail_payload = _html_payload_for_request(detail_response["payload"], detail_request)
                detail_payload.setdefault("title", link.get("title"))
                combined["detail_pages"].append(detail_payload)
                result["response_headers"][f"detail_{len(combined['detail_pages']) - 1}"] = detail_response["headers"]
                if len(combined["detail_pages"]) >= max_detail_pages:
                    break
        result["payloads"].append(combined)
        result["ok"] = True
        return result
    if fetch_plan.get("payload_mode") == "html_search_then_reader_pages":
        policy = (row or {}).get("policy") if isinstance((row or {}).get("policy"), dict) else {}
        max_reader_pages = providers.int_value(policy.get("max_reader_pages"), 5)
        max_reader_pages = max(0, min(max_reader_pages, int(limit or 20)))
        max_series_pages = providers.int_value(policy.get("max_series_pages"), 3)
        max_series_pages = max(0, min(max_series_pages, int(limit or 20)))
        source_site = providers.first_text(
            policy.get("source_site_label"),
            (row or {}).get("display_name"),
            (row or {}).get("provider_id"),
            "Reader page pack source",
        )
        combined = {"source_url": "", "search_pages": [], "series_pages": [], "reader_pages": []}
        seen_reader_urls = set()
        seen_series_urls = set()
        for search_index, request in enumerate(fetch_plan.get("requests") or []):
            response, error = _safe_response_payload(http_get, request)
            result["requests_made"].append(request)
            if error:
                result["reason"] = "http_request_failed"
                result["error"] = error
                return result
            search_payload = _html_payload_for_request(response["payload"], request)
            if not combined["source_url"]:
                combined["source_url"] = request.get("url")
            combined["search_pages"].append(search_payload)
            result["response_headers"][f"search_{search_index}"] = response["headers"]
            if len(combined["reader_pages"]) >= max_reader_pages:
                continue
            links = providers.html_search_result_links_from_payload(search_payload, source_site=source_site, limit=limit, policy=policy)
            for link in links:
                reader_url = str(link.get("url") or "").strip()
                if not reader_url or reader_url in seen_reader_urls:
                    continue
                parsed = urlparse(reader_url)
                if str(parsed.scheme or "").lower() not in {"http", "https"}:
                    continue
                if providers.normalize_extension(reader_url):
                    continue
                seen_reader_urls.add(reader_url)
                reader_request = reader_page_pack_page_request(reader_url, len(combined["reader_pages"]))
                reader_response, reader_error = _safe_response_payload(http_get, reader_request)
                result["requests_made"].append(reader_request)
                if reader_error:
                    result["reason"] = "http_request_failed"
                    result["error"] = reader_error
                    return result
                reader_payload = _html_payload_for_request(reader_response["payload"], reader_request)
                reader_payload.setdefault("title", link.get("title"))
                if providers.reader_page_pack_candidates_from_payload({"reader_pages": [reader_payload]}, row, wanted_item, limit=1):
                    combined["reader_pages"].append(reader_payload)
                    result["response_headers"][f"reader_{len(combined['reader_pages']) - 1}"] = reader_response["headers"]
                    if len(combined["reader_pages"]) >= max_reader_pages:
                        break
                    continue
                if max_series_pages and len(combined["series_pages"]) < max_series_pages and reader_url not in seen_series_urls:
                    seen_series_urls.add(reader_url)
                    combined["series_pages"].append(reader_payload)
                    result["response_headers"][f"series_{len(combined['series_pages']) - 1}"] = reader_response["headers"]
                    chapter_links = providers.reader_chapter_links_from_payload(
                        reader_payload,
                        wanted_item,
                        source_site=source_site,
                        limit=limit,
                        policy=policy,
                    )
                    for chapter_link in chapter_links:
                        chapter_url = str(chapter_link.get("url") or "").strip()
                        if not chapter_url or chapter_url in seen_reader_urls:
                            continue
                        chapter_parsed = urlparse(chapter_url)
                        if str(chapter_parsed.scheme or "").lower() not in {"http", "https"}:
                            continue
                        if providers.normalize_extension(chapter_url):
                            continue
                        seen_reader_urls.add(chapter_url)
                        chapter_request = reader_page_pack_chapter_page_request(chapter_url, len(combined["reader_pages"]))
                        chapter_response, chapter_error = _safe_response_payload(http_get, chapter_request)
                        result["requests_made"].append(chapter_request)
                        if chapter_error:
                            result["reason"] = "http_request_failed"
                            result["error"] = chapter_error
                            return result
                        chapter_payload = _html_payload_for_request(chapter_response["payload"], chapter_request)
                        chapter_title = providers.normalized_query(
                            " ".join(
                                part
                                for part in (reader_payload.get("title"), chapter_link.get("title"))
                                if part
                            )
                        )
                        chapter_payload.setdefault("title", providers.first_text(chapter_title, chapter_link.get("title")))
                        if providers.reader_page_pack_candidates_from_payload({"reader_pages": [chapter_payload]}, row, wanted_item, limit=1):
                            combined["reader_pages"].append(chapter_payload)
                            result["response_headers"][f"reader_{len(combined['reader_pages']) - 1}"] = chapter_response["headers"]
                        if len(combined["reader_pages"]) >= max_reader_pages:
                            break
                if len(combined["reader_pages"]) >= max_reader_pages:
                    break
        result["payloads"].append(combined)
        result["ok"] = True
        return result
    if fetch_plan.get("payload_mode") == "multi_payload":
        for index, request in enumerate(fetch_plan.get("requests") or []):
            response, error = _safe_response_payload(http_get, request)
            result["requests_made"].append(request)
            if error:
                result["reason"] = "http_request_failed"
                result["error"] = error
                return result
            result["payloads"].append(response["payload"])
            result["response_headers"][str(index)] = response["headers"]
        result["ok"] = True
        return result
    if fetch_plan.get("requests"):
        request = fetch_plan["requests"][0]
        response, error = _safe_response_payload(http_get, request)
        if error:
            result["reason"] = "http_request_failed"
            result["error"] = error
            result["requests_made"].append(request)
            return result
        result["requests_made"].append(request)
        payload = response["payload"]
        if fetch_plan.get("adapter_family") in INDEXER_RESULT_ADAPTER_FAMILIES:
            rows = _indexer_payload_results_for_adapter(fetch_plan.get("adapter_family"), payload, row)
            if rows and not _indexer_payload_results(payload):
                payload = {"results": rows}
            payload = _indexer_payload_with_pack_details(
                row,
                plan,
                wanted_item,
                payload,
                http_get,
                result,
                limit=limit,
            )
        elif fetch_plan.get("adapter_family") in RSS_FEED_EVIDENCE_ADAPTER_FAMILIES:
            source_site = providers.first_text(
                ((row or {}).get("policy") if isinstance((row or {}).get("policy"), dict) else {}).get("source_site_label"),
                (row or {}).get("display_name"),
                (row or {}).get("provider_id"),
                "RSS feed",
            )
            _attach_rss_feed_evidence(result, payload if isinstance(payload, dict) else None, payload, wanted_item, source_site=source_site)
        result["payloads"].append(payload)
        result["response_headers"]["0"] = response["headers"]
        result["ok"] = True
        return result
    result["reason"] = fetch_plan.get("reason") or "no_request_available"
    return result


def safe_request_json(request):
    request = request if isinstance(request, dict) else {}
    safe = dict(request)
    if safe.get("secret_params"):
        safe["secret_params"] = {key: "<redacted>" for key in dict(safe.get("secret_params") or {})}
    return json.dumps(safe, sort_keys=True, ensure_ascii=True)
