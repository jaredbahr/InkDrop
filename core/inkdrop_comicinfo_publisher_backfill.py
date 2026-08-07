#!/usr/bin/env python3
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))
_HERE = _Path(__file__).resolve().parent
if str(_HERE) not in _sys.path:
    _sys.path.insert(0, str(_HERE))

import argparse
import json
import os
import sqlite3
import time
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import inkdrop_completed_import as importer
from inkdrop_manga_metadata_guard import read_bounded_comicinfo, env_path, STATE_DIR

INKDROP_STATE_DB = importer.INKDROP_STATE_DB
BACKUP_ROOT = env_path("INKDROP_BACKUP_DIR", STATE_DIR / "backups")
INTERNAL_PREFIX = "_"
# Skip anything the import pipeline could still be touching -- a file this
# fresh might be mid-write from an active import, and rewriting it out from
# under that process would corrupt both the archive and the import result.
MIN_FILE_AGE_SECONDS = int(os.environ.get("INKDROP_COMICINFO_BACKFILL_MIN_AGE_SECONDS", "600") or "600")


def now_slug():
    return time.strftime("%Y%m%d-%H%M%S")


def library_series_rows(series_filter=None):
    filters = {str(item).strip().lower() for item in (series_filter or []) if str(item).strip()}
    conn = sqlite3.connect(f"file:{INKDROP_STATE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            select id, title, media_type, publisher, library_path, library_adapter_path
            from series
            where coalesce(publisher, '') != ''
              and (coalesce(library_path, '') != '' or coalesce(library_adapter_path, '') != '')
            order by title
            """
        ).fetchall()
    finally:
        conn.close()
    out = [dict(row) for row in rows]
    if filters:
        out = [row for row in out if str(row.get("title") or "").strip().lower() in filters]
    return out


def resolve_folder(row):
    library_path = str(row.get("library_path") or "").strip()
    if library_path:
        return Path(library_path)
    adapter = importer.host_path_from_kavita_path(row.get("library_adapter_path"))
    return Path(adapter) if adapter else None


def path_has_internal_segment(path, root):
    try:
        rel = Path(path).relative_to(root)
    except ValueError:
        return False
    return any(part.startswith(INTERNAL_PREFIX) for part in rel.parts)


def iter_archives(folder, cutoff):
    if not folder or not folder.exists():
        return
    # Not sorted() -- a single large series folder over network-attached
    # storage can take minutes to fully enumerate, and sorted() must consume
    # the whole generator before yielding anything, which turns that into a
    # silent multi-minute stall with zero progress output.
    for path in folder.rglob("*.cbz"):
        if path_has_internal_segment(path, folder):
            continue
        if path.name.endswith(".tmp") or path.name.endswith("-tmp") or ".metadata-tmp" in path.name:
            continue
        try:
            if path.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        yield path


def inspect_archive(path):
    """Read-only: classify the archive and return the original ComicInfo bytes for backup."""
    try:
        with zipfile.ZipFile(path, "r") as archive:
            name = next((n for n in archive.namelist() if n.lower().endswith("comicinfo.xml")), None)
            if not name:
                return {"action": "missing_comicinfo", "raw": None, "error": None}
            raw = read_bounded_comicinfo(archive, name)
    except Exception as exc:
        return {"action": "archive_error", "raw": None, "error": f"{type(exc).__name__}: {exc}"}
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        return {"action": "malformed_comicinfo", "raw": raw, "error": f"{type(exc).__name__}: {exc}"}
    node = root.find("Publisher")
    if node is not None and (node.text or "").strip():
        return {"action": "already_present", "raw": raw, "error": None}
    return {"action": "needs_publisher", "raw": raw, "error": None}


def write_publisher(path, raw_original, publisher):
    root = ET.fromstring(raw_original)
    node = ET.SubElement(root, "Publisher")
    node.text = publisher
    format_node = root.find("Format")
    if format_node is not None:
        root.remove(node)
        root.insert(list(root).index(format_node), node)
    xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    tmp = path.with_name(path.name + ".publisher-backfill-tmp")
    tmp.unlink(missing_ok=True)
    with zipfile.ZipFile(path, "r") as src, zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as dst:
        for info in src.infolist():
            if info.filename.lower().endswith("comicinfo.xml"):
                continue
            dst.writestr(info, src.read(info.filename))
        dst.writestr("ComicInfo.xml", xml_bytes)
    tmp.replace(path)
    return xml_bytes


def run(args):
    started = time.time()
    run_id = now_slug()
    cutoff = time.time() - MIN_FILE_AGE_SECONDS
    rows = library_series_rows(args.series)

    backup_dir = None
    backup_fp = None
    if args.apply:
        backup_dir = BACKUP_ROOT / f"{run_id}-comicinfo-publisher-backfill"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_fp = open(backup_dir / "original-comicinfo.jsonl", "a", encoding="utf-8")

    stats = {
        "run_id": run_id,
        "dry_run": not args.apply,
        "series_matched": len(rows),
        "series_skipped_no_folder": [],
        "archives_scanned": 0,
        "publisher_added": 0,
        "already_present": 0,
        "missing_comicinfo": 0,
        "malformed_comicinfo": 0,
        "archive_errors": [],
        "publisher_added_samples": [],
        "max_archives": args.max_archives,
        "scan_limited": False,
    }

    try:
        for series_index, row in enumerate(rows, 1):
            folder = resolve_folder(row)
            if not folder or not folder.exists():
                stats["series_skipped_no_folder"].append({"title": row.get("title"), "id": row.get("id")})
                continue
            publisher = str(row.get("publisher") or "").strip()
            if not publisher:
                continue
            print(
                f"[{series_index}/{len(rows)}] scanning {row.get('title')!r} "
                f"({stats['archives_scanned']} archives so far, {round(time.time() - started, 1)}s elapsed)",
                file=_sys.stderr,
                flush=True,
            )
            for path in iter_archives(folder, cutoff):
                if args.max_archives and stats["archives_scanned"] >= args.max_archives:
                    stats["scan_limited"] = True
                    break
                stats["archives_scanned"] += 1
                if stats["archives_scanned"] % args.progress_every == 0:
                    print(
                        f"... {stats['archives_scanned']} scanned, "
                        f"{stats['publisher_added']} {'would add' if not args.apply else 'added'}, "
                        f"{round(time.time() - started, 1)}s elapsed",
                        file=_sys.stderr,
                        flush=True,
                    )
                info = inspect_archive(path)
                action = info["action"]
                if action == "already_present":
                    stats["already_present"] += 1
                    continue
                if action == "missing_comicinfo":
                    stats["missing_comicinfo"] += 1
                    continue
                if action == "malformed_comicinfo":
                    stats["malformed_comicinfo"] += 1
                    stats["archive_errors"].append(
                        {"path": str(path), "reason": "malformed_comicinfo", "detail": info["error"]}
                    )
                    continue
                if action == "archive_error":
                    stats["archive_errors"].append(
                        {"path": str(path), "reason": "archive_error", "detail": info["error"]}
                    )
                    continue
                # action == "needs_publisher"
                if args.apply:
                    entry = {
                        "path": str(path),
                        "series_id": row.get("id"),
                        "original_comicinfo_xml": info["raw"].decode("utf-8", errors="replace"),
                    }
                    backup_fp.write(json.dumps(entry) + "\n")
                    backup_fp.flush()
                    os.fsync(backup_fp.fileno())
                    try:
                        write_publisher(path, info["raw"], publisher)
                    except Exception as exc:
                        stats["archive_errors"].append(
                            {"path": str(path), "reason": "write_failed", "detail": f"{type(exc).__name__}: {exc}"}
                        )
                        continue
                stats["publisher_added"] += 1
                if len(stats["publisher_added_samples"]) < 25:
                    stats["publisher_added_samples"].append({"path": str(path), "publisher": publisher})
            if stats["scan_limited"]:
                break
    finally:
        if backup_fp:
            backup_fp.close()
            stats["backup_manifest"] = str(backup_dir / "original-comicinfo.jsonl")

    stats["elapsed_seconds"] = round(time.time() - started, 3)
    print(json.dumps(stats, indent=2, sort_keys=True))
    return stats


def main():
    parser = argparse.ArgumentParser(description="Backfill Publisher into already-imported ComicInfo.xml files")
    parser.add_argument("--apply", action="store_true", help="Write changes; default is dry-run/report-only")
    parser.add_argument("--series", nargs="*", default=None, help="Limit to these series titles (exact, case-insensitive)")
    parser.add_argument("--max-archives", type=int, default=0, help="Stop after this many archives inspected; 0 = no limit")
    parser.add_argument("--progress-every", type=int, default=50, help="Print a progress line to stderr every N archives")
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
