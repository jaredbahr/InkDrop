#!/usr/bin/env python3
import json
import time
import tempfile
from pathlib import Path

import inkdrop_source_catalog as catalog
import inkdrop_source_worker_jobs as jobs
import inkdrop_state


def fail(message):
    print(f"SOURCE_WORKER_JOBS_FAIL: {message}")
    raise SystemExit(1)


def ok(message):
    print(f"SOURCE_WORKER_JOBS_OK: {message}")


def assert_equal(actual, expected, message):
    if actual != expected:
        fail(f"{message}: expected {expected!r}, got {actual!r}")


def assert_true(value, message):
    if not value:
        fail(message)


def assert_false(value, message):
    if value:
        fail(message)


def by_id(rows):
    return {row["provider_id"]: row for row in rows or []}


def snapshot(db_path):
    return inkdrop_state.settings_snapshot(db_path)


SOURCE_TEMPLATE_KINDS = {
    "manual_reader_sites": "html_search_source",
    "manual_ddl_blogs": "html_search_source",
    "manual_search_engines": "html_search_source",
    "public_free_book_sites": "html_search_source",
    "shadow_libraries": "html_search_source",
    "generic_rss_direct_feed": "rss_direct_feed",
    "generic_rss_detail_direct_feed": "rss_detail_direct_feed",
    "generic_rss_detail_probe_feed": "rss_detail_probe_feed",
    "generic_rss_reader_page_pack_feed": "rss_reader_page_pack_feed",
    "generic_direct_file_search": "direct_file_html_search",
    "generic_direct_file_detail_search": "direct_file_detail_search",
    "generic_direct_file_probe_source": "direct_file_probe_source",
    "generic_reader_page_pack_source": "reader_page_pack_source",
    "generic_json_direct_source": "json_direct_source",
    "generic_opds_catalog": "opds_acquisition_catalog",
}


def ensure_provider_parent(db_path, provider_id):
    with inkdrop_state.connect_read(db_path) as con:
        row = con.execute("select 1 from provider_configs where id = ?", (provider_id,)).fetchone()
    if row:
        return
    source_kind = SOURCE_TEMPLATE_KINDS.get(provider_id)
    settings = {
        "implementation_status": "fixture",
        **({"source_kind": source_kind} if source_kind else {}),
    }
    if source_kind:
        # Legacy manual-source buckets are intentionally fixture-only here.
        # Mark them as source templates so the registry keeps them searchable
        # without making them download-capable production providers.
        settings.update(
            {
                "source_template": True,
                "source_mode": "assist",
                "requires_manual_confirm": True,
            }
        )
    inkdrop_state.sync_settings(
        db_path,
        providers=[
            {
                "id": provider_id,
                "provider_type": "source",
                "display_name": provider_id.replace("_", " ").title(),
                "enabled": False,
                "source": "smoke_fixture",
                "settings": settings,
            }
        ],
        settings=[],
    )


def enable_implemented(db_path, provider_id):
    ensure_provider_parent(db_path, provider_id)
    settings = {"implementation_status": "implemented"}
    source_kind = SOURCE_TEMPLATE_KINDS.get(provider_id)
    if source_kind:
        settings.update(
            {
                "source_kind": source_kind,
                "source_template": True,
                "source_mode": "assist",
                "requires_manual_confirm": True,
            }
        )
    inkdrop_state.update_provider_config(
        db_path,
        provider_id,
        {"enabled": True, "settings": settings},
    )


def configure_auto_provider(db_path, provider_id, policy=None):
    ensure_provider_parent(db_path, provider_id)
    inkdrop_state.update_provider_config(
        db_path,
        provider_id,
        {
            "enabled": True,
            "settings": {
                "implementation_status": "implemented",
                "source_mode": "auto",
                "auto_download_allowed": True,
                "requires_manual_confirm": False,
                "policy": dict(policy or {"requires_manual_confirm": False}),
            },
        },
    )


def ensure_source_attempt_parents(con, *, queue_id, wanted_id, series_id, issue_id, title="Source worker memory row"):
    now = time.time()
    con.execute(
        """
        insert or ignore into series(id, title, sort_title, media_type, publisher, created_at, updated_at, raw_json)
        values(?,?,?,?,?,?,?,?)
        """,
        (series_id, title, title.lower(), "manga", "Fixture", now, now, "{}"),
    )
    con.execute(
        """
        insert or ignore into issues(id, series_id, issue_number, normalized_number, title, created_at, updated_at, raw_json)
        values(?,?,?,?,?,?,?,?)
        """,
        (issue_id, series_id, "1", "001", title, now, now, "{}"),
    )
    con.execute(
        """
        insert or ignore into wanted_items(id, series_id, issue_id, reason, status, priority, created_at, updated_at, raw_json)
        values(?,?,?,?,?,?,?,?,?)
        """,
        (wanted_id, series_id, issue_id, "source worker memory fixture", "wanted", 50, now, now, "{}"),
    )
    con.execute(
        """
        insert or ignore into queue_items(
            id, wanted_id, series_id, issue_id, state, current_source, query, last_event, active,
            created_at, updated_at, retry_after, outcome, display_phase, raw_json
        ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            queue_id,
            wanted_id,
            series_id,
            issue_id,
            "queued",
            "source_ladder",
            title,
            "source worker memory fixture",
            1,
            now,
            now,
            now - 60,
            "waiting",
            "queued",
            "{}",
        ),
    )


def native_auto_provider_configs():
    return [
        {
            "id": "prowlarr",
            "provider_type": "indexer",
            "display_name": "Prowlarr",
            "enabled": True,
            "base_url": "http://prowlarr.local/api/v1",
            "secret_ref": "InkDrop provider setting: api_key; fallback Prowlarr config.xml: ApiKey",
            "settings_group": "indexers",
            "source": "runtime",
            "settings": {
                "implementation_status": "implemented",
                "source_mode": "auto",
                "auto_download_allowed": True,
                "requires_manual_confirm": False,
                "policy": {"requires_manual_confirm": False},
            },
        },
        {
            "id": "rss",
            "provider_type": "direct_download",
            "display_name": "RSS Discovery",
            "enabled": True,
            "base_url": "https://feeds.example/detail.xml",
            "settings_group": "download_sources",
            "source": "runtime",
            "settings": {
                "implementation_status": "implemented",
                "source_mode": "auto",
                "auto_download_allowed": True,
                "requires_manual_confirm": False,
                "policy": {
                    "allowed_extensions": [".cbz", ".zip"],
                    "max_detail_pages": 2,
                    "requires_manual_confirm": False,
                },
            },
        },
    ]


def archive_metadata(identifier="jobs-example-comics"):
    return {
        "metadata": {
            "identifier": identifier,
            "title": "Example Book",
            "creator": ["Jobs Press"],
            "language": ["eng"],
            "mediatype": "texts",
            "collection": ["opensource"],
            "licenseurl": "https://creativecommons.org/publicdomain/mark/1.0/",
            "rights": "Public domain",
        },
        "files": [
            {
                "name": f"{identifier}.pdf",
                "format": "Text PDF",
                "source": "original",
                "size": "1048576",
            }
        ],
    }


def fake_http_get(request):
    url = request["url"]
    if url == "https://standardebooks.org/feeds/opds":
        return {
            "text": """<?xml version="1.0" encoding="utf-8"?>
            <feed xmlns="http://www.w3.org/2005/Atom">
              <entry>
                <title>Example Book</title>
                <id>https://standardebooks.org/ebooks/example/book</id>
                <author><name>Example Author</name></author>
                <link rel="http://opds-spec.org/acquisition" type="application/epub+zip"
                  href="https://standardebooks.org/ebooks/example/book/downloads/example_book.epub"/>
              </entry>
            </feed>
            """,
            "headers": {"Content-Type": "application/atom+xml"},
        }
    if url == "https://gutendex.com/books":
        return {
            "json": {
                "count": 1,
                "results": [
                    {
                        "id": 777,
                        "title": "Example Book",
                        "authors": [{"name": "Example, Author"}],
                        "languages": ["en"],
                        "copyright": False,
                        "formats": {
                            "application/epub+zip": "https://www.gutenberg.org/ebooks/777.epub3.images"
                        },
                    }
                ],
            },
            "headers": {"Content-Type": "application/json"},
        }
    if url == "https://archive.org/services/search/v1/scrape":
        return {
            "json": {
                "items": [{"identifier": "jobs-example-comics", "title": "Example Book"}],
                "count": 1,
            },
            "headers": {"Content-Type": "application/json"},
        }
    if url == "https://archive.org/metadata/jobs-example-comics":
        return {"json": archive_metadata(), "headers": {"Content-Type": "application/json"}}
    if url == "https://api.mangadex.org/manga":
        return {
            "json": {
                "data": [
                    {
                        "id": "jobs-mangadex-example",
                        "type": "manga",
                        "attributes": {"title": {"en": "Example Book"}},
                    }
                ]
            },
            "headers": {"Content-Type": "application/json"},
        }
    if url == "https://api.mangadex.org/manga/jobs-mangadex-example/feed":
        return {
            "json": {
                "data": [
                    {
                        "id": "jobs-mangadex-chapter-001",
                        "type": "chapter",
                        "attributes": {
                            "chapter": "1",
                            "volume": "1",
                            "title": "Opening",
                            "translatedLanguage": "en",
                        },
                    }
                ]
            },
            "headers": {"Content-Type": "application/json"},
        }
    if url == "https://api.mangadex.org/at-home/server/jobs-mangadex-chapter-001":
        return {
            "json": {
                "baseUrl": "https://uploads.mangadex.org",
                "chapter": {
                    "hash": "jobs-mangadex-hash",
                    "data": ["001.jpg", "002.jpg", "003.webp"],
                    "dataSaver": ["001-saver.jpg", "002-saver.jpg", "003-saver.jpg"],
                },
            },
            "headers": {"Content-Type": "application/json"},
        }
    if url == "http://prowlarr.local/api/v1/search":
        params = request.get("params") or {}
        if params.get("query") == "Absolute Superman 20":
            return {
                "json": [],
                "headers": {"Content-Type": "application/json"},
            }
        if params.get("query") == "DC Week":
            return {
                "json": [
                    {
                        "title": "2019.08.28.DC.Week+/Detective.Comics.1010.2019.Digital.Zone-Empire",
                        "protocol": "torrent",
                        "indexer": "TorrentLeech",
                        "indexerId": 444,
                        "categories": [{"id": 7030, "name": "Books/Comics"}],
                        "seeders": 8,
                        "infoHash": "NOISYDCWEEKDETECTIVE2019",
                        "downloadUrl": "https://torrentleech.example/get/noisy-dc-week-detective.torrent",
                        "infoUrl": "https://torrentleech.example/torrent/noisy-dc-week-detective",
                        "size": 1400000000,
                    },
                    {
                        "title": "2019.08.28.DC.Week+/Action.Comics.1014.2019.Digital.Zone-Empire",
                        "protocol": "torrent",
                        "indexer": "TorrentLeech",
                        "indexerId": 444,
                        "categories": [{"id": 7030, "name": "Books/Comics"}],
                        "seeders": 7,
                        "infoHash": "NOISYDCWEEKACTION2019",
                        "downloadUrl": "https://torrentleech.example/get/noisy-dc-week-action.torrent",
                        "infoUrl": "https://torrentleech.example/torrent/noisy-dc-week-action",
                        "size": 1400000000,
                    },
                    {
                        "title": "2019.08.21.DC.Week+/MAD.Magazine.009.2019.digital.Son.of.Ultron-Empire",
                        "protocol": "torrent",
                        "indexer": "TorrentLeech",
                        "indexerId": 444,
                        "categories": [{"id": 7030, "name": "Books/Comics"}],
                        "seeders": 6,
                        "infoHash": "NOISYDCWEEKMAD2019",
                        "downloadUrl": "https://torrentleech.example/get/noisy-dc-week-mad.torrent",
                        "infoUrl": "https://torrentleech.example/torrent/noisy-dc-week-mad",
                        "size": 900000000,
                    },
                    {
                        "title": f"DC Comics Weekly Releases {time.localtime().tm_year}",
                        "protocol": "torrent",
                        "indexer": "TorrentLeech",
                        "indexerId": 444,
                        "categories": [{"id": 7030, "name": "Books/Comics"}],
                        "seeders": 34,
                        "infoHash": "ABSOLUTESUPERMANWEEKLYPACK20260624",
                        "downloadUrl": "https://torrentleech.example/get/weekly-comics-2026-06-24.torrent",
                        "infoUrl": "https://torrentleech.example/torrent/weekly-comics-2026-06-24",
                        "size": 4920000000,
                    }
                ],
                "headers": {"Content-Type": "application/json"},
            }
        if params.get("indexerIds") == "888":
            return {
                "json": [
                    {
                        "title": "Example Book Open Education Dataset 001",
                        "protocol": "torrent",
                        "indexer": "Academic Torrents",
                        "indexerId": 888,
                        "categories": [{"id": 8000, "name": "Books"}],
                        "seeders": 3,
                        "infoHash": "JOBSACADEMICHASH123456789",
                        "magnetUrl": "magnet:?xt=urn:btih:JOBSACADEMICHASH123456789",
                        "size": 987654321,
                    }
                ],
                "headers": {"Content-Type": "application/json"},
            }
        if params.get("indexerIds") in (["6", "46"], "6,46"):
            return {
                "json": [
                    {
                        "title": "Example Book 001 (2026) (Digital).cbz",
                        "protocol": "torrent",
                        "indexer": "Nyaa.si",
                        "indexerId": 6,
                        "categories": [{"id": 7030, "name": "Books/Comics"}],
                        "seeders": 8,
                        "infoHash": "JOBSNyaa123456789",
                        "magnetUrl": "magnet:?xt=urn:btih:JOBSNyaa123456789",
                        "size": 123456789,
                    }
                ],
                "headers": {"Content-Type": "application/json"},
            }
        if params.get("indexerIds") in (["45"], "45"):
            return {
                "json": [
                    {
                        "title": "Example Book 001 (2026) (Digital).cbz",
                        "protocol": "torrent",
                        "indexer": "Tokyo Toshokan",
                        "indexerId": 45,
                        "categories": [{"id": 7030, "name": "Books/Comics"}],
                        "seeders": 6,
                        "infoHash": "JOBSTokyo123456789",
                        "magnetUrl": "magnet:?xt=urn:btih:JOBSTokyo123456789",
                        "size": 122456789,
                    }
                ],
                "headers": {"Content-Type": "application/json"},
            }
        if params.get("indexerIds") in (["15"], "15"):
            return {
                "json": [
                    {
                        "title": "Example Book 001 (2026) (Digital).cbz",
                        "protocol": "usenet",
                        "indexer": "DOGnzb",
                        "indexerId": 15,
                        "categories": [{"id": 7030, "name": "Books/Comics"}],
                        "downloadUrl": "https://dognzb.example/api?t=get&id=example-book-001",
                        "guid": "dognzb-example-book-001",
                        "size": 124456789,
                    }
                ],
                "headers": {"Content-Type": "application/json"},
            }
        return {
            "json": [
                {
                    "title": "Example Book 001 (2026) (Digital).cbz",
                    "protocol": "torrent",
                    "indexer": "Nyaa.si",
                    "indexerId": 321,
                    "categories": [{"id": 7030, "name": "Books/Comics"}],
                    "seeders": 8,
                    "infoHash": "JOBS123456789",
                    "magnetUrl": "magnet:?xt=urn:btih:JOBS123456789",
                    "size": 123456789,
                }
            ],
            "headers": {"Content-Type": "application/json"},
        }
    if url == "https://torrentleech.example/get/weekly-comics-2026-06-24.torrent":
        return {
            "text": """2026.06.24 DC Week:
              Absolute Superman 020 (2026) (Digital) (Lil-Empire).cbz
              Absolute Wonder Woman 021 (2026) (Digital) (Lil-Empire).cbz
              Green Lantern 036 (2026) (Digital) (Pyrate-DCP).cbz
            """,
            "headers": {"Content-Type": "application/x-bittorrent"},
        }
    if url in {
        "https://torrentleech.example/get/noisy-dc-week-detective.torrent",
        "https://torrentleech.example/get/noisy-dc-week-action.torrent",
        "https://torrentleech.example/get/noisy-dc-week-mad.torrent",
    }:
        return {
            "text": """2019.08.28 DC Week:
              Detective Comics 1010 (2019) (Digital) (Zone-Empire).cbz
              Action Comics 1014 (2019) (Digital) (Zone-Empire).cbz
              MAD Magazine 009 (2019) (Digital) (Son of Ultron-Empire).cbz
            """,
            "headers": {"Content-Type": "application/x-bittorrent"},
        }
    if url == "http://jackett.local/api/v2.0/indexers/example/results/torznab":
        params = request.get("params") or {}
        query = params.get("q")
        assert_equal(params.get("t"), "search", "Torznab search type")
        assert_equal(params.get("cat"), "7030,8010", "Torznab category gate")
        assert_equal((request.get("secret_params") or {}).get("apikey"), "<secret_ref:torznab_api_key>", "Torznab secret param")
        if query == "Absolute Superman 20":
            return {
                "text": """<?xml version="1.0" encoding="utf-8"?>
                <rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed"><channel></channel></rss>
                """,
                "headers": {"Content-Type": "application/rss+xml"},
            }
        if query == "DC Week":
            return {
                "text": f"""<?xml version="1.0" encoding="utf-8"?>
                <rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed">
                  <channel>
                    <item>
                      <title>DC Comics Weekly Releases {time.localtime().tm_year}</title>
                      <guid>torznab-jobs-dc-weekly-pack</guid>
                      <link>https://torznab.example/get/jobs-dc-weekly-pack.torrent</link>
                      <comments>https://torznab.example/details/jobs-dc-weekly-pack</comments>
                      <torznab:attr name="category" value="7030"/>
                      <torznab:attr name="seeders" value="29"/>
                      <torznab:attr name="size" value="4920000000"/>
                      <torznab:attr name="infohash" value="TORZNABJOBSDCWEEKLYPACKHASH1234567890"/>
                    </item>
                  </channel>
                </rss>
                """,
                "headers": {"Content-Type": "application/rss+xml"},
            }
        if query != "Example Book":
            return {
                "text": """<?xml version="1.0" encoding="utf-8"?>
                <rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed"><channel></channel></rss>
                """,
                "headers": {"Content-Type": "application/rss+xml"},
            }
        return {
            "text": """<?xml version="1.0" encoding="utf-8"?>
            <rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed">
              <channel>
                <item>
                  <title>Example Book 001 (2026) (Digital).cbz</title>
                  <guid>torznab-jobs-example-book-001</guid>
                  <link>magnet:?xt=urn:btih:TORZNABJOBSHASH123456789</link>
                  <torznab:attr name="category" value="7030"/>
                  <torznab:attr name="seeders" value="9"/>
                  <torznab:attr name="size" value="23456789"/>
                  <torznab:attr name="infohash" value="TORZNABJOBSHASH123456789"/>
                </item>
              </channel>
            </rss>
            """,
            "headers": {"Content-Type": "application/rss+xml"},
        }
    if url == "https://torznab.example/get/jobs-dc-weekly-pack.torrent":
        assert_true(request.get("allow_truncated"), "Torznab jobs pack detail fetch opts into truncation")
        return {
            "text": """
            2026.06.24 DC Week:
              Absolute Superman 020 (2026) (Digital) (Lil-Empire).cbz
              Absolute Wonder Woman 021 (2026) (Digital) (Lil-Empire).cbz
            """,
            "headers": {"Content-Type": "application/x-bittorrent"},
        }
    if url == "https://newznab.example/api":
        params = request.get("params") or {}
        query = params.get("q")
        assert_equal(params.get("t"), "search", "Newznab search type")
        assert_equal(params.get("cat"), "7030,8010", "Newznab category gate")
        assert_equal((request.get("secret_params") or {}).get("apikey"), "<secret_ref:newznab_api_key>", "Newznab secret param")
        if query == "Absolute Superman 20":
            return {
                "text": """<?xml version="1.0" encoding="utf-8"?>
                <rss version="2.0" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/"><channel></channel></rss>
                """,
                "headers": {"Content-Type": "application/rss+xml"},
            }
        if query == "DC Week":
            return {
                "text": f"""<?xml version="1.0" encoding="utf-8"?>
                <rss version="2.0" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">
                  <channel>
                    <item>
                      <title>DC Comics Weekly Releases {time.localtime().tm_year}</title>
                      <guid>newznab-jobs-dc-weekly-pack</guid>
                      <link>https://newznab.example/get/jobs-dc-weekly-pack.nzb</link>
                      <comments>https://newznab.example/details/jobs-dc-weekly-pack</comments>
                      <newznab:attr name="category" value="7030"/>
                      <newznab:attr name="size" value="345678901"/>
                    </item>
                  </channel>
                </rss>
                """,
                "headers": {"Content-Type": "application/rss+xml"},
            }
        if query != "Example Book":
            return {
                "text": """<?xml version="1.0" encoding="utf-8"?>
                <rss version="2.0" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/"><channel></channel></rss>
                """,
                "headers": {"Content-Type": "application/rss+xml"},
            }
        return {
            "text": """<?xml version="1.0" encoding="utf-8"?>
            <rss version="2.0" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">
              <channel>
                <item>
                  <title>Example Book 001 (2026) (Digital).cbz</title>
                  <guid>newznab-jobs-example-book-001</guid>
                  <link>https://newznab.example/get/newznab-jobs-example-book-001.nzb</link>
                  <newznab:attr name="category" value="7030"/>
                  <newznab:attr name="size" value="23456789"/>
                </item>
              </channel>
            </rss>
            """,
            "headers": {"Content-Type": "application/rss+xml"},
        }
    if url == "https://newznab.example/get/jobs-dc-weekly-pack.nzb":
        assert_true(request.get("allow_truncated"), "Newznab jobs pack detail fetch opts into truncation")
        return {
            "text": """<?xml version="1.0" encoding="utf-8"?>
            <nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">
              <file subject="Absolute Superman 020 (2026) (Digital) (Lil-Empire).cbz yEnc (1/9)"></file>
              <file subject="Absolute Wonder Woman 021 (2026) (Digital) (Lil-Empire).cbz yEnc (1/9)"></file>
            </nzb>
            """,
            "headers": {"Content-Type": "application/x-nzb"},
        }
    if url == "https://torrent.example/feed.xml":
        return {
            "text": """<?xml version="1.0" encoding="utf-8"?>
            <rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
              <channel>
                <item>
                  <title>Example Book 001 (2026) (Digital).cbz</title>
                  <link>https://torrent.example/post/example-book-001</link>
                  <guid>torrent-rss-jobs-example-book-001</guid>
                  <size>23456789</size>
                  <content:encoded><![CDATA[
                    <p>Seeders: 9</p>
                    <p>Info Hash: JOBSTORRENTRSSINFOHASH123456789ABCDEF123456</p>
                  ]]></content:encoded>
                </item>
              </channel>
            </rss>
            """,
            "headers": {"Content-Type": "application/rss+xml"},
        }
    if url == "https://torrent-detail-rss.example/feed.xml":
        return {
            "text": """<?xml version="1.0" encoding="utf-8"?>
            <rss version="2.0">
              <channel>
                <item>
                  <title>Example Book 001 (2026) (Digital).cbz</title>
                  <link>https://torrent-detail-rss.example/post/example-book-001</link>
                  <guid>torrent-detail-rss-jobs-example-book-001</guid>
                  <description>Example Book torrent detail feed item.</description>
                </item>
                <item>
                  <title>Example Book 001 (2026) stale mirror</title>
                  <link>https://torrent-detail-rss.example/post/example-book-001-timeout</link>
                  <guid>torrent-detail-rss-jobs-example-book-001-timeout</guid>
                  <description>Example Book stale torrent detail feed item.</description>
                </item>
              </channel>
            </rss>
            """,
            "headers": {"Content-Type": "application/rss+xml"},
        }
    if url == "https://torrent-detail-rss.example/post/example-book-001-timeout":
        raise TimeoutError("simulated stale RSS detail page")
    if url == "https://torrent-detail-rss.example/post/example-book-001":
        return {
            "text": """<html><body>
              <h1>Example Book 001 (2026) (Digital)</h1>
              <p>Seeders: 17</p>
              <p>Info Hash: JOBSDETAILRSSHASH123456789ABCDEF123456</p>
            </body></html>""",
            "headers": {"Content-Type": "text/html"},
        }
    if url == "https://getcomics.org/feed":
        return {
            "text": """<?xml version="1.0" encoding="utf-8"?>
            <rss version="2.0">
              <channel>
                <item>
                  <title>Example Book 001 (2026) (Digital).cbz</title>
                  <link>https://getcomics.org/example-book-001/</link>
                  <guid>example-book-001</guid>
                  <description>Example Book issue feed item.</description>
                </item>
              </channel>
            </rss>
            """,
            "headers": {"Content-Type": "application/rss+xml"},
        }
    if url == "https://getcomics.org/example-book-001/":
        return {
            "text": """<html><body>
              <h1>Example Book 001 (2026) (Digital)</h1>
              <a href="https://pixeldrain.com/u/gcjobs001">Download CBZ</a>
            </body></html>""",
            "headers": {"Content-Type": "text/html"},
        }
    if url == "https://pixeldrain.com/api/file/gcjobs001?download":
        assert_equal(request.get("method"), "HEAD", "GetComics Pixeldrain proof uses HEAD")
        return {
            "headers": {
                "Content-Type": "application/zip",
                "Content-Disposition": 'attachment; filename="Example Book 001.cbz"',
                "Content-Length": "204800",
            },
            "status_code": 200,
        }
    if url == "https://torrent-html.example/search?q=Example+Book":
        return {
            "text": """<html><body>
              <ul>
                <li class="torrent-result">
                  <a href="https://torrent-html.example/post/example-book-001">Example Book 001 (2026) (Digital).cbz</a>
                  <span>Seeders: 13</span>
                  <span>Info Hash: JOBSHTMLINFOHASH123456789ABCDEF123456</span>
                </li>
              </ul>
              <a href="/terms/">Terms</a>
            </body></html>""",
            "headers": {"Content-Type": "text/html"},
        }
    if url == "https://torrent-detail.example/wp-json/wp/v2/search?search=Example+Book&per_page=1&subtype=post":
        return {
            "json": [
                {
                    "id": "torrent-json-search-example-book-001",
                    "title": {"rendered": "Example Book 001 (2026) (Digital).cbz"},
                    "link": "https://torrent-detail.example/post/example-book-001",
                }
            ],
            "headers": {"Content-Type": "application/json"},
        }
    if url == "https://torrent-detail.example/post/example-book-001":
        return {
            "text": """<html><body>
              <p>Seeders: 16</p>
              <p>Info Hash: TORRENTDETAILJOBSHASH123456789ABCDEF123456</p>
            </body></html>""",
            "headers": {"Content-Type": "text/html"},
        }
    if url == "https://feeds.example/direct.xml":
        return {
            "text": """<?xml version="1.0" encoding="utf-8"?>
            <rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
              <channel>
                <item>
                  <title>Example Book 001 (2026) (Digital).cbz</title>
                  <link>https://posts.example/example-book-001/</link>
                  <guid>direct-rss-jobs-example-book-001</guid>
                  <content:encoded><![CDATA[
                    <a href="https://files.example/example-book-001.cbz" type="application/zip" data-size="204800">Download CBZ</a>
                  ]]></content:encoded>
                </item>
              </channel>
            </rss>
            """,
            "headers": {"Content-Type": "application/rss+xml"},
        }
    if url == "https://feeds.example/detail.xml":
        return {
            "text": """<?xml version="1.0" encoding="utf-8"?>
            <rss version="2.0">
              <channel>
                <item>
                  <title>Example Book 001 (2026) (Digital).cbz</title>
                  <link>https://posts.example/detail-feed/example-book-001/</link>
                  <guid>detail-rss-jobs-example-book-001</guid>
                  <description>Example Book detail feed item.</description>
                </item>
              </channel>
            </rss>
            """,
            "headers": {"Content-Type": "application/rss+xml"},
        }
    if url == "https://posts.example/detail-feed/example-book-001/":
        return {
            "text": """<html><body>
              <article>
                <h1>Example Book 001 (2026) (Digital)</h1>
                <a href="https://files.example/detail-feed/example-book-001.cbz" type="application/zip" data-size="204800">Download CBZ</a>
              </article>
            </body></html>""",
            "headers": {"Content-Type": "text/html"},
        }
    if url == "https://feeds.example/probe.xml":
        return {
            "text": """<?xml version="1.0" encoding="utf-8"?>
            <rss version="2.0">
              <channel>
                <item>
                  <title>Example Book 001 probe post</title>
                  <link>https://probe.example/post/example-book-001</link>
                  <guid>probe-rss-jobs-example-book-001</guid>
                  <description>Example Book probe detail feed item.</description>
                </item>
              </channel>
            </rss>
            """,
            "headers": {"Content-Type": "application/rss+xml"},
        }
    if url == "https://feeds.example/reader.xml":
        return {
            "text": """<?xml version="1.0" encoding="utf-8"?>
            <rss version="2.0">
              <channel>
                <item>
                  <title>Example Book 001 reader pages</title>
                  <link>https://reader-pack.example/comic/example-book</link>
                  <guid>reader-rss-jobs-example-book-001</guid>
                  <description>Example Book reader page feed item.</description>
                </item>
              </channel>
            </rss>
            """,
            "headers": {"Content-Type": "application/rss+xml"},
        }
    if url == "https://opds.example/catalog.xml":
        return {
            "text": """<?xml version="1.0" encoding="utf-8"?>
            <feed xmlns="http://www.w3.org/2005/Atom">
              <entry>
                <title>Example Book 001 (2026) (Digital).cbz</title>
                <id>urn:opds-jobs-example-book-001</id>
                <author><name>Jobs Press</name></author>
                <link rel="alternate" href="https://opds.example/books/example-book-001"/>
                <link rel="http://opds-spec.org/acquisition/open-access" href="https://files.example/opds/example-book-001.cbz" type="application/zip" length="204800"/>
              </entry>
            </feed>
            """,
            "headers": {"Content-Type": "application/atom+xml"},
        }
    if url == "https://comics.codes/feed/":
        return {
            "text": """<?xml version="1.0" encoding="utf-8"?>
            <rss version="2.0">
              <channel>
                <item>
                  <title>Example Book 002</title>
                  <link>https://comics.codes/example-book-002/</link>
                  <guid>example-book-002</guid>
                  <description>Example Book issue feed item.</description>
                </item>
              </channel>
            </rss>
            """,
            "headers": {"Content-Type": "application/rss+xml"},
        }
    if url == "https://comics.codes/all-comics-list/":
        return {
            "text": """<html><body>
              <a href="https://comics.codes/example-book-003/">Example Book 003</a>
              <a href="/dmca/">DMCA</a>
            </body></html>""",
            "headers": {"Content-Type": "text/html"},
        }
    if url == "https://comics.codes/all-manga-list/":
        return {
            "text": """<html><body>
              <a href="https://comics.codes/example-manga-001/">Example Manga 001</a>
            </body></html>""",
            "headers": {"Content-Type": "text/html"},
        }
    if url == "https://book-search.example/search?q=Example+Book":
        return {
            "text": """<html><body>
              <a href="https://results.example/books/example-book">Example Book EPUB result</a>
              <a href="/about/">About</a>
            </body></html>""",
            "headers": {"Content-Type": "text/html"},
        }
    if url == "https://public-free.example/search?q=Example+Book":
        return {
            "text": """<html><body>
              <a href="https://public.example/books/example-book">Example Book public catalog result</a>
              <a href="/about/">About</a>
            </body></html>""",
            "headers": {"Content-Type": "text/html"},
        }
    if url == "https://shadow-search.example/search?q=Example+Book":
        return {
            "text": """<html><body>
              <a href="https://shadow.example/md5/example-book">Example Book shadow-library result</a>
              <a href="/login/">Login</a>
            </body></html>""",
            "headers": {"Content-Type": "text/html"},
        }
    if url == "https://ddl-search.example/search?s=Example+Book":
        return {
            "text": """<html><body>
              <a href="https://ddl.example/posts/example-book">Example Book PDF result</a>
              <a href="/terms/">Terms</a>
            </body></html>""",
            "headers": {"Content-Type": "text/html"},
        }
    if url == "https://reader-search.example/search?q=Example+Book":
        return {
            "text": """<html><body>
              <a href="https://reader.example/comic/example-book/1">Example Book 001 reader result</a>
              <a href="/login/">Login</a>
            </body></html>""",
            "headers": {"Content-Type": "text/html"},
        }
    if url == "https://files.example/search?q=Example+Book":
        return {
            "text": """<html><head>
              <link rel="attachment" href="https://redirect.example/out?to=https%3A%2F%2Ffiles.example%2Fdownloads%2Fexample-book-001.cbz" data-size="204800" type="application/zip" />
            </head><body>
              <a href="https://posts.example/example-book-001/">Example Book post page</a>
            </body></html>""",
            "headers": {"Content-Type": "text/html"},
        }
    if url == "https://files.example/search/page/2?q=Example+Book":
        return {
            "text": """<html><head>
              <link rel="attachment" href="https://files.example/downloads/example-book-002.cbz" title="Example Book 002" data-size="204800" type="application/zip" />
            </head><body>
              <a href="https://posts.example/example-book-002/">Example Book second result page</a>
            </body></html>""",
            "headers": {"Content-Type": "text/html"},
        }
    if url == "https://files.example/latest":
        return {
            "text": """<html><head>
              <link rel="attachment" href="https://files.example/downloads/example-book-003.cbz" title="Example Book 003" data-size="204800" type="application/zip" />
            </head><body>
              <a href="/latest/page/2">Next</a>
            </body></html>""",
            "headers": {"Content-Type": "text/html"},
        }
    if url == "https://files.example/latest/page/2":
        return {
            "text": """<html><head>
              <link rel="attachment" href="https://files.example/downloads/example-book-004.cbz" title="Example Book 004" data-size="204800" type="application/zip" />
            </head><body>
              <a href="/latest/page/3">Next</a>
            </body></html>""",
            "headers": {"Content-Type": "text/html"},
        }
    if url == "https://detail.example/wp-json/wp/v2/search?search=Example+Book&per_page=1&subtype=post":
        return {
            "json": [
                {
                    "id": "json-search-example-book-001",
                    "title": {"rendered": "Example Book 001 post page"},
                    "link": "https://detail.example/post/example-book-001",
                }
            ],
            "headers": {"Content-Type": "application/json"},
        }
    if url == "https://detail.example/post/example-book-001":
        return {
            "text": """<html><head>
              <script type="application/ld+json">
                {
                  "@context": "https://schema.org",
                  "@type": "DigitalDocument",
                  "name": "Example Book 001 (2026) (Digital)",
                  "encodingFormat": "application/zip",
                  "contentSize": "204800",
                  "contentUrl": "https://files.example/detail/example-book-001.cbz"
                }
              </script>
            </head><body>
              <p>Preparing download.</p>
            </body></html>""",
            "headers": {"Content-Type": "text/html"},
        }
    if url == "https://probe.example/wp-json/wp/v2/search?search=Example+Book&per_page=1&subtype=post":
        return {
            "json": [
                {
                    "id": "json-probe-example-book-001",
                    "title": {"rendered": "Example Book 001 post page"},
                    "link": "https://probe.example/post/example-book-001",
                }
            ],
            "headers": {"Content-Type": "application/json"},
        }
    if url == "https://probe.example/post/example-book-001":
        return {
            "text": """<html><body>
              <a href="https://pixeldrain.com/u/pdjobs001">Download</a>
            </body></html>""",
            "headers": {"Content-Type": "text/html"},
        }
    if url == "https://pixeldrain.com/api/file/pdjobs001?download":
        assert_equal(request.get("method"), "HEAD", "direct file probe job uses HEAD")
        return {
            "headers": {
                "Content-Type": "application/zip",
                "Content-Disposition": 'attachment; filename="Example Book 001.cbz"',
                "Content-Length": "204800",
            },
            "status_code": 200,
        }
    if url == "https://reader-pack.example/search?q=Example+Book":
        return {
            "text": """<html><body>
              <a href="https://reader-pack.example/comic/example-book">Example Book</a>
              <a href="/privacy/">Privacy</a>
            </body></html>""",
            "headers": {"Content-Type": "text/html"},
        }
    if url == "https://reader-pack.example/comic/example-book":
        return {
            "text": """<html><body>
              <h1>Example Book</h1>
              <a href="https://reader-pack.example/comic/example-book/1">Chapter 1</a>
              <a href="/privacy/">Privacy</a>
            </body></html>""",
            "headers": {"Content-Type": "text/html"},
        }
    if url == "https://reader-pack.example/comic/example-book/1":
        return {
            "text": """<html><body>
              <img src="https://img.reader-pack.example/example-book-001-001.jpg" />
              <img data-src="https://img.reader-pack.example/example-book-001-002.jpg" />
              <img data-original="https://img.reader-pack.example/example-book-001-003.webp" />
            </body></html>""",
            "headers": {"Content-Type": "text/html"},
        }
    if url == "https://json.example/wp-json/wp/v2/posts?search=Example+Book&per_page=1&_fields=id,link,title,content,excerpt":
        return {
            "json": {
                "results": [
                    {
                        "title": "Example Book 001 (2026) (Digital).cbz",
                        "link": "https://json.example/items/example-book-001",
                        "content": {
                            "rendered": "<p><a href=\"https://files.example/json/example-book-001.cbz\" type=\"application/zip\" data-size=\"204800\">Download CBZ</a></p>"
                        },
                    },
                    {
                        "title": "Example Book post page",
                        "url": "https://json.example/items/example-book-001",
                    },
                ]
            },
            "headers": {"Content-Type": "application/json"},
        }
    fail(f"unexpected fake request: {request}")


def seed_suwayomi_source_error_memory(
    db_path,
    *,
    now,
    count=2,
    age_seconds=120,
    source_id="2131019126180322627",
    source_display_name="MangaFire (EN)",
    source_error="simulated MangaFire source search failure",
    stage="source_search",
    reason="partial_suwayomi_source_search_failed",
    queue_prefix="queue-suwayomi-source-error",
    wanted_prefix="wanted-suwayomi-source-error",
    issue_prefix="issue-suwayomi-source-error",
    attempt_prefix="source-attempt-suwayomi-source-error",
):
    with inkdrop_state.connect(db_path) as con:
        for index in range(int(count or 0)):
            attempt_at = now - int(age_seconds or 0) + index
            ensure_source_attempt_parents(
                con,
                queue_id=f"{queue_prefix}-{index}",
                wanted_id=f"{wanted_prefix}-{index}",
                series_id="series-suwayomi-source-error",
                issue_id=f"{issue_prefix}-{index}",
                title="Fairy Tail",
            )
            inkdrop_state.record_source_attempt(
                con,
                queue_id=f"{queue_prefix}-{index}",
                wanted_id=f"{wanted_prefix}-{index}",
                series_id="series-suwayomi-source-error",
                issue_id=f"{issue_prefix}-{index}",
                attempt_id=f"{attempt_prefix}-{index}",
                started_at=attempt_at,
                completed_at=attempt_at,
                attempt={
                    "source": "suwayomi",
                    "provider": "suwayomi",
                    "provider_id": "suwayomi",
                    "source_type": "metadata_download_source",
                    "status": "provider_wait",
                    "title": "Fairy Tail",
                    "reason": reason,
                    "raw": {
                        "fetch": {
                            "payload_mode": "suwayomi_search_then_chapters",
                            "partial_errors": [
                                {
                                    "stage": stage,
                                    "query": "Fairy Tail",
                                    "source_id": source_id,
                                    "source_display_name": source_display_name,
                                    "error": source_error,
                                }
                            ],
                        }
                    },
                },
            )


def seed_suwayomi_volume_gap_memory(
    db_path,
    *,
    now,
    count=2,
    source_id="2499283573021220255",
    source_display_name="MangaDex (EN)",
    series_id="series-suwayomi-volume-gap",
    series_title="Fairy Tail",
    wanted_volume="31",
):
    with inkdrop_state.connect(db_path) as con:
        for index in range(int(count or 0)):
            attempt_at = now - 120 + index
            ensure_source_attempt_parents(
                con,
                queue_id=f"queue-suwayomi-volume-gap-{index}",
                wanted_id=f"wanted-suwayomi-volume-gap-{index}",
                series_id=series_id,
                issue_id=f"issue-suwayomi-volume-gap-{index}",
                title=series_title,
            )
            inkdrop_state.record_source_attempt(
                con,
                queue_id=f"queue-suwayomi-volume-gap-{index}",
                wanted_id=f"wanted-suwayomi-volume-gap-{index}",
                series_id=series_id,
                issue_id=f"issue-suwayomi-volume-gap-{index}",
                attempt_id=f"source-attempt-suwayomi-volume-gap-{index}",
                started_at=attempt_at,
                completed_at=attempt_at,
                attempt={
                    "source": "suwayomi",
                    "provider": "suwayomi",
                    "provider_id": "suwayomi",
                    "source_type": "metadata_download_source",
                    "status": "searched_no_candidates",
                    "title": f"{series_title} Vol. {wanted_volume}",
                    "reason": "suwayomi_volume_metadata_missing",
                    "failure_reason": "suwayomi_volume_metadata_missing",
                    "raw": {
                        "query": f"{series_title} Vol. {wanted_volume}",
                        "failure_reason": "suwayomi_volume_metadata_missing",
                        "fetch": {
                            "payload_mode": "suwayomi_search_then_chapters",
                            "reason": "searched_no_candidates",
                            "suwayomi_payload_summaries": [
                                {
                                    "source_id": source_id,
                                    "source_name": source_display_name,
                                    "source_language": "en",
                                    "source_search_query": series_title,
                                    "manga_title": series_title,
                                    "chapter_count": 180,
                                    "chapter_has_volume_count": 0,
                                    "chapter_matching_wanted_volume_count": 0,
                                    "pages_by_chapter_count": 0,
                                }
                            ],
                        },
                    },
                },
            )


def suwayomi_persisted_source_error_cooldown_http_get(request):
    url = request["url"]
    if url == "http://127.0.0.1:4568/api/v1/source/list":
        return {
            "json": [
                {
                    "id": "2131019126180322627",
                    "displayName": "MangaFire (EN)",
                    "name": "MangaFire",
                    "lang": "en",
                    "baseUrl": "https://mangafire.to",
                },
                {
                    "id": "2499283573021220255",
                    "displayName": "MangaDex (EN)",
                    "name": "MangaDex",
                    "lang": "en",
                    "baseUrl": "https://mangadex.org",
                },
            ],
            "headers": {"Content-Type": "application/json"},
        }
    if url == "http://127.0.0.1:4568/api/v1/extension/list":
        return {"json": [], "headers": {"Content-Type": "application/json"}}
    if url == "http://127.0.0.1:4568/api/v1/source/2131019126180322627/search":
        fail("persisted Suwayomi source-error cooldown should skip MangaFire before search")
    if url == "http://127.0.0.1:4568/api/v1/source/2499283573021220255/search":
        return {"json": {"mangaList": [], "hasNextPage": False}, "headers": {"Content-Type": "application/json"}}
    fail(f"unexpected persisted Suwayomi cooldown request: {request}")


def suwayomi_volume_gap_cooldown_http_get(request):
    url = request["url"]
    if url == "http://127.0.0.1:4568/api/v1/source/list":
        return {
            "json": [
                {
                    "id": "2499283573021220255",
                    "displayName": "MangaDex (EN)",
                    "name": "MangaDex",
                    "lang": "en",
                    "baseUrl": "https://mangadex.org",
                },
                {
                    "id": "2131019126180322627",
                    "displayName": "Weeb Central (EN)",
                    "name": "Weeb Central",
                    "lang": "en",
                    "baseUrl": "https://weebcentral.example",
                },
            ],
            "headers": {"Content-Type": "application/json"},
        }
    if url == "http://127.0.0.1:4568/api/v1/extension/list":
        return {"json": [], "headers": {"Content-Type": "application/json"}}
    if url == "http://127.0.0.1:4568/api/v1/source/2499283573021220255/search":
        fail("Suwayomi volume-gap cooldown should skip MangaDex before search")
    if url == "http://127.0.0.1:4568/api/v1/source/2131019126180322627/search":
        return {"json": {"mangaList": [], "hasNextPage": False}, "headers": {"Content-Type": "application/json"}}
    fail(f"unexpected Suwayomi volume-gap cooldown request: {request}")


def suwayomi_volume_gap_probe_http_get(request):
    url = request["url"]
    if url == "http://127.0.0.1:4568/api/v1/source/list":
        return {
            "json": [
                {
                    "id": "2499283573021220255",
                    "displayName": "MangaDex (EN)",
                    "name": "MangaDex",
                    "lang": "en",
                    "baseUrl": "https://mangadex.org",
                },
                {
                    "id": "2131019126180322627",
                    "displayName": "Weeb Central (EN)",
                    "name": "Weeb Central",
                    "lang": "en",
                    "baseUrl": "https://weebcentral.example",
                },
            ],
            "headers": {"Content-Type": "application/json"},
        }
    if url == "http://127.0.0.1:4568/api/v1/extension/list":
        return {"json": [], "headers": {"Content-Type": "application/json"}}
    if url == "http://127.0.0.1:4568/api/v1/source/2499283573021220255/search":
        return {"json": {"mangaList": [], "hasNextPage": False}, "headers": {"Content-Type": "application/json"}}
    if url == "http://127.0.0.1:4568/api/v1/source/2131019126180322627/search":
        fail("Suwayomi volume-gap cooldown probe should stay bounded to the configured cooled source")
    fail(f"unexpected Suwayomi volume-gap cooldown probe request: {request}")


def suwayomi_persisted_source_error_probe_http_get(request):
    url = request["url"]
    if url == "http://127.0.0.1:4568/api/v1/source/list":
        return {
            "json": [
                {
                    "id": "2131019126180322627",
                    "displayName": "MangaFire (EN)",
                    "name": "MangaFire",
                    "lang": "en",
                    "baseUrl": "https://mangafire.to",
                }
            ],
            "headers": {"Content-Type": "application/json"},
        }
    if url == "http://127.0.0.1:4568/api/v1/extension/list":
        return {"json": [], "headers": {"Content-Type": "application/json"}}
    if url == "http://127.0.0.1:4568/api/v1/source/2131019126180322627/search":
        return {"json": {"mangaList": [], "hasNextPage": False}, "headers": {"Content-Type": "application/json"}}
    fail(f"unexpected persisted Suwayomi cooldown probe request: {request}")


def suwayomi_persisted_source_error_multi_probe_http_get(request):
    url = request["url"]
    if url == "http://127.0.0.1:4568/api/v1/source/list":
        return {
            "json": [
                {
                    "id": "2131019126180322627",
                    "displayName": "MangaFire (EN)",
                    "name": "MangaFire",
                    "lang": "en",
                    "baseUrl": "https://mangafire.to",
                },
                {
                    "id": "2499283573021220255",
                    "displayName": "MangaDex (EN)",
                    "name": "MangaDex",
                    "lang": "en",
                    "baseUrl": "https://mangadex.org",
                },
            ],
            "headers": {"Content-Type": "application/json"},
        }
    if url == "http://127.0.0.1:4568/api/v1/extension/list":
        return {"json": [], "headers": {"Content-Type": "application/json"}}
    if url in {
        "http://127.0.0.1:4568/api/v1/source/2131019126180322627/search",
        "http://127.0.0.1:4568/api/v1/source/2499283573021220255/search",
    }:
        return {"json": {"mangaList": [], "hasNextPage": False}, "headers": {"Content-Type": "application/json"}}
    fail(f"unexpected persisted Suwayomi multi-cooldown probe request: {request}")


def suwayomi_persisted_source_error_rotating_probe_http_get(request):
    url = request["url"]
    if url == "http://127.0.0.1:4568/api/v1/source/list":
        return {
            "json": [
                {
                    "id": "2499283573021220255",
                    "displayName": "MangaDex (EN)",
                    "name": "MangaDex",
                    "lang": "en",
                    "baseUrl": "https://mangadex.org",
                },
                {
                    "id": "569821715369244319",
                    "displayName": "ComicK Fanmade (EN)",
                    "name": "ComicK Fanmade",
                    "lang": "en",
                    "baseUrl": "https://comick.example",
                },
                {
                    "id": "2131019126180322627",
                    "displayName": "Weeb Central (EN)",
                    "name": "Weeb Central",
                    "lang": "en",
                    "baseUrl": "https://weebcentral.example",
                },
            ],
            "headers": {"Content-Type": "application/json"},
        }
    if url == "http://127.0.0.1:4568/api/v1/extension/list":
        return {"json": [], "headers": {"Content-Type": "application/json"}}
    if url in {
        "http://127.0.0.1:4568/api/v1/source/2499283573021220255/search",
        "http://127.0.0.1:4568/api/v1/source/569821715369244319/search",
        "http://127.0.0.1:4568/api/v1/source/2131019126180322627/search",
    }:
        return {"json": {"mangaList": [], "hasNextPage": False}, "headers": {"Content-Type": "application/json"}}
    fail(f"unexpected persisted Suwayomi rotating cooldown probe request: {request}")


def fake_http_get_nyaa_categoryless_fallback(request):
    url = request["url"]
    params = request.get("params") or {}
    if url != "http://prowlarr.local/api/v1/search":
        fail(f"unexpected Nyaa categoryless fallback URL: {request}")
    if params.get("indexerIds") in (["6", "46"], "6,46") and params.get("categories") == ["7030"]:
        return {"json": [], "headers": {"Content-Type": "application/json"}}
    if params.get("indexerIds") == "6" and not params.get("categories"):
        fail("categoryless Nyaa fallback must not search the generic anime-heavy indexer")
    if params.get("indexerIds") == "46" and not params.get("categories"):
        return {
            "json": [
                {
                    "title": "Example Book 001 (2026) (Digital).cbz",
                    "protocol": "torrent",
                    "indexer": "Nyaa.si Literature",
                    "indexerId": 46,
                    "categories": [{"id": 7000, "name": "Books"}, {"id": 156719, "name": "Manga"}],
                    "seeders": 5,
                    "infoHash": "JOBSNyaaLiteratureFallback123456789",
                    "magnetUrl": "magnet:?xt=urn:btih:JOBSNyaaLiteratureFallback123456789",
                    "size": 123456789,
                }
            ],
            "headers": {"Content-Type": "application/json"},
        }
    fail(f"unexpected Nyaa categoryless fallback request: {request}")


def main():
    with tempfile.TemporaryDirectory(prefix="inkdrop-source-worker-jobs-") as tmp:
        db_path = Path(tmp) / "inkdrop-state.sqlite3"
        seed = catalog.settings_seed_payload()
        inkdrop_state.sync_settings(db_path, providers=seed["providers"], settings=seed["settings"])
        inkdrop_state.sync_settings(db_path, providers=native_auto_provider_configs())

        for provider_id in (
            "standard_ebooks",
            "gutendex",
            "internet_archive",
            "mangadex",
            "suwayomi",
            "prowlarr_nyaa",
            "prowlarr_tokyo_toshokan_manga",
            "prowlarr_torrentleech_comics",
            "prowlarr_dognzb_comics",
            "generic_torznab_indexer",
            "generic_newznab_indexer",
            "generic_torrent_rss_feed",
            "generic_torrent_detail_rss_feed",
            "comic_dl",
            "comics_downloader",
            "manga_bot",
            "hakuneko_haruneko",
            "manual_reader_sites",
            "manual_ddl_blogs",
            "manual_search_engines",
            "public_free_book_sites",
            "shadow_libraries",
            "rss_getcomics",
            "generic_rss_direct_feed",
            "generic_rss_detail_direct_feed",
            "generic_rss_detail_probe_feed",
            "generic_rss_reader_page_pack_feed",
            "generic_direct_file_search",
            "generic_direct_file_detail_search",
            "generic_direct_file_probe_source",
            "generic_reader_page_pack_source",
            "generic_json_direct_source",
            "generic_opds_catalog",
            "comicscodes",
        ):
            enable_implemented(db_path, provider_id)

        configure_auto_provider(db_path, "standard_ebooks")
        configure_auto_provider(db_path, "gutendex")
        configure_auto_provider(
            db_path,
            "internet_archive",
            {
                "rights_gate": "public_domain_or_open_license_required",
                "direct_url_policy": "allow_archive_download_links_after_file_metadata_check",
                "allowed_extensions": [".pdf"],
                "requires_manual_confirm": False,
            },
        )
        inkdrop_state.update_provider_config(
            db_path,
            "prowlarr_nyaa",
            {
                "base_url": "http://prowlarr.local",
                "secret_ref": "prowlarr_api_key",
                "settings": {
                    "implementation_status": "implemented",
                    "source_mode": "auto",
                    "auto_download_allowed": True,
                    "requires_manual_confirm": False,
                    "policy": {
                        "rights_gate": "user_owned_collection_required",
                        "direct_url_policy": "download_client_handoff_after_strict_candidate_verdict",
                        "allowed_languages": ["en"],
                        "categories": ["7030"],
                        "minimum_seeders": 1,
                        "indexer_ids": ["6", "46"],
                        "categoryless_fallback_indexer_ids": ["46"],
                        "requires_manual_confirm": False,
                        "scope_policy": "manga_metadata_or_manga_publisher",
                    },
                },
            },
        )
        inkdrop_state.update_provider_config(
            db_path,
            "prowlarr_tokyo_toshokan_manga",
            {
                "base_url": "http://prowlarr.local",
                "secret_ref": "prowlarr_api_key",
                "settings": {
                    "implementation_status": "implemented",
                    "source_mode": "auto",
                    "auto_download_allowed": True,
                    "requires_manual_confirm": False,
                    "policy": {
                        "rights_gate": "user_owned_collection_required",
                        "direct_url_policy": "download_client_handoff_after_strict_candidate_verdict",
                        "allowed_languages": ["en"],
                        "categories": ["7030", "8000", "8010"],
                        "minimum_seeders": 1,
                        "indexer_ids": ["45"],
                        "requires_manual_confirm": False,
                        "scope_policy": "manga_metadata_or_manga_publisher",
                    },
                },
            },
        )
        inkdrop_state.update_provider_config(
            db_path,
            "prowlarr_dognzb_comics",
            {
                "base_url": "http://prowlarr.local",
                "secret_ref": "prowlarr_api_key",
                "settings": {
                    "implementation_status": "implemented",
                    "source_mode": "auto",
                    "auto_download_allowed": True,
                    "requires_manual_confirm": False,
                    "policy": {
                        "rights_gate": "user_owned_collection_required",
                        "direct_url_policy": "download_client_handoff_after_strict_candidate_verdict",
                        "categories": ["7030"],
                        "minimum_seeders": 0,
                        "indexer_ids": ["15"],
                        "download_client_by_protocol": {"usenet": "sabnzbd"},
                        "requires_manual_confirm": False,
                    },
                },
            },
        )
        inkdrop_state.update_provider_config(
            db_path,
            "generic_torznab_indexer",
            {
                "base_url": "http://jackett.local/api/v2.0/indexers/example/results/torznab",
                "secret_ref": "torznab_api_key",
                "settings": {
                    "implementation_status": "implemented",
                    "policy": {"categories": ["7030", "8010"], "minimum_seeders": 1},
                },
            },
        )
        inkdrop_state.update_provider_config(
            db_path,
            "generic_newznab_indexer",
            {
                "base_url": "https://newznab.example/api",
                "secret_ref": "newznab_api_key",
                "settings": {
                    "implementation_status": "implemented",
                    "policy": {"categories": ["7030", "8010"]},
                },
            },
        )
        inkdrop_state.update_provider_config(
            db_path,
            "generic_torrent_rss_feed",
            {
                "base_url": "https://torrent.example/feed.xml",
                "settings": {
                    "implementation_status": "implemented",
                    "policy": {
                        "allowed_extensions": [".cbz", ".zip"],
                        "minimum_seeders": 1,
                        "requires_manual_confirm": True,
                    },
                },
            },
        )
        inkdrop_state.update_provider_config(
            db_path,
            "generic_torrent_detail_rss_feed",
            {
                "base_url": "https://torrent-detail-rss.example/feed.xml",
                "settings": {
                    "implementation_status": "implemented",
                    "policy": {
                        "allowed_extensions": [".cbz", ".zip"],
                        "minimum_seeders": 1,
                        "requires_manual_confirm": True,
                        "max_detail_pages": 2,
                    },
                },
            },
        )
        inkdrop_state.update_provider_config(
            db_path,
            "generic_rss_direct_feed",
            {
                "base_url": "https://feeds.example/direct.xml",
                "settings": {
                    "implementation_status": "implemented",
                    "policy": {
                        "allowed_extensions": [".cbz", ".zip"],
                        "requires_manual_confirm": True,
                    },
                },
            },
        )
        inkdrop_state.update_provider_config(
            db_path,
            "generic_rss_detail_direct_feed",
            {
                "base_url": "https://feeds.example/detail.xml",
                "settings": {
                    "implementation_status": "implemented",
                    "policy": {
                        "allowed_extensions": [".cbz", ".zip"],
                        "requires_manual_confirm": True,
                        "max_detail_pages": 2,
                    },
                },
            },
        )
        inkdrop_state.update_provider_config(
            db_path,
            "generic_rss_detail_probe_feed",
            {
                "base_url": "https://feeds.example/probe.xml",
                "settings": {
                    "implementation_status": "implemented",
                    "policy": {
                        "allowed_extensions": [".cbz", ".zip"],
                        "shared_file_hosts": ["pixeldrain"],
                        "requires_manual_confirm": True,
                        "max_detail_pages": 2,
                        "max_probe_links": 2,
                    },
                },
            },
        )
        inkdrop_state.update_provider_config(
            db_path,
            "generic_rss_reader_page_pack_feed",
            {
                "base_url": "https://feeds.example/reader.xml",
                "settings": {
                    "implementation_status": "implemented",
                    "policy": {
                        "allowed_extensions": [".jpg", ".jpeg", ".png", ".webp"],
                        "requires_manual_confirm": True,
                        "min_page_images": 2,
                        "max_series_pages": 2,
                        "max_reader_pages": 3,
                    },
                },
            },
        )
        inkdrop_state.update_provider_config(
            db_path,
            "generic_direct_file_search",
            {
                "settings": {
                    "implementation_status": "implemented",
                    "policy": {
                        "search_url_templates": ["https://files.example/search?q={query}"],
                        "pagination_url_templates": ["https://files.example/search/page/{page}?q={query}"],
                        "list_url_templates": ["https://files.example/latest"],
                        "list_pagination_url_templates": ["https://files.example/latest/page/{page}"],
                        "max_search_pages": 2,
                        "allowed_extensions": [".cbz", ".zip"],
                        "requires_manual_confirm": True,
                    },
                },
            },
        )
        inkdrop_state.update_provider_config(
            db_path,
            "generic_direct_file_detail_search",
            {
                "base_url": "https://detail.example",
                "settings": {
                    "implementation_status": "implemented",
                    "policy": {
                        "search_api_flavors": ["wordpress"],
                        "allowed_extensions": [".cbz", ".zip"],
                        "requires_manual_confirm": True,
                        "max_detail_pages": 3,
                    },
                },
            },
        )
        inkdrop_state.update_provider_config(
            db_path,
            "generic_direct_file_probe_source",
            {
                "base_url": "https://probe.example",
                "settings": {
                    "implementation_status": "implemented",
                    "policy": {
                        "search_api_flavors": ["wordpress"],
                        "allowed_extensions": [".cbz", ".zip"],
                        "requires_manual_confirm": True,
                        "max_detail_pages": 3,
                        "max_probe_links": 3,
                    },
                },
            },
        )
        inkdrop_state.update_provider_config(
            db_path,
            "generic_reader_page_pack_source",
            {
                "settings": {
                    "implementation_status": "implemented",
                    "policy": {
                        "search_url_templates": ["https://reader-pack.example/search?q={query}"],
                        "allowed_extensions": [".jpg", ".jpeg", ".png", ".webp"],
                        "requires_manual_confirm": True,
                        "min_page_images": 2,
                        "max_series_pages": 2,
                        "max_reader_pages": 3,
                    },
                },
            },
        )
        inkdrop_state.update_provider_config(
            db_path,
            "generic_json_direct_source",
            {
                "base_url": "https://json.example",
                "settings": {
                    "implementation_status": "implemented",
                    "policy": {
                        "search_api_flavors": ["wordpress_posts"],
                        "allowed_extensions": [".cbz", ".zip"],
                        "requires_manual_confirm": True,
                    },
                },
            },
        )
        inkdrop_state.update_provider_config(
            db_path,
            "generic_opds_catalog",
            {
                "base_url": "https://opds.example/catalog.xml",
                "settings": {
                    "implementation_status": "implemented",
                    "policy": {
                        "allowed_extensions": [".cbz", ".zip"],
                        "requires_manual_confirm": True,
                    },
                },
            },
        )
        inkdrop_state.update_provider_config(
            db_path,
            "manual_search_engines",
            {
                "settings": {
                    "implementation_status": "implemented",
                    "policy": {
                        "search_url_templates": ["https://book-search.example/search?q={query}"],
                        "source_site_label": "Rave Book Search",
                        "requires_manual_confirm": True,
                    },
                },
            },
        )
        inkdrop_state.update_provider_config(
            db_path,
            "manual_ddl_blogs",
            {
                "settings": {
                    "implementation_status": "implemented",
                    "policy": {
                        "search_url_templates": ["https://ddl-search.example/search?s={query}"],
                        "source_site_label": "DownMagaz",
                        "requires_manual_confirm": True,
                    },
                },
            },
        )
        inkdrop_state.update_provider_config(
            db_path,
            "manual_reader_sites",
            {
                "settings": {
                    "implementation_status": "implemented",
                    "policy": {
                        "search_url_templates": ["https://reader-search.example/search?q={query}"],
                        "source_site_label": "ReadComicOnline",
                        "requires_manual_confirm": True,
                    },
                },
            },
        )
        inkdrop_state.update_provider_config(
            db_path,
            "public_free_book_sites",
            {
                "settings": {
                    "implementation_status": "implemented",
                    "policy": {
                        "search_url_templates": ["https://public-free.example/search?q={query}"],
                        "source_site_label": "Manybooks",
                        "requires_manual_confirm": True,
                    },
                },
            },
        )
        inkdrop_state.update_provider_config(
            db_path,
            "shadow_libraries",
            {
                "settings": {
                    "implementation_status": "implemented",
                    "policy": {
                        "search_url_templates": ["https://shadow-search.example/search?q={query}"],
                        "source_site_label": "Anna's Archive / LibGen",
                        "requires_manual_confirm": True,
                    },
                },
            },
        )

        wanted = {"series_title": "Example Book", "language": "en", "media_type": "manga", "publisher": "Shueisha"}
        source_jobs = jobs.source_jobs_from_settings_snapshot(snapshot(db_path), wanted, include_operator=True, limit=1)
        jobs_by_id = by_id(source_jobs)
        assert_true("private_trackers" not in jobs_by_id, "disabled boundary sources are excluded by default")

        standard = jobs_by_id["standard_ebooks"]
        assert_equal(standard["job_status"], "ready", "Standard Ebooks HTTP job is ready")
        assert_equal(standard["fetch_plan"]["requests"][0]["url"], "https://standardebooks.org/feeds/opds", "Standard Ebooks request URL")
        assert_true(standard["emits_download_task"], "Standard Ebooks may emit a task after safe verdict")

        gutendex = jobs_by_id["gutendex"]
        assert_equal(gutendex["job_status"], "ready", "Gutendex HTTP job is ready")
        assert_equal(gutendex["fetch_plan"]["requests"][0]["params"]["search"], "Example Book", "Gutendex query is wanted title")

        archive = jobs_by_id["internet_archive"]
        assert_equal(archive["job_status"], "ready", "Internet Archive HTTP job is ready")
        assert_equal(archive["fetch_plan"]["payload_mode"], "archive_search_then_metadata", "IA job expands search into metadata")

        mangadex = jobs_by_id["mangadex"]
        assert_equal(mangadex["job_status"], "ready", "MangaDex API job is ready")
        assert_equal(mangadex["fetch_plan"]["payload_mode"], "mangadex_search_then_feed", "MangaDex job expands search into feed")
        assert_true(mangadex["emits_download_task"], "MangaDex At-Home API job can emit a page-pack task")
        assert_equal(mangadex["fetch_plan"]["requests"][0]["url"], "https://api.mangadex.org/manga", "MangaDex job uses API search URL")

        suwayomi = jobs_by_id["suwayomi"]
        assert_equal(suwayomi["job_status"], "ready", "Suwayomi API job is ready")
        assert_equal(suwayomi["fetch_plan"]["payload_mode"], "suwayomi_search_then_chapters", "Suwayomi job expands sources into chapters")
        assert_true(suwayomi["emits_download_task"], "Suwayomi API job can emit a page-pack task")
        assert_equal(suwayomi["fetch_plan"]["requests"][0]["url"], "http://127.0.0.1:4568/api/v1/source/list", "Suwayomi job starts with source list")
        assert_equal(len(suwayomi["fetch_plan"]["requests"]), 2, "Suwayomi static plan keeps source and extension requests")
        assert_equal(suwayomi["fetch_plan"]["estimated_request_count"], 16, "Suwayomi job carries bounded expanded request estimate")

        seed_suwayomi_source_error_memory(db_path, now=123456.0)
        suwayomi_cooldown_row = dict(suwayomi["registry_row"])
        suwayomi_cooldown_row["policy"] = dict(suwayomi_cooldown_row.get("policy") or {})
        suwayomi_cooldown_row["policy"].update(
            {
                "suwayomi_source_names": ["MangaFire", "MangaDex"],
                "suwayomi_max_source_count": 2,
                "suwayomi_max_query_variants": 1,
                "suwayomi_source_error_cooldown_enabled": True,
                "suwayomi_source_error_cooldown_seconds": 3600,
                "suwayomi_source_error_cooldown_threshold": 2,
            }
        )
        suwayomi_cooldown_job = jobs.source_job_for_row(
            suwayomi_cooldown_row,
            suwayomi["worker_plan"],
            wanted_item={"series_title": "Fairy Tail", "issue_number": "35", "language": "en", "media_type": "manga", "publisher": "Kodansha"},
            limit=3,
        )
        suwayomi_cooldown_result = jobs.run_source_job(
            suwayomi_cooldown_job,
            http_get=suwayomi_persisted_source_error_cooldown_http_get,
            source_memory_db_path=db_path,
            now=123456.0,
        )
        assert_equal(suwayomi_cooldown_result["result_status"], "searched_no_candidates", "Suwayomi persisted source cooldown keeps healthy source search moving")
        suwayomi_cooldown_fetch = suwayomi_cooldown_result["attempts"][0]["raw"]["fetch"]
        assert_equal(
            suwayomi_cooldown_fetch["estimated_request_count"],
            13,
            "Suwayomi fetch evidence preserves the bounded request estimate",
        )
        assert_equal(
            suwayomi_cooldown_result["attempts"][0]["raw"]["source_worker"]["request_count"],
            13,
            "Suwayomi source-worker evidence uses estimated request count",
        )
        assert_equal(
            suwayomi_cooldown_fetch["suwayomi_source_error_cooldown"]["source_count"],
            1,
            "Suwayomi persisted source cooldown records cooled source count",
        )
        assert_equal(
            suwayomi_cooldown_fetch["suwayomi_source_error_cooldown"]["sources"][0]["source_id"],
            "2131019126180322627",
            "Suwayomi persisted source cooldown records cooled source id",
        )
        suwayomi_search_requests = [
            request
            for request in suwayomi_cooldown_fetch["requests"]
            if str(request.get("request_id") or "").startswith("suwayomi_source_search")
        ]
        assert_equal(len(suwayomi_search_requests), 1, "Suwayomi persisted source cooldown searches only the healthy source")
        assert_true(
            any(
                row.get("source_id") == "2131019126180322627" and row.get("reason") == "source_error_cooldown"
                for row in suwayomi_cooldown_fetch["suwayomi_source_selection"]["skipped_sources"]
            ),
            "Suwayomi persisted source cooldown records selection skip reason",
        )

        chapter_lookup_db = Path(tmp) / "inkdrop-source-chapter-lookup-cooldown.sqlite3"
        with inkdrop_state.connect(chapter_lookup_db) as con:
            inkdrop_state.init_schema(con)
        seed_suwayomi_source_error_memory(
            chapter_lookup_db,
            now=123456.0,
            count=2,
            source_id="2499283573021220255",
            source_display_name="MangaDex (EN)",
            source_error="graphql_errors",
            stage="manga_chapters_no_meta_fallback",
            reason="suwayomi_chapter_lookup_failed",
            queue_prefix="queue-suwayomi-chapter-lookup",
            wanted_prefix="wanted-suwayomi-chapter-lookup",
            issue_prefix="issue-suwayomi-chapter-lookup",
            attempt_prefix="source-attempt-suwayomi-chapter-lookup",
        )
        suwayomi_chapter_lookup_row = dict(suwayomi["registry_row"])
        suwayomi_chapter_lookup_row["policy"] = dict(suwayomi_chapter_lookup_row.get("policy") or {})
        suwayomi_chapter_lookup_row["policy"].update(
            {
                "suwayomi_source_names": ["MangaDex", "Weeb Central"],
                "suwayomi_max_source_count": 2,
                "suwayomi_max_query_variants": 1,
                "suwayomi_source_error_cooldown_enabled": True,
                "suwayomi_source_error_cooldown_seconds": 3600,
                "suwayomi_source_error_cooldown_threshold": 2,
            }
        )
        suwayomi_chapter_lookup_job = jobs.source_job_for_row(
            suwayomi_chapter_lookup_row,
            suwayomi["worker_plan"],
            wanted_item={"series_title": "Frieren: Beyond Journey's End", "volume_number": "14", "unit_type": "volume", "language": "en", "media_type": "manga", "publisher": "Viz"},
            limit=3,
        )
        suwayomi_chapter_lookup_result = jobs.run_source_job(
            suwayomi_chapter_lookup_job,
            http_get=suwayomi_volume_gap_cooldown_http_get,
            source_memory_db_path=chapter_lookup_db,
            now=123456.0,
        )
        assert_equal(
            suwayomi_chapter_lookup_result["result_status"],
            "searched_no_candidates",
            "Suwayomi chapter lookup source cooldown keeps alternate source automation moving",
        )
        suwayomi_chapter_lookup_fetch = suwayomi_chapter_lookup_result["attempts"][0]["raw"]["fetch"]
        assert_equal(
            suwayomi_chapter_lookup_fetch["suwayomi_source_error_cooldown"]["source_count"],
            1,
            "Suwayomi chapter lookup cooldown records one cooled source",
        )
        assert_equal(
            suwayomi_chapter_lookup_fetch["suwayomi_source_error_cooldown"]["sources"][0]["manga_chapter_lookup_error_count"],
            2,
            "Suwayomi chapter lookup cooldown counts repeated manga lookup failures",
        )
        assert_true(
            any(
                row.get("source_id") == "2499283573021220255" and row.get("reason") == "source_error_cooldown"
                for row in suwayomi_chapter_lookup_fetch["suwayomi_source_selection"]["skipped_sources"]
            ),
            "Suwayomi chapter lookup cooldown records selection skip reason",
        )
        suwayomi_chapter_lookup_search_requests = [
            request
            for request in suwayomi_chapter_lookup_fetch["requests"]
            if str(request.get("request_id") or "").startswith("suwayomi_source_search")
        ]
        assert_equal(len(suwayomi_chapter_lookup_search_requests), 1, "Suwayomi chapter lookup cooldown searches only the alternate source")
        assert_equal(
            suwayomi_chapter_lookup_search_requests[0].get("source_display_name"),
            "Weeb Central (EN)",
            "Suwayomi chapter lookup cooldown preserves configured alternate source identity",
        )

        suwayomi_all_cooled_row = dict(suwayomi["registry_row"])
        suwayomi_all_cooled_row["policy"] = dict(suwayomi_all_cooled_row.get("policy") or {})
        suwayomi_all_cooled_row["policy"].update(
            {
                "suwayomi_source_names": ["MangaFire"],
                "suwayomi_max_source_count": 1,
                "suwayomi_max_query_variants": 1,
                "suwayomi_source_error_cooldown_enabled": True,
                "suwayomi_source_error_cooldown_seconds": 3600,
                "suwayomi_source_error_cooldown_threshold": 2,
            }
        )
        suwayomi_all_cooled_job = jobs.source_job_for_row(
            suwayomi_all_cooled_row,
            suwayomi["worker_plan"],
            wanted_item={"series_title": "Fairy Tail", "issue_number": "31", "language": "en", "media_type": "manga", "publisher": "Kodansha"},
            limit=3,
        )
        suwayomi_all_cooled_result = jobs.run_source_job(
            suwayomi_all_cooled_job,
            http_get=suwayomi_persisted_source_error_cooldown_http_get,
            source_memory_db_path=db_path,
            now=123456.0,
        )
        assert_equal(
            suwayomi_all_cooled_result["result_status"],
            "provider_wait",
            "Suwayomi all-cooled configured source pool records provider wait instead of no-candidate",
        )
        assert_equal(
            suwayomi_all_cooled_result["reason"],
            "suwayomi_source_pool_cooldown",
            "Suwayomi all-cooled source pool records a precise provider-wait reason",
        )
        suwayomi_all_cooled_fetch = suwayomi_all_cooled_result["attempts"][0]["raw"]["fetch"]
        assert_equal(
            suwayomi_all_cooled_fetch["suwayomi_source_selection"]["selected_count"],
            0,
            "Suwayomi all-cooled source pool evidence records zero selected sources",
        )
        assert_true(
            any(
                row.get("source_id") == "2131019126180322627" and row.get("reason") == "source_error_cooldown"
                for row in suwayomi_all_cooled_fetch["suwayomi_source_selection"]["skipped_sources"]
            ),
            "Suwayomi all-cooled source pool evidence records the cooled source",
        )

        suwayomi_probe_row = dict(suwayomi["registry_row"])
        suwayomi_probe_row["policy"] = dict(suwayomi_probe_row.get("policy") or {})
        suwayomi_probe_row["policy"].update(
            {
                "suwayomi_source_names": ["MangaFire"],
                "suwayomi_max_source_count": 1,
                "suwayomi_max_query_variants": 1,
                "suwayomi_source_error_cooldown_enabled": True,
                "suwayomi_source_error_cooldown_seconds": 3600,
                "suwayomi_source_error_cooldown_threshold": 2,
                "suwayomi_source_error_cooldown_probe_enabled": True,
                "suwayomi_source_error_cooldown_probe_after_seconds": 60,
            }
        )
        suwayomi_probe_job = jobs.source_job_for_row(
            suwayomi_probe_row,
            suwayomi["worker_plan"],
            wanted_item={"series_title": "Fairy Tail", "issue_number": "31", "language": "en", "media_type": "manga", "publisher": "Kodansha"},
            limit=3,
        )
        suwayomi_probe_result = jobs.run_source_job(
            suwayomi_probe_job,
            http_get=suwayomi_persisted_source_error_probe_http_get,
            source_memory_db_path=db_path,
            now=123456.0,
        )
        assert_equal(
            suwayomi_probe_result["result_status"],
            "searched_no_candidates",
            "Suwayomi all-cooled probe searches one aged cooldown source instead of staying visually stuck",
        )
        suwayomi_probe_fetch = suwayomi_probe_result["attempts"][0]["raw"]["fetch"]
        assert_equal(
            suwayomi_probe_fetch["suwayomi_source_error_cooldown"]["probe_source_count"],
            1,
            "Suwayomi cooldown evidence records one probe source",
        )
        assert_equal(
            suwayomi_probe_fetch["suwayomi_source_selection"]["selected_count"],
            1,
            "Suwayomi cooldown probe selects exactly one source",
        )
        assert_equal(
            suwayomi_probe_fetch["suwayomi_source_selection"]["cooldown_probe_count"],
            1,
            "Suwayomi source selection labels cooldown probe selection",
        )
        suwayomi_probe_search_requests = [
            request
            for request in suwayomi_probe_fetch["requests"]
            if str(request.get("request_id") or "").startswith("suwayomi_source_search")
        ]
        assert_equal(len(suwayomi_probe_search_requests), 1, "Suwayomi cooldown probe makes one source search request")
        assert_true(
            suwayomi_probe_search_requests[0].get("source_error_cooldown_probe"),
            "Suwayomi cooldown probe request carries explicit evidence",
        )

        seed_suwayomi_source_error_memory(
            db_path,
            now=123456.0,
            count=2,
            age_seconds=120,
            source_id="2131019126180322627",
            source_display_name="MangaFire (EN)",
            source_error="simulated MangaFire aged source search failure",
            queue_prefix="queue-suwayomi-source-multi-probe-fire",
            wanted_prefix="wanted-suwayomi-source-multi-probe-fire",
            issue_prefix="issue-suwayomi-source-multi-probe-fire",
            attempt_prefix="source-attempt-suwayomi-source-multi-probe-fire",
        )
        seed_suwayomi_source_error_memory(
            db_path,
            now=123456.0,
            count=2,
            age_seconds=120,
            source_id="2499283573021220255",
            source_display_name="MangaDex (EN)",
            source_error="simulated MangaDex aged source search failure",
            queue_prefix="queue-suwayomi-source-multi-probe-dex",
            wanted_prefix="wanted-suwayomi-source-multi-probe-dex",
            issue_prefix="issue-suwayomi-source-multi-probe-dex",
            attempt_prefix="source-attempt-suwayomi-source-multi-probe-dex",
        )
        suwayomi_multi_probe_row = dict(suwayomi["registry_row"])
        suwayomi_multi_probe_row["policy"] = dict(suwayomi_multi_probe_row.get("policy") or {})
        suwayomi_multi_probe_row["policy"].update(
            {
                "suwayomi_source_names": ["MangaFire", "MangaDex"],
                "suwayomi_max_source_count": 2,
                "suwayomi_max_query_variants": 1,
                "suwayomi_source_error_cooldown_enabled": True,
                "suwayomi_source_error_cooldown_seconds": 3600,
                "suwayomi_source_error_cooldown_threshold": 2,
                "suwayomi_source_error_cooldown_probe_enabled": True,
                "suwayomi_source_error_cooldown_probe_after_seconds": 60,
                "suwayomi_source_error_cooldown_probe_max_sources": 2,
            }
        )
        suwayomi_multi_probe_job = jobs.source_job_for_row(
            suwayomi_multi_probe_row,
            suwayomi["worker_plan"],
            wanted_item={"series_title": "Fairy Tail", "issue_number": "31", "language": "en", "media_type": "manga", "publisher": "Kodansha"},
            limit=3,
        )
        suwayomi_multi_probe_result = jobs.run_source_job(
            suwayomi_multi_probe_job,
            http_get=suwayomi_persisted_source_error_multi_probe_http_get,
            source_memory_db_path=db_path,
            now=123456.0,
        )
        assert_equal(
            suwayomi_multi_probe_result["result_status"],
            "searched_no_candidates",
            "Suwayomi multi-source cooldown probe remains automatic when probes return no manga",
        )
        suwayomi_multi_probe_fetch = suwayomi_multi_probe_result["attempts"][0]["raw"]["fetch"]
        assert_equal(
            suwayomi_multi_probe_fetch["suwayomi_source_error_cooldown"]["probe_max_sources"],
            2,
            "Suwayomi cooldown evidence records the configured multi-source probe cap",
        )
        assert_equal(
            suwayomi_multi_probe_fetch["suwayomi_source_error_cooldown"]["probe_source_count"],
            2,
            "Suwayomi cooldown evidence records two probe sources",
        )
        assert_equal(
            suwayomi_multi_probe_fetch["suwayomi_source_selection"]["selected_count"],
            2,
            "Suwayomi all-cooled fallback selects two cooled sources when configured",
        )
        assert_equal(
            suwayomi_multi_probe_fetch["suwayomi_source_selection"]["cooldown_probe_count"],
            2,
            "Suwayomi source selection labels both cooldown probes",
        )
        suwayomi_multi_probe_search_requests = [
            request
            for request in suwayomi_multi_probe_fetch["requests"]
            if str(request.get("request_id") or "").startswith("suwayomi_source_search")
        ]
        assert_equal(len(suwayomi_multi_probe_search_requests), 2, "Suwayomi multi-source cooldown probe makes two source search requests")
        assert_equal(
            sorted(request.get("source_display_name") for request in suwayomi_multi_probe_search_requests),
            ["MangaDex (EN)", "MangaFire (EN)"],
            "Suwayomi multi-source cooldown probe tries the configured cooled sources",
        )
        assert_true(
            all(request.get("source_error_cooldown_probe") for request in suwayomi_multi_probe_search_requests),
            "Suwayomi multi-source cooldown probe requests carry explicit evidence",
        )

        rotating_memory_db = Path(tmp) / "inkdrop-source-rotating-probe.sqlite3"
        with inkdrop_state.connect(rotating_memory_db) as con:
            inkdrop_state.init_schema(con)
        for source_id, source_name, source_error, prefix in (
            (
                "2499283573021220255",
                "MangaDex (EN)",
                "simulated MangaDex aged source search failure",
                "dex",
            ),
            (
                "569821715369244319",
                "ComicK Fanmade (EN)",
                "simulated ComicK aged source search failure",
                "comick",
            ),
            (
                "2131019126180322627",
                "Weeb Central (EN)",
                "simulated Weeb Central aged source search failure",
                "weeb",
            ),
        ):
            seed_suwayomi_source_error_memory(
                rotating_memory_db,
                now=123456.0,
                count=2,
                age_seconds=120,
                source_id=source_id,
                source_display_name=source_name,
                source_error=source_error,
                queue_prefix=f"queue-suwayomi-rotating-probe-{prefix}",
                wanted_prefix=f"wanted-suwayomi-rotating-probe-{prefix}",
                issue_prefix=f"issue-suwayomi-rotating-probe-{prefix}",
                attempt_prefix=f"source-attempt-suwayomi-rotating-probe-{prefix}",
            )
        suwayomi_rotating_probe_row = dict(suwayomi["registry_row"])
        suwayomi_rotating_probe_row["policy"] = dict(suwayomi_rotating_probe_row.get("policy") or {})
        suwayomi_rotating_probe_row["policy"].update(
            {
                "suwayomi_source_names": ["MangaDex", "ComicK Fanmade", "Weeb Central"],
                "suwayomi_max_source_count": 3,
                "suwayomi_max_query_variants": 1,
                "suwayomi_source_error_cooldown_enabled": True,
                "suwayomi_source_error_cooldown_seconds": 3600,
                "suwayomi_source_error_cooldown_threshold": 2,
                "suwayomi_source_error_cooldown_probe_enabled": True,
                "suwayomi_source_error_cooldown_probe_after_seconds": 60,
                "suwayomi_source_error_cooldown_probe_max_sources": 2,
            }
        )
        rotating_probe_sets = []
        for series_title, issue_number in (
            ("Fairy Tail", "31"),
            ("Gachiakuta", "7"),
            ("Oyasumi Punpun", "8"),
            ("20th Century Boys", "3"),
            ("Bleach", "12"),
        ):
            rotating_probe_job = jobs.source_job_for_row(
                suwayomi_rotating_probe_row,
                suwayomi["worker_plan"],
                wanted_item={
                    "series_title": series_title,
                    "issue_number": issue_number,
                    "language": "en",
                    "media_type": "manga",
                    "publisher": "Kodansha",
                },
                limit=3,
            )
            rotating_probe_result = jobs.run_source_job(
                rotating_probe_job,
                http_get=suwayomi_persisted_source_error_rotating_probe_http_get,
                source_memory_db_path=rotating_memory_db,
                now=123456.0,
            )
            assert_equal(
                rotating_probe_result["result_status"],
                "searched_no_candidates",
                "Suwayomi rotating cooldown probe remains automatic when probes return no manga",
            )
            rotating_probe_fetch = rotating_probe_result["attempts"][0]["raw"]["fetch"]
            assert_equal(
                rotating_probe_fetch["suwayomi_source_error_cooldown"]["probe_source_count"],
                2,
                "Suwayomi rotating cooldown evidence keeps the configured probe cap",
            )
            assert_true(
                rotating_probe_fetch["suwayomi_source_error_cooldown"].get("probe_rotation_hash"),
                "Suwayomi rotating cooldown evidence records the rotation fingerprint",
            )
            rotating_probe_search_requests = [
                request
                for request in rotating_probe_fetch["requests"]
                if str(request.get("request_id") or "").startswith("suwayomi_source_search")
            ]
            assert_equal(len(rotating_probe_search_requests), 2, "Suwayomi rotating cooldown probe makes two source search requests")
            rotating_probe_sets.append(tuple(sorted(request.get("source_display_name") for request in rotating_probe_search_requests)))
        assert_true(
            len(set(rotating_probe_sets)) > 1,
            "Suwayomi rotating cooldown probe does not pin every wanted row to the same cooled sources",
        )
        assert_true(
            any("Weeb Central (EN)" in source_set for source_set in rotating_probe_sets),
            "Suwayomi rotating cooldown probe eventually samples the third cooled source",
        )

        seed_suwayomi_source_error_memory(
            db_path,
            now=123456.0,
            count=4,
            age_seconds=7200,
            queue_prefix="queue-suwayomi-source-quarantine",
            wanted_prefix="wanted-suwayomi-source-quarantine",
            issue_prefix="issue-suwayomi-source-quarantine",
            attempt_prefix="source-attempt-suwayomi-source-quarantine",
        )
        suwayomi_quarantine_row = dict(suwayomi["registry_row"])
        suwayomi_quarantine_row["policy"] = dict(suwayomi_quarantine_row.get("policy") or {})
        suwayomi_quarantine_row["policy"].update(
            {
                "suwayomi_source_names": ["MangaFire", "MangaDex"],
                "suwayomi_max_source_count": 2,
                "suwayomi_max_query_variants": 1,
                "suwayomi_source_error_cooldown_enabled": True,
                "suwayomi_source_error_cooldown_seconds": 60,
                "suwayomi_source_error_cooldown_threshold": 3,
                "suwayomi_source_error_quarantine_enabled": True,
                "suwayomi_source_error_quarantine_seconds": 86400,
                "suwayomi_source_error_quarantine_threshold": 4,
            }
        )
        suwayomi_quarantine_job = jobs.source_job_for_row(
            suwayomi_quarantine_row,
            suwayomi["worker_plan"],
            wanted_item={"series_title": "Fairy Tail", "issue_number": "35", "language": "en", "media_type": "manga", "publisher": "Kodansha"},
            limit=3,
        )
        suwayomi_quarantine_result = jobs.run_source_job(
            suwayomi_quarantine_job,
            http_get=suwayomi_persisted_source_error_cooldown_http_get,
            source_memory_db_path=db_path,
            now=123456.0,
        )
        assert_equal(suwayomi_quarantine_result["result_status"], "searched_no_candidates", "Suwayomi persistent source quarantine keeps healthy source search moving")
        suwayomi_quarantine_fetch = suwayomi_quarantine_result["attempts"][0]["raw"]["fetch"]
        assert_equal(
            suwayomi_quarantine_fetch["suwayomi_source_error_cooldown"]["quarantine_source_count"],
            1,
            "Suwayomi persistent source quarantine records quarantined source count",
        )
        assert_equal(
            suwayomi_quarantine_fetch["suwayomi_source_error_cooldown"]["sources"][0]["cooldown_kind"],
            "persistent_source_error_quarantine",
            "Suwayomi persistent source quarantine labels the cooled source",
        )
        assert_equal(
            suwayomi_quarantine_fetch["suwayomi_source_error_cooldown"]["sources"][0]["source_id"],
            "2131019126180322627",
            "Suwayomi persistent source quarantine records source id",
        )
        suwayomi_quarantine_search_requests = [
            request
            for request in suwayomi_quarantine_fetch["requests"]
            if str(request.get("request_id") or "").startswith("suwayomi_source_search")
        ]
        assert_equal(len(suwayomi_quarantine_search_requests), 1, "Suwayomi persistent source quarantine searches only the healthy source")
        assert_true(
            any(
                row.get("source_id") == "2131019126180322627" and row.get("reason") == "source_error_cooldown"
                for row in suwayomi_quarantine_fetch["suwayomi_source_selection"]["skipped_sources"]
            ),
            "Suwayomi persistent source quarantine records selection skip reason",
        )

        volume_gap_db = Path(tmp) / "inkdrop-source-volume-gap.sqlite3"
        with inkdrop_state.connect(volume_gap_db) as con:
            inkdrop_state.init_schema(con)
        seed_suwayomi_volume_gap_memory(volume_gap_db, now=123456.0)
        suwayomi_volume_gap_row = dict(suwayomi["registry_row"])
        suwayomi_volume_gap_row["policy"] = dict(suwayomi_volume_gap_row.get("policy") or {})
        suwayomi_volume_gap_row["policy"].update(
            {
                "suwayomi_source_names": ["MangaDex", "Weeb Central"],
                "suwayomi_max_source_count": 2,
                "suwayomi_max_query_variants": 1,
                "suwayomi_source_error_cooldown_enabled": False,
                "suwayomi_volume_gap_cooldown_enabled": True,
                "suwayomi_volume_gap_cooldown_seconds": 3600,
                "suwayomi_volume_gap_cooldown_threshold": 2,
                "suwayomi_volume_gap_cooldown_max_sources": 2,
            }
        )
        suwayomi_volume_gap_job = jobs.source_job_for_row(
            suwayomi_volume_gap_row,
            suwayomi["worker_plan"],
            wanted_item={
                "series_id": "series-suwayomi-volume-gap",
                "series_title": "Fairy Tail",
                "volume_number": "31",
                "unit_type": "volume",
                "language": "en",
                "media_type": "manga",
                "publisher": "Kodansha",
            },
            limit=3,
        )
        suwayomi_volume_gap_result = jobs.run_source_job(
            suwayomi_volume_gap_job,
            http_get=suwayomi_volume_gap_cooldown_http_get,
            source_memory_db_path=volume_gap_db,
            now=123456.0,
        )
        assert_equal(
            suwayomi_volume_gap_result["result_status"],
            "searched_no_candidates",
            "Suwayomi volume-gap cooldown keeps fallback source search moving",
        )
        suwayomi_volume_gap_fetch = suwayomi_volume_gap_result["attempts"][0]["raw"]["fetch"]
        assert_equal(
            suwayomi_volume_gap_fetch["suwayomi_source_error_cooldown"]["volume_gap_source_count"],
            1,
            "Suwayomi volume-gap cooldown records cooled source count",
        )
        assert_equal(
            suwayomi_volume_gap_fetch["suwayomi_source_error_cooldown"]["sources"][0]["cooldown_kind"],
            "volume_evidence_gap_cooldown",
            "Suwayomi volume-gap cooldown labels the cooled source",
        )
        assert_true(
            any(
                row.get("source_id") == "2499283573021220255" and row.get("reason") == "volume_evidence_gap_cooldown"
                for row in suwayomi_volume_gap_fetch["suwayomi_source_selection"]["skipped_sources"]
            ),
            "Suwayomi volume-gap cooldown records selection skip reason",
        )
        suwayomi_volume_gap_search_requests = [
            request
            for request in suwayomi_volume_gap_fetch["requests"]
            if str(request.get("request_id") or "").startswith("suwayomi_source_search")
        ]
        assert_equal(len(suwayomi_volume_gap_search_requests), 1, "Suwayomi volume-gap cooldown searches only the fallback source")
        assert_equal(
            suwayomi_volume_gap_search_requests[0].get("source_display_name"),
            "Weeb Central (EN)",
            "Suwayomi volume-gap cooldown preserves same-series fallback source automation",
        )

        suwayomi_volume_gap_all_cooled_row = dict(suwayomi["registry_row"])
        suwayomi_volume_gap_all_cooled_row["policy"] = dict(suwayomi_volume_gap_all_cooled_row.get("policy") or {})
        suwayomi_volume_gap_all_cooled_row["policy"].update(
            {
                "suwayomi_source_names": ["MangaDex"],
                "suwayomi_max_source_count": 1,
                "suwayomi_max_query_variants": 1,
                "suwayomi_source_error_cooldown_enabled": False,
                "suwayomi_volume_gap_cooldown_enabled": True,
                "suwayomi_volume_gap_cooldown_seconds": 3600,
                "suwayomi_volume_gap_cooldown_threshold": 2,
                "suwayomi_volume_gap_cooldown_max_sources": 1,
                "suwayomi_source_error_cooldown_probe_enabled": False,
            }
        )
        suwayomi_volume_gap_all_cooled_job = jobs.source_job_for_row(
            suwayomi_volume_gap_all_cooled_row,
            suwayomi["worker_plan"],
            wanted_item={
                "series_id": "series-suwayomi-volume-gap",
                "series_title": "Fairy Tail",
                "volume_number": "31",
                "unit_type": "volume",
                "language": "en",
                "media_type": "manga",
                "publisher": "Kodansha",
            },
            limit=3,
        )
        suwayomi_volume_gap_all_cooled_result = jobs.run_source_job(
            suwayomi_volume_gap_all_cooled_job,
            http_get=suwayomi_volume_gap_probe_http_get,
            source_memory_db_path=volume_gap_db,
            now=123456.0,
        )
        assert_equal(
            suwayomi_volume_gap_all_cooled_result["result_status"],
            "provider_wait",
            "Suwayomi volume-gap all-cooled source pool records provider wait",
        )
        assert_equal(
            suwayomi_volume_gap_all_cooled_result["reason"],
            "suwayomi_source_pool_cooldown",
            "Suwayomi volume-gap all-cooled source pool records a precise provider-wait reason",
        )
        suwayomi_volume_gap_all_cooled_fetch = suwayomi_volume_gap_all_cooled_result["attempts"][0]["raw"]["fetch"]
        assert_equal(
            suwayomi_volume_gap_all_cooled_fetch["suwayomi_source_selection"]["selected_count"],
            0,
            "Suwayomi volume-gap all-cooled source pool evidence records zero selected sources",
        )
        assert_true(
            any(
                row.get("source_id") == "2499283573021220255" and row.get("reason") == "volume_evidence_gap_cooldown"
                for row in suwayomi_volume_gap_all_cooled_fetch["suwayomi_source_selection"]["skipped_sources"]
            ),
            "Suwayomi volume-gap all-cooled source pool evidence records the cooled source",
        )

        volume_metadata_gap_db = Path(tmp) / "inkdrop-source-volume-metadata-gap.sqlite3"
        with inkdrop_state.connect(volume_metadata_gap_db) as con:
            inkdrop_state.init_schema(con)
        seed_suwayomi_volume_gap_memory(volume_metadata_gap_db, now=123456.0, count=1)
        suwayomi_volume_metadata_gap_row = dict(suwayomi["registry_row"])
        suwayomi_volume_metadata_gap_row["policy"] = dict(suwayomi_volume_metadata_gap_row.get("policy") or {})
        suwayomi_volume_metadata_gap_row["policy"].update(
            {
                "suwayomi_source_names": ["MangaDex", "Weeb Central"],
                "suwayomi_max_source_count": 2,
                "suwayomi_max_query_variants": 1,
                "suwayomi_source_error_cooldown_enabled": False,
                "suwayomi_volume_gap_cooldown_enabled": True,
                "suwayomi_volume_gap_cooldown_seconds": 3600,
                "suwayomi_volume_gap_cooldown_threshold": 2,
                "suwayomi_volume_gap_cooldown_max_sources": 2,
            }
        )
        suwayomi_volume_metadata_gap_job = jobs.source_job_for_row(
            suwayomi_volume_metadata_gap_row,
            suwayomi["worker_plan"],
            wanted_item={
                "series_id": "series-suwayomi-volume-gap",
                "series_title": "Fairy Tail",
                "volume_number": "31",
                "unit_type": "volume",
                "language": "en",
                "media_type": "manga",
                "publisher": "Kodansha",
            },
            limit=3,
        )
        suwayomi_volume_metadata_gap_result = jobs.run_source_job(
            suwayomi_volume_metadata_gap_job,
            http_get=suwayomi_volume_gap_cooldown_http_get,
            source_memory_db_path=volume_metadata_gap_db,
            now=123456.0,
        )
        suwayomi_volume_metadata_gap_fetch = suwayomi_volume_metadata_gap_result["attempts"][0]["raw"]["fetch"]
        assert_equal(
            suwayomi_volume_metadata_gap_fetch["suwayomi_source_error_cooldown"]["volume_gap_source_count"],
            1,
            "Suwayomi cools a source after one same-series volume metadata gap proof",
        )
        assert_equal(
            suwayomi_volume_metadata_gap_fetch["suwayomi_source_error_cooldown"]["volume_metadata_gap_threshold"],
            1,
            "Suwayomi records the stricter metadata-gap cooldown threshold",
        )
        assert_true(
            any(
                row.get("source_id") == "2499283573021220255" and row.get("reason") == "volume_evidence_gap_cooldown"
                for row in suwayomi_volume_metadata_gap_fetch["suwayomi_source_selection"]["skipped_sources"]
            ),
            "Suwayomi one-proof metadata gap cooldown records selection skip reason",
        )

        volume_gap_probe_db = Path(tmp) / "inkdrop-source-volume-gap-probe.sqlite3"
        with inkdrop_state.connect(volume_gap_probe_db) as con:
            inkdrop_state.init_schema(con)
        seed_suwayomi_volume_gap_memory(volume_gap_probe_db, now=123456.0)
        suwayomi_volume_gap_probe_row = dict(suwayomi["registry_row"])
        suwayomi_volume_gap_probe_row["policy"] = dict(suwayomi_volume_gap_probe_row.get("policy") or {})
        suwayomi_volume_gap_probe_row["policy"].update(
            {
                "suwayomi_source_names": ["MangaDex"],
                "suwayomi_max_source_count": 1,
                "suwayomi_max_query_variants": 1,
                "suwayomi_source_error_cooldown_enabled": False,
                "suwayomi_volume_gap_cooldown_enabled": True,
                "suwayomi_volume_gap_cooldown_seconds": 3600,
                "suwayomi_volume_gap_cooldown_threshold": 2,
                "suwayomi_volume_gap_cooldown_max_sources": 1,
                "suwayomi_source_error_cooldown_probe_enabled": True,
                "suwayomi_source_error_cooldown_probe_after_seconds": 1800,
                "suwayomi_source_error_cooldown_probe_max_sources": 1,
                "suwayomi_volume_gap_cooldown_probe_enabled": True,
                "suwayomi_volume_gap_cooldown_probe_after_seconds": 60,
                "suwayomi_volume_gap_cooldown_probe_max_sources": 1,
            }
        )
        suwayomi_volume_gap_probe_job = jobs.source_job_for_row(
            suwayomi_volume_gap_probe_row,
            suwayomi["worker_plan"],
            wanted_item={
                "series_id": "series-suwayomi-volume-gap",
                "series_title": "Fairy Tail",
                "volume_number": "31",
                "unit_type": "volume",
                "language": "en",
                "media_type": "manga",
                "publisher": "Kodansha",
            },
            limit=3,
        )
        suwayomi_volume_gap_probe_result = jobs.run_source_job(
            suwayomi_volume_gap_probe_job,
            http_get=suwayomi_volume_gap_probe_http_get,
            source_memory_db_path=volume_gap_probe_db,
            now=123456.0,
        )
        assert_equal(
            suwayomi_volume_gap_probe_result["result_status"],
            "searched_no_candidates",
            "Suwayomi volume-gap cooldown probe remains bounded when every configured source is cooled",
        )
        suwayomi_volume_gap_probe_fetch = suwayomi_volume_gap_probe_result["attempts"][0]["raw"]["fetch"]
        assert_equal(
            suwayomi_volume_gap_probe_fetch["suwayomi_source_error_cooldown"]["volume_gap_probe_after_seconds"],
            60,
            "Suwayomi volume-gap cooldown probe uses the volume-gap probe age instead of source-error probe age",
        )
        assert_equal(
            suwayomi_volume_gap_probe_fetch["suwayomi_source_error_cooldown"]["volume_gap_probe_source_count"],
            1,
            "Suwayomi volume-gap cooldown probe records volume-gap probe count",
        )
        assert_equal(
            suwayomi_volume_gap_probe_fetch["suwayomi_source_selection"]["cooldown_probe_count"],
            1,
            "Suwayomi volume-gap cooldown probe selects one cooled source",
        )
        suwayomi_volume_gap_probe_search_requests = [
            request
            for request in suwayomi_volume_gap_probe_fetch["requests"]
            if str(request.get("request_id") or "").startswith("suwayomi_source_search")
        ]
        assert_equal(len(suwayomi_volume_gap_probe_search_requests), 1, "Suwayomi volume-gap cooldown probe searches one source")
        assert_true(
            suwayomi_volume_gap_probe_search_requests[0].get("source_error_cooldown_probe"),
            "Suwayomi volume-gap cooldown probe marks the bounded probe request",
        )
        assert_equal(
            suwayomi_volume_gap_probe_search_requests[0].get("source_id"),
            "2499283573021220255",
            "Suwayomi volume-gap cooldown probe preserves configured source identity",
        )

        auto_mangadex_row = dict(mangadex["registry_row"])
        auto_mangadex_row.update(
            {
                "source_mode": "auto",
                "registry_state": "ready",
                "auto_download_allowed": True,
                "requires_manual_review": False,
                "manual_review_allowed": False,
            }
        )
        auto_mangadex_row["policy"] = dict(auto_mangadex_row.get("policy") or {})
        auto_mangadex_row["policy"].update(
            {
                "fetch_at_home_pages": True,
                "allowed_image_extensions": [".jpg", ".jpeg", ".png", ".webp"],
                "max_at_home_chapters": 1,
                "requires_manual_confirm": False,
            }
        )
        auto_mangadex = jobs.source_job_for_row(
            auto_mangadex_row,
            wanted_item={"series_title": "Example Book", "issue_number": "1", "language": "en", "media_type": "manga", "publisher": "Shueisha"},
            limit=3,
        )
        assert_true(auto_mangadex["emits_download_task"], "MangaDex At-Home job can emit page-pack task after explicit auto policy")
        auto_mangadex_result = jobs.run_source_job(
            auto_mangadex,
            http_get=fake_http_get,
            staging_root="/tmp/inkdrop-staging",
            now=123456.0,
        )
        assert_equal(auto_mangadex_result["result_status"], "sent", "MangaDex At-Home job can send page-pack task")
        assert_equal(len(auto_mangadex_result["fetch"]["requests_made"]), 3, "MangaDex At-Home job fetches search, feed, and At-Home pages")
        assert_equal(auto_mangadex_result["attempts"][0]["download_client"], "inkdrop_page_pack", "MangaDex At-Home job uses page-pack downloader")
        assert_equal(auto_mangadex_result["attempts"][0]["page_count"], 3, "MangaDex At-Home job page count")

        nyaa = jobs_by_id["prowlarr_nyaa"]
        assert_equal(nyaa["job_status"], "ready", "Nyaa Prowlarr job is ready when enabled")
        assert_true(nyaa["emits_download_task"], "Nyaa can emit downloader tasks for manga after strict verdicts")
        assert_equal(nyaa["registry_row"]["registry_state"], "ready", "Nyaa registry state")
        assert_true(nyaa["registry_row"]["auto_download_allowed"], "Nyaa registry allows auto-download")
        assert_false(nyaa["registry_row"]["requires_manual_review"], "Nyaa registry does not require manual review")
        assert_equal(nyaa["registry_row"]["policy"]["indexer_ids"], ["6", "46"], "Nyaa targets the configured manga indexers")
        assert_equal(
            nyaa["registry_row"]["policy"]["categoryless_fallback_indexer_ids"],
            ["46"],
            "Nyaa categoryless fallback targets only the Literature indexer",
        )
        request = nyaa["fetch_plan"]["requests"][0]
        assert_equal(request["params"]["indexerIds"], ["6", "46"], "Nyaa request targets configured indexer IDs")
        assert_equal(request["params"]["categories"], ["7030"], "Nyaa request uses configured manga/comic category")
        assert_equal(request["headers"]["X-Api-Key"], "<secret_ref:prowlarr_api_key>", "Nyaa job uses a secret ref")
        fallback_request = nyaa["fetch_plan"]["categoryless_fallback_requests"][0]
        assert_equal(fallback_request["params"]["indexerIds"], "46", "Nyaa categoryless fallback targets only configured Literature indexer")
        assert_false("categories" in fallback_request["params"], "Nyaa categoryless fallback omits Prowlarr categories")
        categoryless_nyaa_row = dict(nyaa["registry_row"])
        categoryless_nyaa_row["policy"] = dict(nyaa["registry_row"]["policy"])
        categoryless_nyaa_row["policy"]["max_query_variants"] = 1
        categoryless_job = jobs.source_job_for_row(
            categoryless_nyaa_row,
            wanted_item={"series_title": "Example Book", "issue_number": "1", "language": "en", "media_type": "manga", "publisher": "Shueisha"},
            limit=3,
        )
        categoryless_result = jobs.run_source_job(
            categoryless_job,
            http_get=fake_http_get_nyaa_categoryless_fallback,
            staging_root="/tmp/inkdrop-staging",
            now=123456.0,
        )
        assert_equal(categoryless_result["result_status"], "sent", "Nyaa categoryless Literature fallback can send after strict verdicts")
        assert_equal(categoryless_result["safe_candidate_count"], 1, "Nyaa categoryless Literature fallback produces one safe candidate")
        categoryless_requests = categoryless_result["fetch"]["requests_made"]
        assert_equal(len(categoryless_requests), 2, "Nyaa categoryless fallback runs after one empty category-gated request")
        assert_equal(categoryless_requests[0]["params"]["categories"], ["7030"], "Nyaa categoryless fallback keeps first request category-gated")
        assert_equal(categoryless_requests[1]["params"]["indexerIds"], "46", "Nyaa categoryless fallback request targets Literature indexer")
        assert_false("categories" in categoryless_requests[1]["params"], "Nyaa categoryless fallback request omits categories")
        assert_equal(categoryless_result["attempts"][0]["indexer_id"], "46", "Nyaa categoryless fallback attempt preserves Literature indexer ID")

        tokyo = jobs_by_id["prowlarr_tokyo_toshokan_manga"]
        assert_equal(tokyo["job_status"], "ready", "Tokyo Toshokan manga Prowlarr job is ready when enabled")
        assert_true(tokyo["emits_download_task"], "Tokyo Toshokan manga can emit downloader tasks after strict verdicts")
        assert_equal(tokyo["registry_row"]["registry_state"], "ready", "Tokyo Toshokan manga registry state")
        assert_true(tokyo["registry_row"]["auto_download_allowed"], "Tokyo Toshokan manga registry allows auto-download")
        assert_false(tokyo["registry_row"]["requires_manual_review"], "Tokyo Toshokan manga registry does not require manual review")
        assert_equal(tokyo["registry_row"]["policy"]["indexer_ids"], ["45"], "Tokyo Toshokan manga targets the configured indexer")
        tokyo_request = tokyo["fetch_plan"]["requests"][0]
        assert_equal(tokyo_request["params"]["indexerIds"], "45", "Tokyo Toshokan manga request targets configured indexer ID")
        assert_equal(tokyo_request["params"]["categories"], ["7030", "8000", "8010"], "Tokyo Toshokan manga request uses configured categories")
        assert_equal(tokyo_request["headers"]["X-Api-Key"], "<secret_ref:prowlarr_api_key>", "Tokyo Toshokan manga job uses a secret ref")

        western_wanted = {"series_title": "Absolute Batman", "issue_number": "1", "language": "en", "media_type": "comic", "publisher": "DC Comics"}
        western_nyaa = jobs.source_job_for_row(nyaa["registry_row"], wanted_item=western_wanted, limit=3)
        assert_equal(western_nyaa["job_status"], "blocked", "Nyaa blocks western comic rows")
        assert_true("scoped to manga" in western_nyaa["reason"], "Nyaa block reason explains manga scope")
        western_tokyo = jobs.source_job_for_row(tokyo["registry_row"], wanted_item=western_wanted, limit=3)
        assert_equal(western_tokyo["job_status"], "blocked", "Tokyo Toshokan manga blocks western comic rows")
        assert_true("scoped to manga" in western_tokyo["reason"], "Tokyo Toshokan manga block reason explains manga scope")

        comic_wanted = {"series_title": "Example Book", "issue_number": "1", "language": "en", "media_type": "comic", "publisher": "DC Comics"}
        comic_jobs_by_id = by_id(jobs.source_jobs_from_settings_snapshot(snapshot(db_path), comic_wanted, include_operator=True, limit=1))
        tl_comics = comic_jobs_by_id["prowlarr_torrentleech_comics"]
        assert_equal(tl_comics["job_status"], "ready", "TorrentLeech Comics Prowlarr job is ready when enabled")
        assert_true(tl_comics["emits_download_task"], "TorrentLeech Comics can emit downloader tasks after strict verdicts")
        assert_equal(tl_comics["registry_row"]["registry_state"], "ready", "TorrentLeech Comics registry state")
        assert_equal(tl_comics["registry_row"]["policy"]["indexer_ids"], ["47"], "TorrentLeech Comics targets the comics clone")
        tl_request = tl_comics["fetch_plan"]["requests"][0]
        assert_equal(tl_request["params"]["indexerIds"], "47", "TorrentLeech Comics request targets the configured indexer id")
        assert_equal(
            tl_request["headers"]["X-Api-Key"],
            "<secret_ref:prowlarr_api_key>",
            "TorrentLeech Comics inherits parent Prowlarr secret",
        )
        manga_wanted = {"series_title": "Fairy Tail", "issue_number": "1", "language": "en", "media_type": "manga", "publisher": "Kodansha"}
        manga_tl = jobs.source_job_for_row(tl_comics["registry_row"], wanted_item=manga_wanted, limit=3)
        assert_equal(manga_tl["job_status"], "ready", "TorrentLeech Comics can search manga rows when the provider declares manga support")
        assert_true(manga_tl["emits_download_task"], "TorrentLeech Comics manga pack lane can emit downloader tasks after strict verdicts")
        assert_equal(
            manga_tl["fetch_plan"]["requests"][0]["params"]["indexerIds"],
            "47",
            "TorrentLeech Comics manga pack lane keeps the configured comics clone",
        )
        urasawa_wanted = {
            "series_title": "Naoki Urasawa's 20th Century Boys",
            "issue_number": "7",
            "language": "en",
            "media_type": "manga",
            "publisher": "Viz",
            "year": 2009,
        }
        urasawa_tl = jobs.source_job_for_row(tl_comics["registry_row"], wanted_item=urasawa_wanted, limit=3)
        urasawa_queries = [
            (request.get("params") or {}).get("query")
            for request in (urasawa_tl.get("fetch_plan") or {}).get("requests") or []
        ]
        assert_true("20th Century Boys" in urasawa_queries, "TorrentLeech pack lane keeps short series-only aliases for broad manga packs")
        comic_only_tl_row = dict(tl_comics["registry_row"])
        comic_only_tl_row["media_types"] = ["comic"]
        manga_comic_only_tl = jobs.source_job_for_row(comic_only_tl_row, wanted_item=manga_wanted, limit=3)
        assert_equal(manga_comic_only_tl["job_status"], "blocked", "comic-only pack providers still block manga rows")
        assert_true("scoped to comics" in manga_comic_only_tl["reason"], "comic-only pack block reason explains comic scope")
        aliased_snapshot = snapshot(db_path)
        aliased_snapshot["providers"].append(
            {
                "id": "torrentleech_inkdrop_comics_all",
                "provider_type": "download_source",
                "display_name": "Torrentleech Inkdrop Comics All",
                "enabled": True,
                "base_url": None,
                "secret_ref": None,
                "settings_group": "download_sources",
                "source": "state_memory",
                "settings": {},
            }
        )
        aliased_jobs_by_id = by_id(
            jobs.source_jobs_from_settings_snapshot(
                aliased_snapshot,
                comic_wanted,
                include_operator=True,
                limit=1,
            )
        )
        aliased_tl = aliased_jobs_by_id["prowlarr_torrentleech_comics"]
        assert_equal(
            aliased_tl["job_status"],
            "ready",
            "state-memory TorrentLeech alias does not shadow the settings-backed Prowlarr job",
        )
        assert_equal(
            aliased_tl["fetch_plan"]["requests"][0]["url"],
            "http://prowlarr.local/api/v1/search",
            "deduped TorrentLeech job keeps the parent Prowlarr base URL",
        )

        dognzb_comics = comic_jobs_by_id["prowlarr_dognzb_comics"]
        assert_equal(dognzb_comics["job_status"], "ready", "DOGnzb Comics Prowlarr job is ready when enabled")
        assert_true(dognzb_comics["emits_download_task"], "DOGnzb Comics can emit downloader tasks after strict verdicts")
        assert_equal(dognzb_comics["registry_row"]["registry_state"], "ready", "DOGnzb Comics registry state")
        assert_equal(dognzb_comics["registry_row"]["policy"]["indexer_ids"], ["15"], "DOGnzb Comics targets the configured Newznab indexer")
        assert_equal(
            dognzb_comics["registry_row"]["policy"]["download_client_by_protocol"],
            {"usenet": "sabnzbd"},
            "DOGnzb Comics prefers SAB for usenet handoff",
        )
        assert_equal(
            dognzb_comics["registry_row"]["policy"]["pack_detail_allowed_hosts"],
            ["dognzb.cr"],
            "DOGnzb Comics keeps sidecar host allowlist provider-scoped",
        )
        dognzb_request = dognzb_comics["fetch_plan"]["requests"][0]
        assert_equal(dognzb_request["params"]["indexerIds"], "15", "DOGnzb Comics request targets configured indexer id")
        assert_equal(dognzb_request["params"]["categories"], ["7030"], "DOGnzb Comics request uses comic category")
        assert_equal(
            dognzb_request["headers"]["X-Api-Key"],
            "<secret_ref:prowlarr_api_key>",
            "DOGnzb Comics inherits parent Prowlarr secret",
        )
        manga_dognzb = jobs.source_job_for_row(dognzb_comics["registry_row"], wanted_item=manga_wanted, limit=3)
        assert_equal(manga_dognzb["job_status"], "ready", "DOGnzb Comics can search manga rows when the provider declares manga support")
        assert_true(manga_dognzb["emits_download_task"], "DOGnzb Comics manga pack lane can emit downloader tasks after strict verdicts")
        assert_equal(
            manga_dognzb["fetch_plan"]["requests"][0]["params"]["indexerIds"],
            "15",
            "DOGnzb Comics manga pack lane keeps the configured Newznab indexer",
        )

        native_prowlarr = jobs_by_id["prowlarr"]
        assert_equal(native_prowlarr["job_status"], "ready", "native Prowlarr aggregate job is ready")
        assert_true(native_prowlarr["emits_download_task"], "native Prowlarr aggregate job can emit a task")
        native_prowlarr_request = native_prowlarr["fetch_plan"]["requests"][0]
        assert_equal(native_prowlarr_request["url"], "http://prowlarr.local/api/v1/search", "native Prowlarr job avoids double api path")
        assert_equal(native_prowlarr_request["params"]["categories"], ["7030", "8000", "8010"], "native Prowlarr job uses built-in comics/books category defaults")
        assert_equal(native_prowlarr_request["headers"]["X-Api-Key"], "<secret_ref:prowlarr_api_key>", "native Prowlarr job uses stable secret alias")

        current_year = time.localtime().tm_year
        absolute_wanted = {
            "series_title": "Absolute Superman",
            "issue_number": "20",
            "language": "en",
            "media_type": "comic",
            "publisher": "DC Comics",
            "year": current_year,
        }
        absolute_prowlarr_row = dict(native_prowlarr["registry_row"])
        absolute_prowlarr_row["policy"] = dict(absolute_prowlarr_row.get("policy") or {})
        absolute_prowlarr_row["policy"]["pack_detail_max_fetches"] = 1
        absolute_prowlarr = jobs.source_job_for_row(
            absolute_prowlarr_row,
            native_prowlarr["worker_plan"],
            absolute_wanted,
            limit=3,
        )
        absolute_queries = [request.get("params", {}).get("query") for request in absolute_prowlarr["fetch_plan"]["requests"]]
        assert_equal(absolute_prowlarr["fetch_plan"]["payload_mode"], "prowlarr_multi_search", "Absolute DC Prowlarr job uses multi-search")
        assert_true("Absolute Superman 20" in absolute_queries, "Absolute DC Prowlarr job keeps issue query")
        assert_true("DC Week" in absolute_queries, "Absolute DC Prowlarr job adds publisher weekly pack query")
        assert_true(f"DC Comics Weekly Releases {current_year}" in absolute_queries, "Absolute DC Prowlarr job adds current yearly release query")
        assert_false(f"Weekly Comics Pack {current_year}" in absolute_queries, "built-in Prowlarr default skips generic weekly pack query")

        prior_year_wanted = dict(absolute_wanted)
        prior_year_wanted["year"] = current_year - 1
        prior_year_prowlarr = jobs.source_job_for_row(
            absolute_prowlarr_row,
            native_prowlarr["worker_plan"],
            prior_year_wanted,
            limit=3,
        )
        prior_year_queries = [
            request.get("params", {}).get("query")
            for request in prior_year_prowlarr["fetch_plan"]["requests"]
        ]
        assert_true(
            f"DC Comics Weekly Releases {current_year - 1}" in prior_year_queries,
            "prior-year Absolute row keeps its yearly release query",
        )
        assert_false(
            f"DC Comics Weekly Releases {current_year}" in prior_year_queries,
            "built-in Prowlarr default skips current yearly release query for prior-year rows",
        )
        assert_false(
            f"Weekly Comics Pack {current_year}" in prior_year_queries,
            "built-in Prowlarr default skips current generic weekly pack query",
        )
        expanded_weekly_row = dict(absolute_prowlarr_row)
        expanded_weekly_row["policy"] = dict(expanded_weekly_row.get("policy") or {})
        expanded_weekly_row["policy"]["weekly_pack_query_limit"] = 4
        expanded_weekly_prowlarr = jobs.source_job_for_row(
            expanded_weekly_row,
            native_prowlarr["worker_plan"],
            prior_year_wanted,
            limit=3,
        )
        expanded_weekly_queries = [
            request.get("params", {}).get("query")
            for request in expanded_weekly_prowlarr["fetch_plan"]["requests"]
        ]
        assert_true(
            f"Weekly Comics Pack {current_year}" in expanded_weekly_queries,
            "explicit Prowlarr weekly cap can opt into generic weekly pack query",
        )
        absolute_result = jobs.run_source_job(
            absolute_prowlarr,
            http_get=fake_http_get,
            staging_root="/tmp/inkdrop-staging",
            now=123456.0,
        )
        absolute_sent = [attempt for attempt in absolute_result["attempts"] if attempt.get("status") == "sent"]
        assert_equal(absolute_result["result_status"], "sent", "Absolute DC weekly pack can auto-send from manifest evidence")
        assert_equal(len(absolute_sent), 1, "Absolute DC weekly pack yields one sent attempt")
        assert_equal(absolute_sent[0]["download_client"], "qbittorrent", "Absolute DC weekly pack uses torrent downloader")
        assert_equal(absolute_sent[0]["pack_contents_coverage_source"], "pack_contents_filename", "Absolute DC weekly pack requires manifest filename evidence")
        assert_true(
            "Absolute Superman 020" in absolute_sent[0]["pack_contents_matching_entry"],
            "Absolute DC weekly pack records matching manifest entry",
        )
        assert_true(
            any(str(request.get("purpose") or "").startswith("fetch_indexer_pack_detail") for request in absolute_result["fetch"]["requests_made"]),
            "Absolute DC weekly pack fetches pack detail before trusting broad pack title",
        )
        assert_equal(
            absolute_result["fetch"]["payloads"][0]["pack_detail_fetch_count"],
            1,
            "Absolute DC weekly pack priority uses the single allowed detail fetch on the useful weekly-release row",
        )
        def fake_nohit_prowlarr(request):
            assert_equal(request["url"], "http://prowlarr.local/api/v1/search", "no-hit Prowlarr still uses native aggregate endpoint")
            return {"json": [], "headers": {"Content-Type": "application/json"}}

        nohit_absolute_result = jobs.run_source_job(
            absolute_prowlarr,
            http_get=fake_nohit_prowlarr,
            staging_root="/tmp/inkdrop-staging",
            now=123456.0,
        )
        assert_equal(
            nohit_absolute_result["result_status"],
            "searched_no_candidates",
            "no-hit Prowlarr multi-search stays a no-candidate result",
        )
        nohit_fetch = nohit_absolute_result["attempts"][0]["raw"]["fetch"]
        nohit_runtime_fetch = nohit_absolute_result["runtime_results"][0]["attempts"][0]["raw"]["fetch"]
        assert_equal(nohit_fetch["payload_mode"], "prowlarr_multi_search", "no-hit attempt records multi-search mode")
        assert_equal(nohit_runtime_fetch["payload_mode"], "prowlarr_multi_search", "no-hit runtime result keeps fetch evidence")
        assert_equal(nohit_fetch["requests_made_count"], len(absolute_queries), "no-hit attempt records all executed requests")
        assert_equal(nohit_fetch["query_variants"], absolute_queries, "no-hit attempt records query variants")
        assert_equal(nohit_fetch["payload_result_count"], 0, "no-hit attempt records empty combined payload")
        assert_equal(nohit_fetch["pack_detail_fetch_count"], 0, "no-hit attempt records zero pack-detail fetches")
        assert_true(
            any(row.get("query") == "DC Week" and row.get("results") == 0 for row in nohit_fetch["variant_result_counts"]),
            "no-hit attempt records weekly-pack variant result counts",
        )
        nohit_evidence_json = json.dumps(nohit_fetch, sort_keys=True)
        assert_false("http://prowlarr.local" in nohit_evidence_json, "no-hit fetch evidence redacts request URLs")
        assert_false("<secret_ref" in nohit_evidence_json, "no-hit fetch evidence redacts secret refs")

        torznab = jobs_by_id["generic_torznab_indexer"]
        assert_equal(torznab["job_status"], "ready", "Generic Torznab job can search")
        assert_false(torznab["emits_download_task"], "Generic Torznab assist job cannot emit a task")
        torznab_request = torznab["fetch_plan"]["requests"][0]
        assert_equal(torznab_request["params"]["q"], "Example Book", "Generic Torznab job uses wanted query")
        assert_equal(torznab_request["params"]["cat"], "7030,8010", "Generic Torznab job uses category gate")
        assert_equal(torznab_request["secret_params"]["apikey"], "<secret_ref:torznab_api_key>", "Generic Torznab job uses a secret ref")

        newznab = jobs_by_id["generic_newznab_indexer"]
        assert_equal(newznab["job_status"], "ready", "Generic Newznab job can search")
        assert_false(newznab["emits_download_task"], "Generic Newznab assist job cannot emit a task")
        newznab_request = newznab["fetch_plan"]["requests"][0]
        assert_equal(newznab_request["params"]["q"], "Example Book", "Generic Newznab job uses wanted query")
        assert_equal(newznab_request["params"]["cat"], "7030,8010", "Generic Newznab job uses category gate")
        assert_equal(newznab_request["secret_params"]["apikey"], "<secret_ref:newznab_api_key>", "Generic Newznab job uses a secret ref")

        absolute_torznab = jobs.source_job_for_row(
            torznab["registry_row"],
            torznab["worker_plan"],
            absolute_wanted,
            limit=3,
        )
        absolute_torznab_queries = [request.get("params", {}).get("q") for request in absolute_torznab["fetch_plan"]["requests"]]
        assert_equal(absolute_torznab["fetch_plan"]["payload_mode"], "indexer_multi_search", "Absolute DC Torznab job uses multi-search")
        assert_true("Absolute Superman 20" in absolute_torznab_queries, "Absolute DC Torznab job keeps issue query")
        assert_true("DC Week" in absolute_torznab_queries, "Absolute DC Torznab job adds publisher weekly pack query")
        assert_true(f"Weekly Comics Pack {current_year}" in absolute_torznab_queries, "Absolute DC Torznab job adds generic weekly pack query")
        assert_true(f"Weekly Comics Pack {current_year - 1}" in absolute_torznab_queries, "Absolute DC Torznab job adds prior-year weekly pack query")
        absolute_torznab_result = jobs.run_source_job(
            absolute_torznab,
            http_get=fake_http_get,
            staging_root="/tmp/inkdrop-staging",
            now=123456.0,
        )
        assert_equal(absolute_torznab_result["result_status"], "review", "Generic Torznab stays assist while finding manifest-backed weekly pack")
        assert_true(
            any(str(request.get("purpose") or "").startswith("fetch_indexer_pack_detail") for request in absolute_torznab_result["fetch"]["requests_made"]),
            "Absolute DC Torznab weekly pack fetches pack detail before verdict",
        )
        assert_equal(
            absolute_torznab_result["fetch"]["payloads"][0]["pack_detail_fetch_count"],
            1,
            "Absolute DC Torznab weekly pack records detail fetch count",
        )
        assert_equal(
            absolute_torznab_result["runtime_results"][0]["verdicts"][0]["pack_contents_coverage_source"],
            "pack_contents_filename",
            "Absolute DC Torznab weekly pack requires manifest filename evidence",
        )

        def fake_nohit_torznab(request):
            assert_equal(request["url"], "http://jackett.local/api/v2.0/indexers/example/results/torznab", "no-hit Torznab still uses configured endpoint")
            return {
                "text": """<?xml version="1.0" encoding="utf-8"?>
                <rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed"><channel></channel></rss>
                """,
                "headers": {"Content-Type": "application/rss+xml"},
            }

        nohit_torznab_result = jobs.run_source_job(
            absolute_torznab,
            http_get=fake_nohit_torznab,
            staging_root="/tmp/inkdrop-staging",
            now=123456.0,
        )
        assert_equal(nohit_torznab_result["result_status"], "searched_no_candidates", "no-hit Torznab multi-search stays no-candidate")
        nohit_torznab_fetch = nohit_torznab_result["attempts"][0]["raw"]["fetch"]
        assert_equal(nohit_torznab_fetch["payload_mode"], "indexer_multi_search", "no-hit Torznab evidence records indexer multi-search mode")
        assert_equal(nohit_torznab_fetch["requests_made_count"], len(absolute_torznab_queries), "no-hit Torznab evidence records every executed query")
        assert_equal(nohit_torznab_fetch["query_variants"], absolute_torznab_queries, "no-hit Torznab evidence records query variants")
        assert_equal(nohit_torznab_fetch["payload_result_count"], 0, "no-hit Torznab evidence records empty combined payload")
        assert_equal(nohit_torznab_fetch["pack_detail_fetch_count"], 0, "no-hit Torznab evidence records zero pack-detail fetches")
        assert_true(
            any(row.get("query") == "DC Week" and row.get("results") == 0 for row in nohit_torznab_fetch["variant_result_counts"]),
            "no-hit Torznab evidence records weekly-pack variant counts",
        )
        nohit_torznab_json = json.dumps(nohit_torznab_fetch, sort_keys=True)
        assert_false("http://jackett.local" in nohit_torznab_json, "no-hit Torznab evidence redacts request URLs")
        assert_false("<secret_ref" in nohit_torznab_json, "no-hit Torznab evidence redacts secret refs")

        absolute_newznab = jobs.source_job_for_row(
            newznab["registry_row"],
            newznab["worker_plan"],
            absolute_wanted,
            limit=3,
        )
        absolute_newznab_queries = [request.get("params", {}).get("q") for request in absolute_newznab["fetch_plan"]["requests"]]
        assert_equal(absolute_newznab["fetch_plan"]["payload_mode"], "indexer_multi_search", "Absolute DC Newznab job uses multi-search")
        assert_true("DC Week" in absolute_newznab_queries, "Absolute DC Newznab job adds publisher weekly pack query")

        torrent_rss = jobs_by_id["generic_torrent_rss_feed"]
        assert_equal(torrent_rss["job_status"], "ready", "Generic torrent RSS job can poll")
        assert_false(torrent_rss["emits_download_task"], "Generic torrent RSS assist job cannot emit a task")
        torrent_rss_request = torrent_rss["fetch_plan"]["requests"][0]
        assert_equal(torrent_rss_request["url"], "https://torrent.example/feed.xml", "Generic torrent RSS job uses configured feed URL")

        torrent_detail_rss = jobs_by_id["generic_torrent_detail_rss_feed"]
        assert_equal(torrent_detail_rss["job_status"], "ready", "Generic torrent detail RSS job can poll")
        assert_false(torrent_detail_rss["emits_download_task"], "Generic torrent detail RSS assist job cannot emit a task")
        assert_equal(torrent_detail_rss["fetch_plan"]["payload_mode"], "rss_feed_then_torrent_detail_pages", "Generic torrent detail RSS job uses bounded detail-page payload mode")
        torrent_detail_rss_request = torrent_detail_rss["fetch_plan"]["requests"][0]
        assert_equal(torrent_detail_rss_request["url"], "https://torrent-detail-rss.example/feed.xml", "Generic torrent detail RSS job uses configured feed URL")
        torrent_detail_rss_partial = jobs.source_job_for_row(
            torrent_detail_rss["registry_row"],
            torrent_detail_rss["worker_plan"],
            torrent_detail_rss["wanted_item"],
            limit=2,
        )
        torrent_detail_rss_partial_result = jobs.run_source_job(
            torrent_detail_rss_partial,
            http_get=fake_http_get,
            now=123456.0,
        )
        assert_equal(torrent_detail_rss_partial_result["result_status"], "review", "Generic torrent detail RSS partial detail failure stays review when another item succeeds")
        detail_rss_fetch_evidence = torrent_detail_rss_partial_result["attempts"][0]["raw"]["fetch"]
        assert_equal(len(detail_rss_fetch_evidence["partial_errors"]), 1, "Generic torrent detail RSS records partial detail fetch errors")
        assert_equal(
            detail_rss_fetch_evidence["partial_errors"][0]["stage"],
            "rss_torrent_detail_page",
            "Generic torrent detail RSS attempt evidence keeps partial error stage",
        )
        assert_true(detail_rss_fetch_evidence["partial_errors"][0]["url_hash"], "Generic torrent detail RSS partial error stores URL hash")

        comic_dl = jobs_by_id["comic_dl"]
        assert_equal(comic_dl["job_status"], "operator_required", "Comic-DL bridge requires an operator payload")
        assert_false(comic_dl["emits_download_task"], "Comic-DL bridge does not emit a task by default")

        for provider_id, label in (("comics_downloader", "Comics Downloader"), ("manga_bot", "Manga Bot")):
            tool = jobs_by_id[provider_id]
            assert_equal(tool["job_status"], "operator_required", f"{label} bridge requires an operator payload")
            assert_false(tool["emits_download_task"], f"{label} bridge does not emit a task by default")
            assert_equal(tool["fetch_plan"]["command_plan"]["provider_id"], provider_id, f"{label} job uses source row")

        hakuneko = jobs_by_id["hakuneko_haruneko"]
        assert_equal(hakuneko["job_status"], "operator_required", "HakuNeko bridge requires an operator payload")
        assert_false(hakuneko["emits_download_task"], "HakuNeko bridge does not emit a task by default")
        assert_equal(hakuneko["fetch_plan"]["command_plan"]["provider_id"], "hakuneko_haruneko", "HakuNeko job uses source row")
        assert_equal(
            hakuneko["fetch_plan"]["command_plan"]["output_schema"]["contract"],
            "external_tool_candidates_from_results",
            "HakuNeko bridge exposes the external-tool result schema",
        )

        manual_cards = jobs_by_id["manual_reader_sites"]
        assert_equal(manual_cards["job_status"], "ready", "reader-site source job is ready")
        assert_false(manual_cards["emits_download_task"], "reader-site source job cannot emit a task")
        assert_equal(manual_cards["fetch_plan"]["requests"][0]["url"], "https://reader-search.example/search?q=Example+Book", "reader-site job uses URL template")

        manual_ddl = jobs_by_id["manual_ddl_blogs"]
        assert_equal(manual_ddl["job_status"], "ready", "DDL/blog source job is ready")
        assert_false(manual_ddl["emits_download_task"], "DDL/blog source job cannot emit a task")
        assert_equal(manual_ddl["fetch_plan"]["requests"][0]["url"], "https://ddl-search.example/search?s=Example+Book", "DDL/blog job uses URL template")

        manual_search = jobs_by_id["manual_search_engines"]
        assert_equal(manual_search["job_status"], "ready", "book search engine job is ready")
        assert_false(manual_search["emits_download_task"], "book search engine job cannot emit a task")
        assert_equal(manual_search["fetch_plan"]["requests"][0]["url"], "https://book-search.example/search?q=Example+Book", "book search job uses URL template")

        public_free = jobs_by_id["public_free_book_sites"]
        assert_equal(public_free["job_status"], "ready", "public/free source job is ready")
        assert_false(public_free["emits_download_task"], "public/free source job cannot emit a task")
        assert_equal(public_free["fetch_plan"]["requests"][0]["url"], "https://public-free.example/search?q=Example+Book", "public/free job uses URL template")

        shadow_search = jobs_by_id["shadow_libraries"]
        assert_equal(shadow_search["job_status"], "ready", "shadow library source job is ready")
        assert_false(shadow_search["emits_download_task"], "shadow library source job cannot emit a task")
        assert_equal(shadow_search["fetch_plan"]["requests"][0]["url"], "https://shadow-search.example/search?q=Example+Book", "shadow library job uses URL template")

        rss = jobs_by_id["rss_getcomics"]
        assert_equal(rss["job_status"], "ready", "RSS/GetComics feed job is ready")
        assert_false(rss["emits_download_task"], "RSS/GetComics feed job cannot emit a task")
        assert_equal(rss["fetch_plan"]["payload_mode"], "rss_feed_then_direct_file_probes", "RSS/GetComics job uses bounded detail/probe payload mode")
        assert_equal(rss["fetch_plan"]["requests"][0]["url"], "https://getcomics.org/feed", "RSS/GetComics job uses feed URL")

        direct_rss = jobs_by_id["generic_rss_direct_feed"]
        assert_equal(direct_rss["job_status"], "ready", "direct RSS feed job is ready")
        assert_false(direct_rss["emits_download_task"], "direct RSS assist job cannot emit a task")
        assert_equal(direct_rss["fetch_plan"]["requests"][0]["url"], "https://feeds.example/direct.xml", "direct RSS job uses configured feed URL")

        detail_rss = jobs_by_id["generic_rss_detail_direct_feed"]
        assert_equal(detail_rss["job_status"], "ready", "RSS detail direct feed job is ready")
        assert_false(detail_rss["emits_download_task"], "RSS detail direct assist job cannot emit a task")
        assert_equal(detail_rss["fetch_plan"]["payload_mode"], "rss_feed_then_direct_file_pages", "RSS detail direct job uses bounded detail-page payload mode")
        assert_equal(detail_rss["fetch_plan"]["requests"][0]["url"], "https://feeds.example/detail.xml", "RSS detail direct job uses configured feed URL")

        native_rss = jobs_by_id["rss"]
        assert_equal(native_rss["job_status"], "ready", "native RSS aggregate job is ready")
        assert_true(native_rss["emits_download_task"], "native RSS aggregate job can emit a task")
        assert_equal(native_rss["fetch_plan"]["payload_mode"], "rss_feed_then_direct_file_pages", "native RSS aggregate uses bounded detail-page payload mode")
        assert_equal(native_rss["fetch_plan"]["requests"][0]["url"], "https://feeds.example/detail.xml", "native RSS aggregate uses configured feed URL")

        current_year = time.localtime().tm_year
        stale_release_date = f"{current_year - 2}-01-01"
        recent_release_date = time.strftime("%Y-%m-%d")
        stale_wanted = {
            "series_title": "Example Book",
            "issue_number": "1",
            "release_date": stale_release_date,
            "language": "en",
        }
        recent_wanted = {
            "series_title": "Example Book",
            "issue_number": "1",
            "release_date": recent_release_date,
            "language": "en",
        }
        stale_native_rss = jobs.source_job_for_row(native_rss["registry_row"], wanted_item=stale_wanted, limit=1)
        assert_equal(stale_native_rss["job_status"], "blocked", "native RSS blocks stale backfill rows by default")
        assert_true("fresh releases" in stale_native_rss["reason"], "native RSS stale block explains fresh-release scope")
        recent_native_rss = jobs.source_job_for_row(native_rss["registry_row"], wanted_item=recent_wanted, limit=1)
        assert_equal(recent_native_rss["job_status"], "ready", "native RSS remains ready for recent releases")
        assert_false("source_scope" in recent_native_rss, "recent native RSS row has no scope block")
        undated_native_rss = jobs.source_job_for_row(native_rss["registry_row"], wanted_item={"series_title": "Example Book"}, limit=1)
        assert_equal(undated_native_rss["job_status"], "ready", "native RSS stays ready when metadata has no date signal")
        optout_native_rss_row = dict(native_rss["registry_row"])
        optout_native_rss_row["policy"] = dict(optout_native_rss_row.get("policy") or {})
        optout_native_rss_row["policy"]["fresh_release_only"] = False
        optout_native_rss = jobs.source_job_for_row(optout_native_rss_row, wanted_item=stale_wanted, limit=1)
        assert_equal(optout_native_rss["job_status"], "ready", "native RSS can be opted into backfill from provider policy")

        stale_detail_rss = jobs.source_job_for_row(detail_rss["registry_row"], wanted_item=stale_wanted, limit=1)
        assert_equal(stale_detail_rss["job_status"], "blocked", "RSS detail feed blocks stale backfill rows by default")
        stale_torrent_rss = jobs.source_job_for_row(torrent_rss["registry_row"], wanted_item=stale_wanted, limit=1)
        assert_equal(stale_torrent_rss["job_status"], "ready", "torrent RSS indexer feeds are not blocked by direct RSS freshness scope")

        feed_detail_probe = jobs_by_id["generic_rss_detail_probe_feed"]
        assert_equal(feed_detail_probe["job_status"], "ready", "RSS detail probe feed job is ready")
        assert_false(feed_detail_probe["emits_download_task"], "RSS detail probe assist job cannot emit a task")
        assert_equal(feed_detail_probe["fetch_plan"]["payload_mode"], "rss_feed_then_direct_file_probes", "RSS detail probe job uses bounded detail-page plus HEAD-probe payload mode")
        assert_equal(feed_detail_probe["fetch_plan"]["requests"][0]["url"], "https://feeds.example/probe.xml", "RSS detail probe job uses configured feed URL")

        feed_reader_pack = jobs_by_id["generic_rss_reader_page_pack_feed"]
        assert_equal(feed_reader_pack["job_status"], "ready", "RSS reader page pack feed job is ready")
        assert_false(feed_reader_pack["emits_download_task"], "RSS reader page pack feed assist job cannot emit a task")
        assert_equal(feed_reader_pack["fetch_plan"]["payload_mode"], "rss_feed_then_reader_pages", "RSS reader page pack job uses bounded reader-page payload mode")
        assert_equal(feed_reader_pack["fetch_plan"]["requests"][0]["url"], "https://feeds.example/reader.xml", "RSS reader page pack job uses configured feed URL")

        direct_file = jobs_by_id["generic_direct_file_search"]
        assert_equal(direct_file["job_status"], "ready", "direct file search job is ready")
        assert_false(direct_file["emits_download_task"], "direct file search assist job cannot emit a task")
        assert_equal(direct_file["fetch_plan"]["requests"][0]["url"], "https://files.example/search?q=Example+Book", "direct file search job uses configured URL template")
        assert_equal(direct_file["fetch_plan"]["requests"][1]["url"], "https://files.example/search/page/2?q=Example+Book", "direct file search job uses configured pagination URL template")
        assert_equal(direct_file["fetch_plan"]["requests"][2]["url"], "https://files.example/latest", "direct file search job uses literal list URL template")
        assert_equal(direct_file["fetch_plan"]["requests"][3]["url"], "https://files.example/latest/page/2", "direct file search job uses literal list pagination URL template")

        direct_detail = jobs_by_id["generic_direct_file_detail_search"]
        assert_equal(direct_detail["job_status"], "ready", "direct file detail search job is ready")
        assert_false(direct_detail["emits_download_task"], "direct file detail search assist job cannot emit a task")
        assert_equal(direct_detail["fetch_plan"]["payload_mode"], "html_search_then_direct_file_pages", "direct file detail search job uses bounded detail-page payload mode")
        assert_equal(direct_detail["fetch_plan"]["requests"][0]["url"], "https://detail.example/wp-json/wp/v2/search?search=Example+Book&per_page=1&subtype=post", "direct file detail job derives WordPress search endpoint from base URL")

        direct_probe = jobs_by_id["generic_direct_file_probe_source"]
        assert_equal(direct_probe["job_status"], "ready", "direct file probe job is ready")
        assert_false(direct_probe["emits_download_task"], "direct file probe assist job cannot emit a task")
        assert_equal(direct_probe["fetch_plan"]["payload_mode"], "html_search_then_direct_file_probes", "direct file probe job uses bounded detail-page plus HEAD-probe payload mode")
        assert_equal(direct_probe["fetch_plan"]["requests"][0]["url"], "https://probe.example/wp-json/wp/v2/search?search=Example+Book&per_page=1&subtype=post", "direct file probe job derives WordPress search endpoint from base URL")

        reader_pack = jobs_by_id["generic_reader_page_pack_source"]
        assert_equal(reader_pack["job_status"], "ready", "reader page pack job is ready")
        assert_false(reader_pack["emits_download_task"], "reader page pack assist job cannot emit a task")
        assert_equal(reader_pack["fetch_plan"]["payload_mode"], "html_search_then_reader_pages", "reader page pack job uses bounded reader-page payload mode")
        assert_equal(reader_pack["fetch_plan"]["requests"][0]["url"], "https://reader-pack.example/search?q=Example+Book", "reader page pack job uses configured URL template")

        json_direct = jobs_by_id["generic_json_direct_source"]
        assert_equal(json_direct["job_status"], "ready", "Generic JSON direct job is ready")
        assert_false(json_direct["emits_download_task"], "Generic JSON direct assist job cannot emit a task")
        assert_equal(json_direct["fetch_plan"]["requests"][0]["url"], "https://json.example/wp-json/wp/v2/posts?search=Example+Book&per_page=1&_fields=id,link,title,content,excerpt", "Generic JSON direct job derives WordPress posts endpoint from base URL")

        opds = jobs_by_id["generic_opds_catalog"]
        assert_equal(opds["job_status"], "ready", "Generic OPDS job is ready")
        assert_false(opds["emits_download_task"], "Generic OPDS assist job cannot emit a task")
        assert_equal(opds["fetch_plan"]["requests"][0]["url"], "https://opds.example/catalog.xml", "Generic OPDS job uses configured catalog URL")

        comicscodes = jobs_by_id["comicscodes"]
        assert_equal(comicscodes["job_status"], "ready", "ComicsCodes feed/list job is ready")
        assert_false(comicscodes["emits_download_task"], "ComicsCodes feed/list job cannot emit a task")
        assert_equal(comicscodes["fetch_plan"]["payload_mode"], "multi_payload", "ComicsCodes job uses multiple payloads")
        assert_equal(comicscodes["fetch_plan"]["requests"][0]["url"], "https://comics.codes/feed/", "ComicsCodes job uses feed URL")

        summary = jobs.source_job_summary(source_jobs)
        assert_true(summary["ready_http"] >= 5, "job summary counts ready HTTP jobs")
        assert_true(summary["operator_required"] >= 4, "job summary counts operator jobs")

        missing_http = jobs.run_source_job(standard)
        assert_equal(missing_http["result_status"], "provider_wait", "HTTP jobs record provider wait without an injected client")
        assert_equal(missing_http["reason"], "http_client_required", "missing HTTP client reason is explicit")
        assert_equal(missing_http["attempts"][0]["status"], "provider_wait", "missing HTTP client becomes a recordable provider-wait attempt")
        assert_equal(missing_http["attempts"][0]["retry_scope"], "source_worker_provider_fetch", "missing HTTP client uses provider-fetch retry scope")

        def failing_http_get(_request):
            raise RuntimeError("simulated provider timeout")

        failed_prowlarr = jobs.run_source_job(nyaa, http_get=failing_http_get)
        assert_equal(failed_prowlarr["result_status"], "provider_wait", "Prowlarr fetch failure records provider wait")
        assert_equal(failed_prowlarr["attempts"][0]["status"], "provider_unavailable", "Prowlarr fetch failure is provider unavailable")
        assert_equal(failed_prowlarr["attempts"][0]["provider_id"], "prowlarr_nyaa", "Prowlarr failure keeps configured provider id")
        assert_true(failed_prowlarr["attempts"][0].get("download_url_hash"), "Prowlarr failure records a request fingerprint")

        volume_nyaa = jobs.source_job_for_row(
            nyaa["registry_row"],
            nyaa["worker_plan"],
            {
                "series_title": "Fairy Tail",
                "issue_number": "52",
                "volume": "52",
                "unitType": "volume",
                "language": "en",
                "media_type": "manga",
                "publisher": "Kodansha",
            },
            limit=20,
        )

        def partially_failing_prowlarr(request):
            query = (request.get("params") or {}).get("query")
            if query == "Fairy Tail v52":
                return {"json": [], "headers": {"Content-Type": "application/json"}}
            raise RuntimeError(f"simulated partial indexer failure for {query}")

        partial_prowlarr = jobs.run_source_job(volume_nyaa, http_get=partially_failing_prowlarr)
        assert_equal(
            partial_prowlarr["result_status"],
            "provider_wait",
            "mostly failed Prowlarr multi-search records provider wait instead of no-candidate",
        )
        assert_equal(partial_prowlarr["reason"], "partial_indexer_search_failed", "partial indexer wait reason is explicit")
        assert_equal(partial_prowlarr["attempts"][0]["status"], "provider_wait", "partial indexer search records provider-wait attempt")
        partial_fetch = partial_prowlarr["attempts"][0]["raw"]["fetch"]
        assert_equal(partial_fetch["payload_mode"], "prowlarr_multi_search", "partial indexer evidence records multi-search mode")
        assert_true(len(partial_fetch["partial_errors"]) >= 4, "partial indexer evidence records failed query variants")
        assert_true(
            any(row.get("query") == "Fairy Tail v52" and row.get("results") == 0 for row in partial_fetch["variant_result_counts"]),
            "partial indexer evidence keeps successful zero-result variant",
        )
        partial_evidence_json = json.dumps(partial_fetch, sort_keys=True)
        assert_false("http://prowlarr.local" in partial_evidence_json, "partial indexer evidence redacts request URLs")
        assert_false("<secret_ref" in partial_evidence_json, "partial indexer evidence redacts secret refs")

        failed_getcomics = jobs.run_source_job(rss, http_get=failing_http_get)
        assert_equal(failed_getcomics["result_status"], "provider_wait", "RSS/GetComics fetch failure records provider wait")
        assert_equal(failed_getcomics["attempts"][0]["status"], "provider_unavailable", "RSS/GetComics fetch failure is provider unavailable")
        assert_equal(failed_getcomics["attempts"][0]["provider_id"], "rss_getcomics", "RSS/GetComics failure keeps configured provider id")

        nohit_detail_rss_job = jobs.source_job_for_row(
            detail_rss["registry_row"],
            detail_rss["worker_plan"],
            {"series_title": "Missing Book", "language": "en"},
            limit=1,
        )
        nohit_detail_rss_result = jobs.run_source_job(
            nohit_detail_rss_job,
            http_get=fake_http_get,
            staging_root="/tmp/inkdrop-staging",
            now=123456.0,
        )
        assert_equal(nohit_detail_rss_result["result_status"], "searched_no_candidates", "RSS detail direct no-hit records no-candidate result")
        nohit_detail_rss_fetch = nohit_detail_rss_result["attempts"][0]["raw"]["fetch"]
        assert_equal(nohit_detail_rss_fetch["payload_mode"], "rss_feed_then_direct_file_pages", "RSS no-hit evidence records detail-feed mode")
        assert_equal(nohit_detail_rss_fetch["requests_made_count"], 1, "RSS no-hit evidence records only the feed request")
        assert_equal(nohit_detail_rss_fetch["feed_evidence"]["feed_item_count"], 1, "RSS no-hit evidence counts feed items")
        assert_equal(nohit_detail_rss_fetch["feed_evidence"]["matching_feed_item_count"], 0, "RSS no-hit evidence records zero matching feed items")
        assert_true(
            "Example Book 001" in nohit_detail_rss_fetch["feed_evidence"]["feed_item_samples"][0]["title"],
            "RSS no-hit evidence samples feed item titles",
        )
        nohit_detail_rss_evidence_json = json.dumps(nohit_detail_rss_fetch, sort_keys=True)
        assert_false("https://posts.example" in nohit_detail_rss_evidence_json, "RSS no-hit evidence redacts item URLs")

        candidate_headers = {
            "standard_ebooks": {"0": {"Content-Type": "application/epub+zip", "Content-Length": "204800"}},
            "gutendex": {"0": {"Content-Type": "application/epub+zip", "Content-Length": "204800"}},
        }
        results = jobs.run_source_jobs(
            [
                standard,
                gutendex,
                archive,
                mangadex,
                nyaa,
                tokyo,
                dognzb_comics,
                native_prowlarr,
                torznab,
                newznab,
                torrent_rss,
                torrent_detail_rss,
                manual_cards,
                manual_ddl,
                manual_search,
                public_free,
                shadow_search,
                rss,
                native_rss,
                direct_rss,
                detail_rss,
                feed_detail_probe,
                feed_reader_pack,
                direct_file,
                direct_detail,
                direct_probe,
                reader_pack,
                json_direct,
                opds,
                comicscodes,
            ],
            http_get=fake_http_get,
            candidate_headers_by_provider=candidate_headers,
            staging_root="/tmp/inkdrop-staging",
            now=123456.0,
        )
        results_by_id = {row["provider_id"]: row for row in results}
        assert_equal(results_by_id["standard_ebooks"]["result_status"], "sent", "Standard Ebooks job can produce a sent attempt")
        assert_equal(
            results_by_id["standard_ebooks"]["attempts"][0]["raw"]["download_task_seed"]["candidate_identity"],
            results_by_id["standard_ebooks"]["attempts"][0]["candidate_identity"],
            "direct download task seed persists the candidate identity",
        )
        assert_equal(results_by_id["gutendex"]["result_status"], "sent", "Gutendex job can produce a sent attempt")
        assert_equal(results_by_id["internet_archive"]["result_status"], "sent", "IA job can produce a sent attempt")
        assert_equal(results_by_id["mangadex"]["result_status"], "blocked", "broad MangaDex series job must not send one arbitrary chapter")
        assert_false(any(row.get("status") == "sent" for row in results_by_id["mangadex"]["attempts"]), "broad MangaDex series job sent an unrequested chapter")
        assert_false("download_client" in results_by_id["mangadex"]["attempts"][0], "blocked broad MangaDex job must not create a page-pack handoff")
        assert_equal(results_by_id["prowlarr_nyaa"]["result_status"], "sent", "Nyaa auto manga job can send")
        assert_equal(results_by_id["prowlarr_nyaa"]["attempts"][0]["download_client"], "qbittorrent", "Nyaa auto manga job uses qBittorrent")
        assert_equal(results_by_id["prowlarr_tokyo_toshokan_manga"]["result_status"], "sent", "Tokyo Toshokan manga auto job can send")
        assert_equal(results_by_id["prowlarr_tokyo_toshokan_manga"]["attempts"][0]["download_client"], "qbittorrent", "Tokyo Toshokan manga auto job uses qBittorrent")
        assert_equal(results_by_id["prowlarr_dognzb_comics"]["result_status"], "sent", "DOGnzb Comics auto job can send")
        assert_equal(results_by_id["prowlarr_dognzb_comics"]["attempts"][0]["download_client"], "sabnzbd", "DOGnzb Comics auto job uses SABnzbd")
        manhattan_wanted = {
            "series_title": "The Manhattan Projects",
            "series": "The Manhattan Projects",
            "issue_number": "3",
            "normalized_number": "003",
            "language": "en",
            "media_type": "comic",
            "publisher": "Image Comics",
            "year": 2012,
        }
        manhattan_dognzb = jobs.source_job_for_row(
            dognzb_comics["registry_row"],
            dognzb_comics["worker_plan"],
            manhattan_wanted,
            limit=10,
        )

        def fake_manhattan_dognzb(request):
            assert_equal(request["url"], "http://prowlarr.local/api/v1/search", "Manhattan DOGnzb job uses native Prowlarr endpoint")
            assert_equal(request.get("params", {}).get("indexerIds"), "15", "Manhattan DOGnzb job targets DOGnzb")
            return {
                "json": [
                    {
                        "title": "The.Manhattan.Projects.003.(2012).(Digital).(Archangel+Zone-Empire)",
                        "protocol": "usenet",
                        "indexer": "DOGnzb",
                        "indexerId": 15,
                        "categories": [{"id": 7030, "name": "Books/Comics"}],
                        "downloadUrl": "https://dognzb.example/api?t=get&id=manhattan-projects-003-digital",
                        "guid": "manhattan-projects-003-digital",
                        "size": 124456789,
                    },
                    {
                        "title": "The.Manhattan.Projects.003.(2012).(c2c).(1920px).(ZeroDaze-DCP-HD)",
                        "protocol": "usenet",
                        "indexer": "DOGnzb",
                        "indexerId": 15,
                        "categories": [{"id": 7030, "name": "Books/Comics"}],
                        "downloadUrl": "https://dognzb.example/api?t=get&id=manhattan-projects-003-c2c-hd",
                        "guid": "manhattan-projects-003-c2c-hd",
                        "size": 224456789,
                    },
                    {
                        "title": "Manhattan.Projects.003.2012.Digital.F.Archangel+Zone-Empire",
                        "protocol": "usenet",
                        "indexer": "DOGnzb",
                        "indexerId": 15,
                        "categories": [{"id": 7030, "name": "Books/Comics"}],
                        "downloadUrl": "https://dognzb.example/api?t=get&id=manhattan-projects-003-noarticle",
                        "guid": "manhattan-projects-003-noarticle",
                        "size": 124456790,
                    },
                    {
                        "title": "The.Manhattan.Projects.-.The.Sun.Beyond.the.Stars.003.2015.Digital.Zone-Empire",
                        "protocol": "usenet",
                        "indexer": "DOGnzb",
                        "indexerId": 15,
                        "categories": [{"id": 7030, "name": "Books/Comics"}],
                        "downloadUrl": "https://dognzb.example/api?t=get&id=manhattan-projects-sun-003",
                        "guid": "manhattan-projects-sun-003",
                        "size": 124456791,
                    },
                ],
                "headers": {"Content-Type": "application/json"},
            }

        manhattan_result = jobs.run_source_job(
            manhattan_dognzb,
            http_get=fake_manhattan_dognzb,
            staging_root="/tmp/inkdrop-staging",
            now=123456.0,
        )
        manhattan_sent = [attempt for attempt in manhattan_result["attempts"] if attempt.get("status") == "sent"]
        assert_equal(manhattan_result["result_status"], "sent", "multi-hit DOGnzb job still sends one candidate")
        assert_equal(manhattan_result["safe_candidate_count"], 3, "multi-hit DOGnzb job excludes related-series candidates")
        assert_equal(len(manhattan_sent), 1, "multi-hit DOGnzb job records one sent handoff")
        assert_equal(
            manhattan_sent[0]["title"],
            "The.Manhattan.Projects.003.(2012).(Digital).(Archangel+Zone-Empire)",
            "multi-hit DOGnzb job selects the closest exact issue title",
        )
        assert_true(manhattan_result["auto_send_selection"]["applied"], "multi-hit DOGnzb job records auto-send selection evidence")
        assert_equal(
            manhattan_result["auto_send_selection"]["suppressed_sent_candidate_count"],
            2,
            "multi-hit DOGnzb job suppresses extra sent handoffs",
        )
        assert_false(
            any("Sun.Beyond.the.Stars" in (attempt.get("title") or "") for attempt in manhattan_sent),
            "multi-hit DOGnzb job does not hand off the subseries-looking title",
        )
        subseries_attempts = [
            attempt
            for attempt in manhattan_result["attempts"]
            if "Sun.Beyond.the.Stars" in (attempt.get("title") or "")
        ]
        assert_equal(len(subseries_attempts), 1, "multi-hit DOGnzb job retains subseries rejection evidence")
        assert_equal(subseries_attempts[0]["status"], "blocked", "subseries candidate fails closed")
        assert_equal(
            subseries_attempts[0]["reason"],
            "related_series_identity",
            "subseries candidate records its identity rejection",
        )
        assert_equal(results_by_id["prowlarr"]["result_status"], "sent", "native Prowlarr aggregate auto job can send")
        assert_equal(results_by_id["prowlarr"]["attempts"][0]["download_client"], "qbittorrent", "native Prowlarr aggregate uses qBittorrent")
        assert_equal(results_by_id["generic_torznab_indexer"]["result_status"], "review", "Generic Torznab stays review by default")
        assert_equal(results_by_id["generic_newznab_indexer"]["result_status"], "review", "Generic Newznab stays review by default")
        assert_equal(results_by_id["generic_torrent_rss_feed"]["result_status"], "review", "Generic torrent RSS stays review by default")
        assert_equal(results_by_id["generic_torrent_rss_feed"]["runtime_results"][0]["verdicts"][0]["info_hash"], "JOBSTORRENTRSSINFOHASH123456789ABCDEF123456", "Generic torrent RSS job parses embedded info hash")
        assert_equal(results_by_id["generic_torrent_detail_rss_feed"]["result_status"], "review", "Generic torrent detail RSS stays review by default")
        assert_equal(results_by_id["generic_torrent_detail_rss_feed"]["runtime_results"][0]["verdicts"][0]["info_hash"], "JOBSDETAILRSSHASH123456789ABCDEF123456", "Generic torrent detail RSS job parses detail page info hash")
        assert_equal(results_by_id["manual_reader_sites"]["result_status"], "review", "reader-site source stays review")
        assert_equal(results_by_id["manual_ddl_blogs"]["result_status"], "review", "DDL/blog source stays review")
        assert_equal(results_by_id["manual_search_engines"]["result_status"], "review", "book search engine stays review")
        assert_equal(results_by_id["public_free_book_sites"]["result_status"], "review", "public/free source stays review")
        assert_equal(results_by_id["shadow_libraries"]["result_status"], "review", "shadow library source stays review")
        assert_equal(results_by_id["rss_getcomics"]["result_status"], "review", "RSS/GetComics feed stays review")
        getcomics_verdict = results_by_id["rss_getcomics"]["runtime_results"][0]["verdicts"][0]
        assert_equal(getcomics_verdict["download_url"], "https://pixeldrain.com/api/file/gcjobs001?download", "RSS/GetComics rewrites Pixeldrain transport")
        assert_equal(getcomics_verdict["discovery_provider_id"], "rss_getcomics", "RSS/GetComics preserves discovery identity")
        assert_equal(getcomics_verdict["transport_id"], "pixeldrain", "RSS/GetComics preserves transport identity")
        assert_equal(getcomics_verdict["probe_status_code"], 200, "RSS/GetComics requires successful probe")
        assert_equal(results_by_id["generic_rss_direct_feed"]["result_status"], "review", "direct RSS feed stays review by default")
        assert_equal(results_by_id["generic_rss_direct_feed"]["attempts"][0]["size_bytes"], 204800, "direct RSS feed parses embedded file link size")
        assert_equal(results_by_id["generic_rss_detail_direct_feed"]["result_status"], "review", "RSS detail direct feed stays review by default")
        assert_equal(results_by_id["generic_rss_detail_direct_feed"]["runtime_results"][0]["verdicts"][0]["download_url"], "https://files.example/detail-feed/example-book-001.cbz", "RSS detail direct job parses detail page file link")
        assert_equal(results_by_id["rss"]["result_status"], "sent", "native RSS aggregate auto job can send")
        assert_equal(results_by_id["rss"]["attempts"][0]["download_client"], "inkdrop_direct", "native RSS aggregate uses direct downloader")
        assert_equal(results_by_id["rss"]["runtime_results"][0]["verdicts"][0]["download_url"], "https://files.example/detail-feed/example-book-001.cbz", "native RSS aggregate parses detail page file link")
        assert_equal(results_by_id["generic_rss_detail_probe_feed"]["result_status"], "review", "RSS detail probe feed stays review by default")
        assert_equal(results_by_id["generic_rss_detail_probe_feed"]["runtime_results"][0]["verdicts"][0]["download_url"], "https://pixeldrain.com/api/file/pdjobs001?download", "RSS detail probe job rewrites supported shared file host link")
        assert_equal(results_by_id["generic_rss_reader_page_pack_feed"]["result_status"], "review", "RSS reader page pack feed stays review by default")
        assert_equal(results_by_id["generic_rss_reader_page_pack_feed"]["runtime_results"][0]["attempts"][0]["page_count"], 3, "RSS reader page pack job expands feed series page into page-pack candidate")
        assert_equal(results_by_id["generic_direct_file_search"]["result_status"], "review", "direct file search stays review by default")
        assert_equal(results_by_id["generic_direct_file_search"]["runtime_results"][0]["verdicts"][0]["download_url"], "https://files.example/downloads/example-book-001.cbz", "direct file search job unwraps explicit redirect query target")
        assert_equal(results_by_id["generic_direct_file_search"]["candidate_count"], 4, "direct file search job aggregates search and list candidates")
        assert_equal(results_by_id["generic_direct_file_search"]["runtime_results"][1]["verdicts"][0]["download_url"], "https://files.example/downloads/example-book-002.cbz", "direct file search job parses paginated result page")
        assert_equal(results_by_id["generic_direct_file_search"]["runtime_results"][2]["verdicts"][0]["download_url"], "https://files.example/downloads/example-book-003.cbz", "direct file search job parses literal list page")
        assert_equal(results_by_id["generic_direct_file_search"]["runtime_results"][3]["verdicts"][0]["download_url"], "https://files.example/downloads/example-book-004.cbz", "direct file search job parses literal list pagination page")
        assert_equal(results_by_id["generic_direct_file_detail_search"]["result_status"], "review", "direct file detail search stays review by default")
        assert_equal(results_by_id["generic_direct_file_detail_search"]["runtime_results"][0]["verdicts"][0]["download_url"], "https://files.example/detail/example-book-001.cbz", "direct file detail job parses JSON-LD file URL")
        assert_equal(results_by_id["generic_direct_file_probe_source"]["result_status"], "review", "direct file probe stays review by default")
        assert_equal(results_by_id["generic_direct_file_probe_source"]["runtime_results"][0]["verdicts"][0]["download_url"], "https://pixeldrain.com/api/file/pdjobs001?download", "direct file probe job rewrites supported shared file host link")
        assert_equal(results_by_id["generic_reader_page_pack_source"]["result_status"], "review", "reader page pack stays review by default")
        assert_equal(results_by_id["generic_reader_page_pack_source"]["runtime_results"][0]["attempts"][0]["page_count"], 3, "reader page pack job expands series page into page-pack candidate")
        assert_equal(results_by_id["generic_json_direct_source"]["result_status"], "review", "Generic JSON direct stays review by default")
        assert_equal(results_by_id["generic_json_direct_source"]["runtime_results"][0]["verdicts"][0]["download_url"], "https://files.example/json/example-book-001.cbz", "Generic JSON direct job parses rendered HTML file link")
        assert_equal(results_by_id["generic_opds_catalog"]["result_status"], "review", "Generic OPDS stays review by default")
        assert_equal(results_by_id["comicscodes"]["result_status"], "review", "ComicsCodes feed/list stays review")
        assert_false("download_client" in results_by_id["rss_getcomics"]["attempts"][0], "RSS/GetComics feed has no download handoff")
        assert_false("download_client" in results_by_id["generic_rss_direct_feed"]["attempts"][0], "direct RSS feed has no download handoff")
        assert_false("download_client" in results_by_id["generic_rss_detail_direct_feed"]["attempts"][0], "RSS detail direct feed has no download handoff")
        assert_false("download_client" in results_by_id["generic_rss_detail_probe_feed"]["attempts"][0], "RSS detail probe feed has no download handoff")
        assert_false("download_client" in results_by_id["generic_rss_reader_page_pack_feed"]["attempts"][0], "RSS reader page pack feed has no download handoff")
        assert_false("download_client" in results_by_id["generic_direct_file_search"]["attempts"][0], "direct file search has no download handoff")
        assert_false("download_client" in results_by_id["generic_direct_file_detail_search"]["attempts"][0], "direct file detail search has no download handoff")
        assert_false("download_client" in results_by_id["generic_direct_file_probe_source"]["attempts"][0], "direct file probe has no download handoff")
        assert_false("download_client" in results_by_id["generic_reader_page_pack_source"]["attempts"][0], "reader page pack has no download handoff")
        assert_false("download_client" in results_by_id["generic_json_direct_source"]["attempts"][0], "Generic JSON direct has no download handoff")
        assert_false("download_client" in results_by_id["generic_opds_catalog"]["attempts"][0], "Generic OPDS has no download handoff")
        assert_false("download_client" in results_by_id["comicscodes"]["attempts"][0], "ComicsCodes feed/list has no download handoff")
        assert_false("download_client" in results_by_id["mangadex"]["attempts"][0], "blocked broad MangaDex job has no download handoff")
        for assist_provider in (
            "generic_torznab_indexer", "generic_newznab_indexer",
            "generic_torrent_rss_feed", "generic_torrent_detail_rss_feed",
            "manual_reader_sites", "manual_ddl_blogs", "manual_search_engines",
            "public_free_book_sites", "shadow_libraries",
        ):
            assert_false(
                any(row.get("status") == "sent" for row in results_by_id[assist_provider]["attempts"]),
                f"{assist_provider} assist job performed a download handoff",
            )

        operator_missing = jobs.run_source_job(comic_dl)
        assert_equal(operator_missing["result_status"], "operator_required", "operator jobs do not fake payloads")

        auto_tool_row = dict(comic_dl["registry_row"])
        auto_tool_row.update(
            {
                "registry_state": "ready",
                "source_mode": "auto",
                "auto_download_allowed": True,
                "requires_manual_review": False,
                "policy": {
                    "allowed_extensions": [".cbz", ".zip"],
                    "requires_manual_confirm": False,
                    "auto_stage_tool_output": True,
                    "staged_output_root": "/tmp/inkdrop-tool-staging",
                    "command_executable": "comic-dl",
                    "command_args": ["--search", "{query}", "--output", "{staged_output_root}"],
                },
            }
        )
        auto_tool_job = jobs.source_job_for_row(auto_tool_row, wanted_item={"series_title": "Example Book"}, limit=2)
        assert_equal(auto_tool_job["job_status"], "ready", "configured external tool job is ready")
        assert_true(auto_tool_job["can_execute_with_tool_runner"], "configured external tool job requires a tool runner")
        assert_true(auto_tool_job["emits_download_task"], "configured external tool job can emit staged task")
        assert_equal(auto_tool_job["fetch_plan"]["payload_mode"], "external_tool_command", "configured external tool uses command payload")
        missing_tool_runner = jobs.run_source_job(auto_tool_job)
        assert_equal(missing_tool_runner["result_status"], "provider_wait", "configured external tool records provider wait without runner")
        assert_equal(missing_tool_runner["reason"], "tool_runner_required", "configured external tool reports missing runner")
        assert_equal(missing_tool_runner["attempts"][0]["status"], "provider_wait", "missing tool runner becomes a recordable provider-wait attempt")
        assert_equal(missing_tool_runner["attempts"][0]["provider_id"], "comic_dl", "external tool provider-wait attempt keeps configured provider id")

        def fake_tool_runner(command_plan):
            assert_equal(command_plan["argv"][0], "comic-dl", "tool runner receives executable")
            assert_equal(command_plan["argv"][2], "Example Book", "tool runner receives expanded query")
            return {
                "json": {
                    "results": [
                        {
                            "title": "Example Book 001",
                            "site": "ReadComicOnline",
                            "url": "https://reader.example/comic/example-book/1",
                            "output_path": "/tmp/inkdrop-tool-staging/example-book-001.cbz",
                            "extension": ".cbz",
                            "tool_name": "comic-dl",
                        }
                    ]
                }
            }

        auto_tool_result = jobs.run_source_job(auto_tool_job, tool_runner=fake_tool_runner)
        assert_equal(auto_tool_result["candidate_count"], 1, "configured external tool result candidate count")
        assert_equal(auto_tool_result["attempts"][0]["download_client"], "inkdrop_external_tool", "configured external tool emits staged handoff")
        assert_equal(auto_tool_result["attempts"][0]["status"], "staged_file_ready", "configured external tool attempt status")

        recordable = jobs.recordable_attempts(results)
        assert_true(len(recordable) >= 4, "job results expose recordable source attempts")
        assert_true(
            any(attempt.get("download_client") == "inkdrop_direct" for attempt in recordable),
            "safe direct jobs expose direct download task seeds",
        )
        assert_true(
            any(
                attempt.get("provider_id") == "prowlarr_nyaa" and attempt.get("download_client") == "qbittorrent"
                for attempt in recordable
            ),
            "Nyaa auto manga attempts expose qBittorrent handoffs",
        )
        assert_true(
            any(
                attempt.get("provider_id") == "prowlarr_tokyo_toshokan_manga" and attempt.get("download_client") == "qbittorrent"
                for attempt in recordable
            ),
            "Tokyo Toshokan manga auto attempts expose qBittorrent handoffs",
        )

    ok("settings-backed source jobs plan, fetch, and evaluate without hidden IO")


if __name__ == "__main__":
    main()
