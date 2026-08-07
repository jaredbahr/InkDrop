#!/usr/bin/env python3
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))

import argparse
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

import requests

from core import inkdrop_db
from core import inkdrop_runtime_config

try:
    from core import inkdrop_state
except Exception:
    inkdrop_state = None

try:
    from core import inkdrop_completed_import as completed_import
except Exception:
    completed_import = None


STATE_DIR = inkdrop_runtime_config.state_dir()
CONFIG_DIR = inkdrop_runtime_config.config_dir()
LOG_DIR = inkdrop_runtime_config.log_dir()
INKDROP_STATE_DB = STATE_DIR / (inkdrop_state.STATE_DB_NAME if inkdrop_state else "inkdrop-state.sqlite3")
KAVITA_DB = inkdrop_runtime_config.kavita_db_path()
MANGA_ROOT = Path(os.environ.get("INKDROP_MANGA_ROOT") or "/library/manga")
KAVITA_MANGA_ROOT = os.environ.get("INKDROP_KAVITA_MANGA_ROOT") or "/data/manga"
LOG_PATH = LOG_DIR / "mangadex-direct.log"
MANGADEX_API = "https://api.mangadex.org"
SOURCE = "mangadex"
TERMINAL_STATES = {"verified", "superseded_duplicate"}


def utc_stamp(ts=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts or time.time()))


def log(event, **payload):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"ts": time.time(), "ts_iso": utc_stamp(), "event": event, **payload}, sort_keys=True) + "\n")


def json_loads(value, default=None):
    try:
        loaded = json.loads(value or "{}")
    except (TypeError, ValueError):
        return default
    return loaded


def normalize_key(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def source_order_allows_mangadex(row, raw):
    values = json_loads(row.get("source_order_json") or "[]", [])
    if not isinstance(values, list) or not values:
        values = raw.get("source_order") if isinstance(raw.get("source_order"), list) else []
    return "mangadex" in {str(value or "").strip().lower() for value in values}


def connect_state():
    return inkdrop_db.open_connection(
        INKDROP_STATE_DB,
        readonly=True,
        timeout_seconds=30,
        busy_timeout_ms=30000,
        operation="mangadex_load_queue",
    )


def load_rows(series_filter=None, max_total=10, force=False):
    if not INKDROP_STATE_DB.exists():
        return []
    clauses = [
        "q.active = 1",
        "lower(coalesce(s.metadata_provider, '')) = 'mangadex'",
        "coalesce(w.status, 'wanted') not in ('satisfied', 'ignored', 'suppressed')",
    ]
    params = []
    if not force:
        clauses.append("q.state not in ('verified', 'superseded_duplicate', 'downloading', 'importing')")
    if series_filter:
        wanted = {normalize_key(value) for value in series_filter if normalize_key(value)}
    else:
        wanted = set()
    with connect_state() as con:
        rows = [
            dict(row)
            for row in con.execute(
                f"""
                select
                    q.id as queue_id,
                    q.wanted_id,
                    q.series_id,
                    q.issue_id,
                    q.state,
                    q.current_source,
                    q.query,
                    q.last_event,
                    q.source_order_json,
                    q.recovery_steps_json,
                    q.raw_json as queue_raw_json,
                    w.raw_json as wanted_raw_json,
                    w.status as wanted_status,
                    s.title as series,
                    s.metadata_id as mangadex_id,
                    s.raw_json as series_raw_json,
                    i.issue_number,
                    i.title as issue_title,
                    i.metadata_id as chapter_id,
                    i.raw_json as issue_raw_json
                from queue_items q
                join series s on s.id = q.series_id
                left join wanted_items w on w.id = q.wanted_id
                left join issues i on i.id = q.issue_id
                where {" and ".join(clauses)}
                order by coalesce(q.updated_at, q.created_at, 0) asc, q.id asc
                limit ?
                """,
                (*params, max(1, min(int(max_total or 10), 100))),
            ).fetchall()
        ]
    out = []
    for row in rows:
        raw = json_loads(row.get("queue_raw_json"), {}) or {}
        if wanted and normalize_key(row.get("series") or raw.get("series")) not in wanted:
            continue
        if not force and not source_order_allows_mangadex(row, raw):
            continue
        out.append(row)
    return out


def merged_payload(row):
    payload = {}
    for key in ("series_raw_json", "issue_raw_json", "wanted_raw_json", "queue_raw_json"):
        source = json_loads(row.get(key), {}) or {}
        nested = source.get("raw") if isinstance(source.get("raw"), dict) else {}
        if nested:
            payload.update(nested)
        payload.update(source)
    return payload


def safe_name(value, fallback="Unknown"):
    text = str(value or fallback).strip() or fallback
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:140] or fallback


def chapter_display(value):
    text = str(value or "").strip()
    return text or "unknown"


def destination_path(row, payload):
    root = configured_manga_root()
    series = safe_name(row.get("series") or payload.get("series") or "MangaDex")
    chapter = chapter_display(payload.get("chapter") or row.get("issue_number"))
    title = safe_name(payload.get("title") or payload.get("chapterTitle") or row.get("issue_title") or "", "")
    chapter_id = str(payload.get("mangadex_chapter_id") or payload.get("chapterId") or row.get("chapter_id") or "").strip()
    title_bits = [series, f"Chapter {chapter}"]
    if title:
        title_bits.append(title)
    if chapter_id:
        title_bits.append(f"MangaDex {chapter_id[:8]}")
    filename = safe_name(" - ".join(title_bits), "MangaDex Chapter") + ".cbz"
    return root / series / filename


def configured_manga_root():
    global MANGA_ROOT
    if completed_import is not None:
        try:
            completed_import.apply_path_provider_settings()
            MANGA_ROOT = Path(completed_import.MANGA_ROOT)
        except Exception as exc:
            log("path_settings_failed", error=f"{type(exc).__name__}: {exc}")
    return MANGA_ROOT


def mangadex_headers():
    return {"User-Agent": "InkDrop/0.1 (+local homelab manga acquisition)"}


def get_json(url, timeout=30):
    response = requests.get(url, headers=mangadex_headers(), timeout=timeout)
    response.raise_for_status()
    return response.json()


def at_home(chapter_id):
    chapter_id = str(chapter_id or "").strip()
    if not chapter_id:
        raise ValueError("MangaDex chapter id is required")
    data = get_json(f"{MANGADEX_API}/at-home/server/{chapter_id}", timeout=30)
    if data.get("result") not in {None, "ok"}:
        raise RuntimeError(f"MangaDex At-Home returned {data.get('result')}")
    chapter = data.get("chapter") if isinstance(data.get("chapter"), dict) else {}
    base_url = str(data.get("baseUrl") or "").rstrip("/")
    chapter_hash = str(chapter.get("hash") or "").strip()
    pages = chapter.get("data") if isinstance(chapter.get("data"), list) else []
    saver_pages = chapter.get("dataSaver") if isinstance(chapter.get("dataSaver"), list) else []
    if not base_url or not chapter_hash or not pages:
        raise RuntimeError("MangaDex At-Home response did not include downloadable pages")
    return {
        "base_url": base_url,
        "hash": chapter_hash,
        "pages": [str(page) for page in pages if str(page or "").strip()],
        "data_saver_pages": [str(page) for page in saver_pages if str(page or "").strip()],
    }


def page_url(info, page, data_saver=False):
    mode = "data-saver" if data_saver else "data"
    return f"{info['base_url']}/{mode}/{info['hash']}/{page}"


def page_extension(name):
    suffix = Path(str(name)).suffix.lower()
    return suffix if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"} else ".jpg"


def comicinfo_xml(row, payload):
    series = row.get("series") or payload.get("series") or "MangaDex"
    chapter = chapter_display(payload.get("chapter") or row.get("issue_number"))
    title = payload.get("title") or payload.get("chapterTitle") or row.get("issue_title") or f"Chapter {chapter}"
    volume = str(payload.get("volume") or "").strip()
    language = str(payload.get("translatedLanguage") or "").strip() or "en"
    web = f"https://mangadex.org/chapter/{payload.get('mangadex_chapter_id') or payload.get('chapterId') or row.get('chapter_id')}"
    fields = [
        ("Series", series),
        ("Title", title),
        ("Number", chapter),
        ("Format", "Manga Chapter"),
        ("LanguageISO", language),
        ("Web", web),
        ("Manga", "YesAndRightToLeft"),
    ]
    if volume:
        fields.insert(3, ("Volume", volume))
    body = "".join(f"<{key}>{xml_escape(str(value))}</{key}>" for key, value in fields if value not in (None, ""))
    return f'<?xml version="1.0" encoding="utf-8"?><ComicInfo>{body}</ComicInfo>'


def write_cbz(row, payload, dest, data_saver=False, max_pages=None):
    chapter_id = payload.get("mangadex_chapter_id") or payload.get("chapterId") or payload.get("chapter_id") or row.get("chapter_id")
    info = at_home(chapter_id)
    pages = info["data_saver_pages"] if data_saver and info["data_saver_pages"] else info["pages"]
    if max_pages:
        pages = pages[: max(1, int(max_pages))]
    if not pages:
        raise RuntimeError("MangaDex At-Home did not return page filenames")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=".inkdrop-mangadex-", suffix=".cbz", dir=str(dest.parent), delete=False) as handle:
        temp_path = Path(handle.name)
    try:
        with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for index, page in enumerate(pages, start=1):
                response = requests.get(page_url(info, page, data_saver=data_saver), headers=mangadex_headers(), timeout=45)
                response.raise_for_status()
                content = response.content
                if not content:
                    raise RuntimeError(f"MangaDex page {index} was empty")
                archive.writestr(f"{index:04d}{page_extension(page)}", content)
            archive.writestr("ComicInfo.xml", comicinfo_xml(row, payload))
        temp_path.replace(dest)
    except Exception:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise
    return {"path": str(dest), "page_count": len(pages), "size_bytes": dest.stat().st_size}


def host_path_to_kavita(path):
    path = Path(path)
    try:
        rel = path.relative_to(configured_manga_root())
    except ValueError:
        return ""
    return f"{KAVITA_MANGA_ROOT}/{rel.as_posix()}"


def kavita_visible(path):
    kavita_path = host_path_to_kavita(path)
    if not kavita_path or not KAVITA_DB.exists():
        return False
    con = sqlite3.connect(KAVITA_DB, timeout=30)
    try:
        return bool(con.execute("select 1 from MangaFile where FilePath = ? limit 1", (kavita_path,)).fetchone())
    finally:
        con.close()


def trigger_scan(dest):
    if completed_import is None:
        return {"ok": False, "reason": "completed_import_module_missing"}
    try:
        result = completed_import.trigger_kavita_scan_folder(str(Path(dest).parent), force_library_scan=True)
        return {"ok": True, **(result if isinstance(result, dict) else {"result": result})}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def wait_visible(path, timeout_seconds=90):
    deadline = time.time() + max(0, float(timeout_seconds or 0))
    while True:
        if kavita_visible(path):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(5)


def record_attempt(row, status, reason, *, dest=None, payload=None, extra=None):
    if inkdrop_state is None:
        return {"ok": False, "reason": "inkdrop_state_module_missing"}
    payload = payload if isinstance(payload, dict) else {}
    attempt = {
        "source": SOURCE,
        "provider": "MangaDex",
        "protocol": "https",
        "download_client": "MangaDex",
        "status": status,
        "reason": reason,
        "title": payload.get("searchQuery") or row.get("query") or f"{row.get('series')} {row.get('issue_number')}",
        "query": payload.get("searchQuery") or row.get("query"),
        "series": row.get("series"),
        "issue": payload.get("chapter") or row.get("issue_number"),
        "external_id": payload.get("mangadex_chapter_id") or payload.get("chapterId") or row.get("chapter_id"),
        "mangadex_id": payload.get("mangadex_id") or row.get("mangadex_id"),
        "mangadex_chapter_id": payload.get("mangadex_chapter_id") or payload.get("chapterId") or row.get("chapter_id"),
        "local_path": str(dest) if dest else None,
        "dest_path": str(dest) if dest else None,
        "staged_path": str(dest) if dest else None,
        "kind": "mangadex_direct",
        "ts": time.time(),
    }
    if extra:
        attempt.update({key: value for key, value in dict(extra).items() if value not in (None, "")})
    return inkdrop_state.record_queue_source_attempt(
        INKDROP_STATE_DB,
        row.get("queue_id"),
        {key: value for key, value in attempt.items() if value not in (None, "")},
        attempt_id=stable_attempt_id(row, status, dest),
    )


def stable_attempt_id(row, status, dest=None):
    raw = "|".join(
        str(value or "")
        for value in (SOURCE, row.get("queue_id"), row.get("chapter_id"), status, dest)
    )
    return "mangadex-direct-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def record_import(row, status, verified, dest, raw):
    if inkdrop_state is None:
        return {"ok": False, "reason": "inkdrop_state_module_missing"}
    return inkdrop_state.record_direct_import_result(
        INKDROP_STATE_DB,
        row.get("queue_id"),
        source_path=str(dest),
        dest_path=str(dest),
        source=SOURCE,
        status=status,
        verified=verified,
        imported_count=1,
        raw=raw,
    )


def process_row(row, args):
    payload = merged_payload(row)
    payload.setdefault("mangadex_id", row.get("mangadex_id"))
    payload.setdefault("mangadex_chapter_id", payload.get("chapterId") or row.get("chapter_id"))
    payload.setdefault("chapter", row.get("issue_number"))
    payload.setdefault("searchQuery", row.get("query"))
    dest = destination_path(row, payload)
    base = {
        "queue_key": row.get("queue_id"),
        "queue_id": row.get("queue_id"),
        "series": row.get("series"),
        "issue": payload.get("chapter") or row.get("issue_number"),
        "mangadex_id": payload.get("mangadex_id"),
        "mangadex_chapter_id": payload.get("mangadex_chapter_id"),
        "dest": str(dest),
        "title": payload.get("searchQuery") or row.get("query"),
    }
    if args.dry_run:
        info = at_home(payload.get("mangadex_chapter_id")) if args.preflight else {}
        return {**base, "status": "dry_run", "reason": "MangaDex direct download preview", "at_home": info}
    if dest.exists() and dest.stat().st_size > 0:
        scan = trigger_scan(dest)
        verified = wait_visible(dest, args.verify_timeout_seconds)
        status = "kavita_verified" if verified else "verification_pending"
        reason = "MangaDex file already present; Kavita verified" if verified else "MangaDex file already present; waiting for Kavita scan"
        record_attempt(row, status, reason, dest=dest, payload=payload, extra={"scan": scan, "already_present": True})
        import_result = record_import(row, status, verified, dest, {"scan": scan, "already_present": True, "payload": payload})
        return {**base, "status": status, "reason": reason, "verified": verified, "scan": scan, "import": import_result}
    record_attempt(row, "download_started", "MangaDex direct download started", dest=dest, payload=payload)
    written = write_cbz(row, payload, dest, data_saver=args.data_saver, max_pages=args.max_pages)
    scan = trigger_scan(dest)
    verified = wait_visible(dest, args.verify_timeout_seconds)
    status = "kavita_verified" if verified else "verification_pending"
    reason = "MangaDex chapter downloaded and verified in Kavita" if verified else "MangaDex chapter downloaded; waiting for Kavita verification"
    record_attempt(row, status, reason, dest=dest, payload=payload, extra={"scan": scan, **written})
    import_result = record_import(row, status, verified, dest, {"scan": scan, "written": written, "payload": payload})
    return {**base, "status": status, "reason": reason, "verified": verified, "scan": scan, "import": import_result, **written}


def run(args):
    rows = load_rows(args.series, args.max_total, force=args.force)
    actions = []
    review = []
    skipped = []
    errors = []
    per_series = {}
    for row in rows:
        series_key = normalize_key(row.get("series"))
        count = per_series.get(series_key, 0)
        if count >= args.max_per_series:
            skipped.append({"queue_id": row.get("queue_id"), "series": row.get("series"), "reason": "max_per_series_reached"})
            continue
        try:
            result = process_row(row, args)
            actions.append(result)
            per_series[series_key] = count + 1
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            log("mangadex_direct_error", queue_id=row.get("queue_id"), series=row.get("series"), error=reason)
            try:
                record_attempt(row, "download_api_error", reason, payload=merged_payload(row))
            except Exception:
                pass
            review.append(
                {
                    "queue_key": row.get("queue_id"),
                    "queue_id": row.get("queue_id"),
                    "series": row.get("series"),
                    "issue": row.get("issue_number"),
                    "reason": "mangadex_direct_error",
                    "error": reason,
                    "source": SOURCE,
                }
            )
            errors.append({"queue_id": row.get("queue_id"), "error": reason})
    result = {
        "ok": True,
        "source": SOURCE,
        "dry_run": bool(args.dry_run),
        "rows_considered": len(rows),
        "actions": actions,
        "review": review,
        "skipped": skipped,
        "errors": errors,
        "policy": "MangaDex direct downloads build CBZ files into the Manga library, trigger Kavita scans, and record queue/import state in InkDrop Core.",
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return result


def main():
    parser = argparse.ArgumentParser(description="InkDrop MangaDex direct downloader")
    parser.add_argument("--series", action="append", default=[], help="Limit to exact series title")
    parser.add_argument("--max-total", type=int, default=6)
    parser.add_argument("--max-per-series", type=int, default=3)
    parser.add_argument("--verify-timeout-seconds", type=int, default=90)
    parser.add_argument("--data-saver", action="store_true", help="Use MangaDex data-saver pages")
    parser.add_argument("--max-pages", type=int, default=0, help="Debug/testing page cap")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--preflight", action="store_true", help="In dry-run, call At-Home to confirm page availability")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    args.max_total = max(1, min(int(args.max_total or 1), 50))
    args.max_per_series = max(1, min(int(args.max_per_series or 1), 20))
    args.verify_timeout_seconds = max(0, min(int(args.verify_timeout_seconds or 0), 300))
    args.max_pages = max(0, min(int(args.max_pages or 0), 500))
    run(args)


if __name__ == "__main__":
    main()
