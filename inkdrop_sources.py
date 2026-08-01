import hashlib
import re


EDITION_QUALIFIER_RE = re.compile(
    r"(?i)^(?:(?:absolute|deluxe|complete|collected|collector(?:'s)?|library|anniversary|ultimate)\s+)+"
)


def collected_title_aliases(value):
    """Derive narrow aliases for qualified collected-edition identities."""
    title = re.sub(r"\s+", " ", str(value or "")).strip()
    if ":" not in title:
        return []
    left, subtitle = (re.sub(r"\s+", " ", part).strip() for part in title.split(":", 1))
    identity = re.sub(r"\s+", " ", EDITION_QUALIFIER_RE.sub("", left)).strip()
    if identity == left or not identity or len(re.findall(r"[a-z0-9]+", subtitle.casefold())) < 3:
        return []
    return [f"{identity} {subtitle}", subtitle]


def contributor_title_aliases(value):
    """Return a conservative work-title alias for a trailing creator byline.

    Comic metadata commonly publishes identities such as ``Swamp Thing by
    Alan Moore`` while provider releases use only ``Swamp Thing``.  A byline
    is removed only when the work has at least two words and the suffix looks
    like a 2-5 part proper name. This avoids treating ordinary ``... by ...``
    title prose as an alias.
    """
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    match = re.match(r"^(.+?)\s+by\s+(.+)$", text, flags=re.IGNORECASE)
    if not match:
        return []
    title = match.group(1).strip(" :-\u2013\u2014")
    contributor = match.group(2).strip()
    title_words = re.findall(r"[A-Za-z0-9]+", title)
    name_parts = contributor.split()
    if len(title_words) < 2 or not 2 <= len(name_parts) <= 5:
        return []
    proper_name = re.compile(r"^(?:[A-Z][A-Za-z.'\u2019-]*|[A-Z]\.)$")
    if not all(proper_name.fullmatch(part) for part in name_parts):
        return []
    return [title]


PROVIDER_ALIASES = {
    "soulseek": "slskd",
    "slskd_api": "slskd",
    "qb": "qbittorrent",
    "qbit": "qbittorrent",
    "sab": "sabnzbd",
    "sabnzbd": "sabnzbd",
    "get_comics": "rss",
    "getcomics": "rss",
    "comics_codes": "comicscodes",
    "comicscodes": "comicscodes",
    "standardebooks": "standard_ebooks",
    "standard_ebooks_org": "standard_ebooks",
    "project_gutenberg": "gutendex",
    "gutenberg": "gutendex",
    "internetarchive": "internet_archive",
    "internet_archive_org": "internet_archive",
    "openlibrary": "open_library",
    "open_library_org": "open_library",
    "comic_vine": "comic_vine",
    "anilist": "anilist",
    "nyaa": "prowlarr_nyaa",
    "nyaa_si": "prowlarr_nyaa",
    "tokyo_toshokan": "prowlarr_tokyo_toshokan_manga",
    "tokyo_toshokan_kavita_manga": "prowlarr_tokyo_toshokan_manga",
    "tokyo_toshokan_manga": "prowlarr_tokyo_toshokan_manga",
    "torrentleech_comics": "prowlarr_torrentleech_comics",
    "torrentleech_inkdrop_comics_all": "prowlarr_torrentleech_comics",
    "torrentleech_inkdrop_comics": "prowlarr_torrentleech_comics",
    "kat": "prowlarr_kat_comics",
    "kat_comics": "prowlarr_kat_comics",
    "kickass": "prowlarr_kat_comics",
    "kickasstorrents": "prowlarr_kat_comics",
    "kickasstorrents_to": "prowlarr_kat_comics",
    "pirate_bay": "prowlarr_pirate_bay_comics",
    "pirate_bay_comics": "prowlarr_pirate_bay_comics",
    "the_pirate_bay": "prowlarr_pirate_bay_comics",
    "tpb": "prowlarr_pirate_bay_comics",
    "tpb_comics": "prowlarr_pirate_bay_comics",
    "torrentdownload": "prowlarr_torrentdownload_comics",
    "torrentdownload_comics": "prowlarr_torrentdownload_comics",
    "torrent_download": "prowlarr_torrentdownload_comics",
    "torrent_download_comics": "prowlarr_torrentdownload_comics",
    "dognzb": "prowlarr_dognzb_comics",
    "dog_nzb": "prowlarr_dognzb_comics",
    "dognzb_comics": "prowlarr_dognzb_comics",
    "ebookbay": "prowlarr_ebookbay",
    "ebook_bay": "prowlarr_ebookbay",
    "academic_torrents": "prowlarr_academic_torrents",
    "bitmagnet": "prowlarr_bitmagnet",
    "torznab": "generic_torznab_indexer",
    "jackett": "generic_torznab_indexer",
    "generic_torznab": "generic_torznab_indexer",
    "generic_jackett": "generic_torznab_indexer",
    "newznab": "generic_newznab_indexer",
    "generic_newznab": "generic_newznab_indexer",
    "nzb_indexer": "generic_newznab_indexer",
    "newznab_indexer": "generic_newznab_indexer",
    "generic_nzb": "generic_newznab_indexer",
    "rss_direct_feed": "generic_rss_direct_feed",
    "direct_rss_feed": "generic_rss_direct_feed",
    "rss_enclosure_feed": "generic_rss_direct_feed",
    "rss_detail_direct_feed": "generic_rss_detail_direct_feed",
    "rss_detail_feed": "generic_rss_detail_direct_feed",
    "rss_landing_page_direct_feed": "generic_rss_detail_direct_feed",
    "feed_detail_direct": "generic_rss_detail_direct_feed",
    "rss_detail_probe_feed": "generic_rss_detail_probe_feed",
    "rss_probe_detail_feed": "generic_rss_detail_probe_feed",
    "feed_detail_probe": "generic_rss_detail_probe_feed",
    "rss_header_probe_feed": "generic_rss_detail_probe_feed",
    "rss_reader_page_pack": "generic_rss_reader_page_pack_feed",
    "rss_reader_page_pack_feed": "generic_rss_reader_page_pack_feed",
    "feed_reader_page_pack": "generic_rss_reader_page_pack_feed",
    "reader_page_pack_feed": "generic_rss_reader_page_pack_feed",
    "direct_file_search": "generic_direct_file_search",
    "direct_file_html_search": "generic_direct_file_search",
    "html_direct_file_search": "generic_direct_file_search",
    "direct_file_index": "generic_direct_file_search",
    "direct_file_detail_search": "generic_direct_file_detail_search",
    "detail_direct_file_search": "generic_direct_file_detail_search",
    "landing_page_direct_file_search": "generic_direct_file_detail_search",
    "two_step_direct_file_search": "generic_direct_file_detail_search",
    "direct_file_probe": "generic_direct_file_probe_source",
    "direct_file_probe_source": "generic_direct_file_probe_source",
    "header_probe_direct_file": "generic_direct_file_probe_source",
    "extensionless_direct_file_search": "generic_direct_file_probe_source",
    "reader_page_pack": "generic_reader_page_pack_source",
    "reader_page_pack_source": "generic_reader_page_pack_source",
    "manga_reader_page_pack": "generic_reader_page_pack_source",
    "browser_reader_pack": "generic_reader_page_pack_source",
    "json_direct": "generic_json_direct_source",
    "json_direct_source": "generic_json_direct_source",
    "generic_json_direct": "generic_json_direct_source",
    "json_file_api": "generic_json_direct_source",
    "opds": "generic_opds_catalog",
    "opds_catalog": "generic_opds_catalog",
    "generic_opds": "generic_opds_catalog",
    "opds_acquisition_catalog": "generic_opds_catalog",
    "torrent_rss": "generic_torrent_rss_feed",
    "torrent_rss_feed": "generic_torrent_rss_feed",
    "generic_torrent_rss": "generic_torrent_rss_feed",
    "magnet_rss": "generic_torrent_rss_feed",
    "torrent_detail_rss": "generic_torrent_detail_rss_feed",
    "torrent_detail_rss_feed": "generic_torrent_detail_rss_feed",
    "magnet_detail_rss": "generic_torrent_detail_rss_feed",
    "magnet_detail_rss_feed": "generic_torrent_detail_rss_feed",
    "torrent_html": "generic_torrent_html_search",
    "torrent_html_search": "generic_torrent_html_search",
    "magnet_html": "generic_torrent_html_search",
    "magnet_html_search": "generic_torrent_html_search",
    "generic_magnet_search": "generic_torrent_html_search",
    "torrent_detail": "generic_torrent_detail_search",
    "torrent_detail_search": "generic_torrent_detail_search",
    "magnet_detail": "generic_torrent_detail_search",
    "magnet_detail_search": "generic_torrent_detail_search",
    "two_step_torrent_search": "generic_torrent_detail_search",
    "comicdl": "comic_dl",
    "comic_dl": "comic_dl",
    "comics_downloader": "comics_downloader",
    "comicsdownloader": "comics_downloader",
    "mangabot": "manga_bot",
    "manga_bot": "manga_bot",
    "hakuneko": "hakuneko_haruneko",
    "haruneko": "hakuneko_haruneko",
    "suwayomi": "suwayomi",
    "tachiyomi": "suwayomi",
    "tachidesk": "suwayomi",
    "tachidesk_suwayomi": "suwayomi",
}

PROVIDER_TYPES = {
    "comicvine": "metadata",
    "comic_vine": "metadata",
    "mangadex": "metadata_download_source",
    "suwayomi": "metadata_download_source",
    "kapowarr": "metadata_adapter",
    "prowlarr": "indexer",
    "prowlarr_internet_archive": "indexer",
    "prowlarr_nyaa": "indexer",
    "prowlarr_tokyo_toshokan_manga": "indexer",
    "prowlarr_torrentleech_comics": "indexer",
    "prowlarr_kat_comics": "indexer",
    "prowlarr_pirate_bay_comics": "indexer",
    "prowlarr_torrentdownload_comics": "indexer",
    "prowlarr_dognzb_comics": "indexer",
    "prowlarr_ebookbay": "indexer",
    "prowlarr_academic_torrents": "indexer",
    "prowlarr_bitmagnet": "indexer",
    "generic_torznab_indexer": "indexer",
    "generic_newznab_indexer": "indexer",
    "generic_torrent_rss_feed": "indexer",
    "generic_torrent_detail_rss_feed": "indexer",
    "generic_torrent_html_search": "indexer",
    "generic_torrent_detail_search": "indexer",
    "generic_rss_direct_feed": "direct_download",
    "generic_rss_detail_direct_feed": "direct_download",
    "generic_rss_detail_probe_feed": "direct_download",
    "generic_rss_reader_page_pack_feed": "direct_download",
    "generic_direct_file_search": "direct_download",
    "generic_direct_file_detail_search": "direct_download",
    "generic_direct_file_probe_source": "direct_download",
    "generic_reader_page_pack_source": "direct_download",
    "generic_json_direct_source": "direct_download",
    "generic_opds_catalog": "direct_download",
    "rss": "direct_download",
    "rss_getcomics": "direct_download",
    "comicscodes": "direct_download",
    "standard_ebooks": "direct_download",
    "gutendex": "direct_download",
    "internet_archive": "direct_download",
    "wikisource": "metadata",
    "open_library": "metadata",
    "anilist": "metadata",
    "slskd": "download_source",
    "comic_dl": "download_source",
    "comics_downloader": "download_source",
    "manga_bot": "download_source",
    "hakuneko_haruneko": "download_source",
    "manual_reader_sites": "source",
    "manual_ddl_blogs": "source",
    "manual_search_engines": "source",
    "public_free_book_sites": "source",
    "course_and_video_sites": "source",
    "shadow_libraries": "source",
    "adult_nsfw_sources": "source",
    "private_trackers": "source",
    "audiobook_sources": "source",
    "qbittorrent": "download_client",
    "sabnzbd": "download_client",
    "kavita": "library",
    "importer": "library",
    "library_paths": "path",
    "manual_inboxes": "path",
    "quality_language_rules": "rule",
}

PROVIDER_LABELS = {
    "comicvine": "ComicVine",
    "comic_vine": "Comic Vine",
    "mangadex": "MangaDex",
    "suwayomi": "Suwayomi",
    "kapowarr": "Kapowarr",
    "prowlarr": "Prowlarr",
    "prowlarr_internet_archive": "Prowlarr: Internet Archive",
    "prowlarr_nyaa": "Prowlarr: Nyaa.si",
    "prowlarr_tokyo_toshokan_manga": "Prowlarr: Tokyo Toshokan Manga",
    "prowlarr_torrentleech_comics": "Prowlarr: TorrentLeech Comics",
    "prowlarr_kat_comics": "Prowlarr: KAT Comics",
    "prowlarr_pirate_bay_comics": "Prowlarr: Pirate Bay Comics",
    "prowlarr_torrentdownload_comics": "Prowlarr: TorrentDownload Comics",
    "prowlarr_dognzb_comics": "Prowlarr: DOGnzb Comics",
    "prowlarr_ebookbay": "Prowlarr: EBookBay",
    "prowlarr_academic_torrents": "Prowlarr: Academic Torrents",
    "prowlarr_bitmagnet": "Prowlarr: BitMagnet",
    "generic_torznab_indexer": "Generic Torznab / Jackett Indexer",
    "generic_newznab_indexer": "Generic Newznab / NZB Indexer",
    "generic_torrent_rss_feed": "Generic Torrent/Magnet RSS Feed",
    "generic_torrent_detail_rss_feed": "Generic Torrent/Magnet Detail RSS Feed",
    "generic_torrent_html_search": "Generic Torrent/Magnet HTML Search",
    "generic_torrent_detail_search": "Generic Torrent/Magnet Detail Search",
    "generic_rss_direct_feed": "Generic RSS/Atom Direct Feed",
    "generic_rss_detail_direct_feed": "Generic RSS/Atom Detail Direct Feed",
    "generic_rss_detail_probe_feed": "Generic RSS/Atom Detail Probe Feed",
    "generic_rss_reader_page_pack_feed": "Generic RSS/Atom Reader Page Pack Feed",
    "generic_direct_file_search": "Generic Direct File Search",
    "generic_direct_file_detail_search": "Generic Direct File Detail Search",
    "generic_direct_file_probe_source": "Generic Direct File Probe Source",
    "generic_reader_page_pack_source": "Generic Reader Page Pack Source",
    "generic_json_direct_source": "Generic JSON Direct Source",
    "generic_opds_catalog": "Generic OPDS Acquisition Catalog",
    "rss": "RSS",
    "rss_getcomics": "RSS / GetComics",
    "comicscodes": "ComicsCodes",
    "standard_ebooks": "Standard Ebooks",
    "gutendex": "Gutendex / Project Gutenberg",
    "internet_archive": "Internet Archive",
    "wikisource": "Wikisource",
    "open_library": "Open Library",
    "anilist": "AniList",
    "slskd": "SLSKD",
    "comic_dl": "Comic-DL",
    "comics_downloader": "Comics Downloader",
    "manga_bot": "Manga Bot",
    "hakuneko_haruneko": "HakuNeko / HaruNeko",
    "manual_reader_sites": "Reader Sites Bucket",
    "manual_ddl_blogs": "Direct Download Blog/File Buckets",
    "manual_search_engines": "Book Search Engines Bucket",
    "public_free_book_sites": "Public/Free Book Sites Bucket",
    "course_and_video_sites": "Course/Video/Anime DDL Bucket",
    "shadow_libraries": "Shadow Library Search Bucket",
    "adult_nsfw_sources": "Adult/NSFW Source Bucket",
    "private_trackers": "Private/Signup Tracker Bucket",
    "audiobook_sources": "Audiobook Source Bucket",
    "qbittorrent": "qBittorrent",
    "sabnzbd": "SABnzbd",
    "kavita": "Kavita",
    "importer": "Importer",
    "library_paths": "Library Paths",
    "manual_inboxes": "Manual Inboxes",
    "quality_language_rules": "Quality / Language Rules",
    "pack_import": "Pack Import",
}

PROVIDER_TYPE_LABELS = {
    "metadata": "Metadata",
    "metadata_adapter": "Metadata Adapter",
    "metadata_download_source": "Metadata + Download Source",
    "indexer": "Indexer",
    "download_source": "Download Source",
    "direct_download": "Direct Download",
    "download_client": "Download Client",
    "library": "Library",
    "path": "Path",
    "rule": "Rule",
    "source": "Source",
}

PRODUCTIVE_STATUSES = {
    "sent",
    "available",
    "download_started",
    "downloading",
    "started_waiting",
    "already_downloading",
    "waiting_for_transfer",
    "transfer_in_progress",
    "transfer_settling",
    "waiting_for_staged_file",
    "staged_file_settling",
    "staged_file_ready",
    "preview_importable",
    "ready_import",
    "import_busy",
    "verification_pending",
    "imported_not_resolved",
    "verified",
    "imported",
    "already_present",
    "resolved",
    "kavita_verified",
    "already_verified",
}

RETRY_STATUSES = {
    "retry_pending",
    "retry_scheduled",
    "retry_exhausted",
    "no_candidate_retry",
    "provider_wait",
    "provider_unavailable",
    "transient_error",
    "api_error",
    "download_api_error",
    "download_preflight_api_error",
    "source_busy",
}

TERMINAL_FAILURE_STATUSES = {
    "blocked",
    "bad_archive",
    "candidate_failed",
    "failed",
    "failed_download",
    "language_blocked",
    "manual_review",
    "preview_not_importable",
    "stale_no_local_file",
    "transfer_failed",
    "transfer_missing_stale",
    "transfer_stale_unknown",
    "transfer_stalled",
    "wrong_series_or_subseries",
}

RETIRED_STATUSES = {
    "queue_superseded",
    "superseded",
    "superseded_duplicate",
    "retired",
    "inactive",
    "skipped_superseded",
}

PROVIDER_HEALTH_PROBLEM_STATES = {"backoff", "disabled", "error", "failed", "unavailable", "watch"}

CONTRACT_VERSION = 1


def normalize_key(value):
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return re.sub(r"_+", "_", text).strip("_")


def normalize_title(value):
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def stable_id(*parts):
    raw = "|".join(str(part or "") for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def provider_key(value):
    key = normalize_key(value)
    return PROVIDER_ALIASES.get(key, key)


def provider_label(value):
    key = provider_key(value)
    return PROVIDER_LABELS.get(key, str(value or key or "source").replace("_", " ").title())


def provider_type_label(value):
    key = normalize_key(value) or "source"
    return PROVIDER_TYPE_LABELS.get(key, str(value or key).replace("_", " ").title())


def first_value(*values):
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def source_title(attempt):
    attempt = attempt if isinstance(attempt, dict) else {}
    return first_value(
        attempt.get("title"),
        attempt.get("filename"),
        attempt.get("release_title"),
        attempt.get("name"),
        attempt.get("query"),
    )


def attempt_provider_id(attempt):
    attempt = attempt if isinstance(attempt, dict) else {}
    source = provider_key(attempt.get("source"))
    download_client = provider_key(attempt.get("download_client") or attempt.get("downloadClient") or attempt.get("client"))
    protocol = provider_key(attempt.get("protocol"))
    provider = provider_key(attempt.get("provider") or attempt.get("indexer"))
    if source in PROVIDER_TYPES:
        return source
    if download_client in PROVIDER_TYPES:
        return download_client
    if protocol in {"soulseek", "slskd"}:
        return "slskd"
    if protocol == "usenet":
        return download_client if download_client in {"sabnzbd"} else "prowlarr"
    if protocol == "torrent":
        return download_client if download_client in {"qbittorrent"} else "prowlarr"
    if provider in PROVIDER_TYPES:
        return provider
    return provider or source or download_client or protocol or "source"


def lifecycle_phase(status):
    status = str(status or "").strip().lower()
    if not status:
        return "unknown"
    if status in RETIRED_STATUSES or "superseded" in status:
        return "retired"
    if status in {"provider_wait", "provider_unavailable", "provider_limited", "source_limited"}:
        return "provider_wait"
    if status in {"searched_no_candidates", "no_candidate", "no_candidates", "checked_no_candidate", "no_candidate_retry"}:
        return "searched_no_candidates"
    if status in {"searching", "available"}:
        return "searching"
    if status in {"sent", "download_started", "started_waiting", "waiting_for_transfer", "transfer_in_progress", "transfer_settling", "downloading", "already_downloading"}:
        return "downloading"
    if status in {"importing", "import_ready", "staged_file_settling", "staged_file_ready", "preview_importable", "ready_import", "import_busy"}:
        return "staged_or_importing"
    if status in {"verification_pending", "imported_not_resolved"}:
        return "verifying"
    if status in {"resolved", "already_verified", "verified", "kavita_verified", "imported"}:
        return "verified"
    if status in RETRY_STATUSES:
        return "retry_later"
    if "review" in status:
        return "manual_review"
    if status in {"failed_candidate_retry", "candidate_recovery"}:
        return "failed_candidate"
    if status in TERMINAL_FAILURE_STATUSES or "fail" in status or "error" in status or "blocked" in status:
        return "failed_candidate"
    if status in PRODUCTIVE_STATUSES:
        return "active"
    return "observed"


def retry_eligible(status):
    phase = lifecycle_phase(status)
    return phase in {"provider_wait", "searched_no_candidates", "retry_later", "failed_candidate"}


def failure_reason(attempt):
    attempt = attempt if isinstance(attempt, dict) else {}
    status = str(attempt.get("status") or "").strip().lower()
    phase = lifecycle_phase(status)
    if phase not in {"provider_wait", "searched_no_candidates", "retry_later", "failed_candidate", "manual_review"}:
        return ""
    return str(
        first_value(
            attempt.get("failure_reason"),
            attempt.get("blocked_reason"),
            attempt.get("reason"),
            status,
        )
        or ""
    ).strip()


def candidate_identity(attempt):
    attempt = attempt if isinstance(attempt, dict) else {}
    provider_id = attempt_provider_id(attempt)
    title = normalize_title(source_title(attempt))
    url_hash = str(first_value(attempt.get("download_url_hash"), attempt.get("downloadUrlHash"), attempt.get("url_hash")) or "").strip()
    source_path = str(first_value(attempt.get("source_path"), attempt.get("save_path"), attempt.get("local_path")) or "").strip().lower()
    external_id = str(first_value(attempt.get("external_id"), attempt.get("client_id"), attempt.get("client_hash"), attempt.get("slskd_transfer_id"), attempt.get("nzo_id")) or "").strip()
    username = normalize_key(attempt.get("username") or attempt.get("user"))
    issue = normalize_key(attempt.get("issue") or attempt.get("issue_number"))
    identity = first_value(url_hash, external_id, source_path, title)
    if not identity:
        return ""
    return stable_id("candidate", provider_id, username, issue, identity)


def normalize_source_attempt(attempt):
    if not isinstance(attempt, dict):
        return attempt
    out = dict(attempt)
    provider_id = provider_key(out.get("provider_id") or attempt_provider_id(out))
    source = provider_key(out.get("source") or provider_id)
    status = str(out.get("status") or "").strip().lower()
    out.setdefault("provider_id", provider_id)
    out.setdefault("source_type", PROVIDER_TYPES.get(provider_id) or PROVIDER_TYPES.get(source) or "source")
    out["source"] = source or out.get("source")
    out.setdefault("candidate_identity", candidate_identity(out))
    out.setdefault("lifecycle_phase", lifecycle_phase(status))
    out.setdefault("retry_eligible", retry_eligible(status))
    out.setdefault("failure_reason", failure_reason(out))
    title = source_title(out)
    if title:
        out.setdefault("normalized_title", normalize_title(title))
    if out.get("provider") in (None, "") and provider_id not in {"slskd", "rss", "comicscodes", "prowlarr"}:
        out["provider"] = provider_id
    return out


def provider_status_contract(
    record=None,
    *,
    provider_id=None,
    provider_type=None,
    status=None,
    state=None,
    lifecycle=None,
    outcome=None,
    health=None,
    activity=None,
    enabled=None,
    detail=None,
    next_action=None,
    row_kind=None,
):
    record = record if isinstance(record, dict) else {}
    health = health if isinstance(health, dict) else (record.get("provider_health") if isinstance(record.get("provider_health"), dict) else {})
    activity = activity if isinstance(activity, dict) else (record.get("activity") if isinstance(record.get("activity"), dict) else {})
    provider_id = provider_key(
        provider_id
        or record.get("provider_id")
        or record.get("provider_key")
        or attempt_provider_id(record)
    )
    provider_type = provider_type or record.get("provider_type") or PROVIDER_TYPES.get(provider_id) or "source"
    status = str(status if status is not None else record.get("status") or record.get("last_status") or "").strip().lower()
    state = str(state if state is not None else record.get("state") or record.get("display_state") or record.get("queue_state") or "").strip().lower()
    phase = str(lifecycle if lifecycle is not None else record.get("lifecycle_phase") or record.get("display_phase") or "").strip().lower()
    if not phase:
        phase = lifecycle_phase(status or state)
    if phase == "unknown" and state:
        phase = lifecycle_phase(state)
    status_phase = lifecycle_phase(status)
    if status_phase in {"manual_review", "failed_candidate", "retry_later", "searched_no_candidates", "provider_wait", "retired"}:
        phase = status_phase
    elif status_phase in {"searching", "downloading", "staged_or_importing", "verifying", "verified"} and phase in {"failed_candidate", "retry_later", "searched_no_candidates", "provider_wait", "queued", "in_progress", "wanted", "unknown", "observed"}:
        phase = status_phase
    elif phase in {"", "unknown", "observed", "queued", "wanted", "in_progress"} and status_phase not in {"unknown", "observed"}:
        phase = status_phase
    outcome_value = str(outcome if outcome is not None else record.get("outcome") or "").strip().lower()
    if isinstance(record.get("outcome"), dict):
        outcome_value = str(record["outcome"].get("label") or record["outcome"].get("state") or "").strip().lower()
    health_state = str(health.get("state") or "").strip().lower()
    health_problem = health_state in PROVIDER_HEALTH_PROBLEM_STATES
    if enabled is None:
        enabled = record.get("enabled")
    enabled_bool = True if enabled is None else bool(enabled)

    if not enabled_bool:
        contract_state = "disabled"
        actionability = "configuration"
        needs_user = False
    elif health_problem:
        contract_state = "provider_wait"
        phase = "provider_wait"
        actionability = "automatic_wait"
        needs_user = False
    elif phase in {"searching", "downloading", "staged_or_importing", "verifying", "active"}:
        contract_state = "active"
        actionability = "automatic"
        needs_user = False
    elif phase == "verified":
        contract_state = "healthy"
        actionability = "automatic"
        needs_user = False
    elif phase == "retired":
        contract_state = "historical"
        actionability = "diagnostic"
        needs_user = False
    elif phase in {"searched_no_candidates", "retry_later", "provider_wait"}:
        contract_state = "waiting"
        actionability = "automatic_wait"
        needs_user = False
    elif phase == "failed_candidate":
        contract_state = "recovering"
        actionability = "automatic_recovery"
        needs_user = False
    elif phase == "manual_review":
        contract_state = "manual_exception"
        actionability = "manual_exception"
        needs_user = True
    elif state in {"attention", "failed", "blocked"} or any(token in status for token in ("fail", "error", "blocked")):
        contract_state = "attention"
        actionability = "automatic_recovery"
        needs_user = False
    elif int(activity.get("recent_attempts") or 0) > 0:
        contract_state = "active"
        actionability = "automatic"
        needs_user = False
    elif int(activity.get("bad_source_candidates") or 0) > 0:
        contract_state = "tracking"
        actionability = "diagnostic"
        needs_user = False
    elif state in {"configured", "enabled", "tracking"}:
        contract_state = state
        actionability = "automatic"
        needs_user = False
    else:
        contract_state = "observed"
        actionability = "automatic"
        needs_user = False

    detail_text = str(
        detail
        or record.get("detail")
        or record.get("activity_summary")
        or health.get("detail")
        or health.get("message")
        or ""
    ).strip()
    next_action_text = str(next_action or record.get("next_action") or "").strip()
    if not next_action_text:
        if contract_state == "provider_wait":
            next_action_text = f"Waiting for {provider_label(provider_id)} to become healthy"
        elif contract_state == "waiting":
            next_action_text = f"{provider_label(provider_id)} will retry automatically"
        elif contract_state == "manual_exception":
            next_action_text = "Manual review exception"
        elif contract_state == "recovering":
            next_action_text = "Automatic retry or cleanup should continue"
        elif contract_state == "historical":
            next_action_text = "Historical provider/source record"
        else:
            next_action_text = f"{provider_label(provider_id)} is available to InkDrop"

    return {
        "contract_version": CONTRACT_VERSION,
        "row_kind": str(row_kind or record.get("kind") or "").strip() or None,
        "provider_id": provider_id,
        "provider_label": provider_label(provider_id),
        "provider_type": provider_type,
        "provider_type_label": provider_type_label(provider_type),
        "state": contract_state,
        "phase": phase,
        "status": status or None,
        "outcome": outcome_value or None,
        "actionability": actionability,
        "needs_user": needs_user,
        "retry_eligible": retry_eligible(status or phase),
        "health_state": health_state or None,
        "health_label": health.get("label") if health else None,
        "health_problem": health_problem,
        "enabled": enabled_bool,
        "detail": detail_text,
        "next_action": next_action_text,
        "source_attempt_filter": provider_key(provider_id),
    }
