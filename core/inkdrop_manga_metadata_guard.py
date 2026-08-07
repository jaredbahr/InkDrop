#!/usr/bin/env python3
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))

import argparse
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import time
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

# ComicInfo.xml is metadata -- a handful of short tags. Reading it unbounded on
# every guard pass is what makes a single poisoned archive a crash loop rather
# than a one-off failure: this sweep re-walks the library on a timer, so the
# same hostile file gets re-read until an operator finds and deletes it. The
# container has no memory limit, so an OOM here takes down whatever else shares
# the host, not just InkDrop.
MAX_COMICINFO_BYTES = 1 * 1024 * 1024


def read_bounded_comicinfo(archive, name, limit=MAX_COMICINFO_BYTES):
    """Read a ComicInfo member, refusing anything past `limit`.

    Consults the declared size first so a decompression bomb is refused before a
    byte is inflated, then requires the inflated length to match what the header
    claimed -- which catches a member that understates its real size.
    """
    info = archive.getinfo(name) if isinstance(name, str) else name
    if info.file_size > limit:
        raise ValueError("comicinfo_exceeds_bounded_limit")
    with archive.open(info) as member:
        data = member.read(limit + 1)
    if len(data) != info.file_size:
        raise ValueError("comicinfo_member_size_mismatch")
    return data


def env_path(name, default):
    return Path(os.environ.get(name) or default)


def compatible_env_path(name, default, *legacy_names):
    for key in (name, *legacy_names):
        value = str(os.environ.get(key) or "").strip()
        if value:
            return Path(value)
    return Path(default)


MANGA_ROOT = env_path("INKDROP_MANGA_ROOT", "/library/manga")
# Kavita's own mount point for the manga library, not InkDrop's. Every other
# module that reads Kavita-side paths (inkdrop_completed_import.py,
# inkdrop_mangadex_direct.py, inkdrop_manual_source_autoresolve.py,
# inkdrop_series_autopilot.py, inkdrop_web.py, inkdrop_state.py) honors this
# same override; this one used to hardcode the default instead.
KAVITA_MANGA_ROOT = os.environ.get("INKDROP_KAVITA_MANGA_ROOT") or "/data/manga"
STATE_DIR = compatible_env_path("INKDROP_STATE_DIR", "/state", "KAVITA_ACQUIRE_STATE_DIR")
STATUS_PATH = env_path("INKDROP_MANGA_METADATA_GUARD_STATUS_PATH", STATE_DIR / "manga-metadata-guard-status.json")
KAVITA_DB = compatible_env_path(
    "INKDROP_KAVITA_DB",
    STATE_DIR / "external/kavita.db",
    "INKDROP_KAVITA_DB_PATH",
)
BACKUP_ROOT = env_path("INKDROP_BACKUP_DIR", STATE_DIR / "backups")
IMPORTER_PATH = env_path("INKDROP_COMPLETED_IMPORT_SCRIPT", Path(__file__).resolve().with_name("inkdrop_completed_import.py"))
OUTSIDE_LIBRARY_ROOT = env_path("INKDROP_INTERNAL_LIBRARY_ROOT", STATE_DIR / "library-internal")

INTERNAL_PREFIX = "_"
MIN_REPAIR_AGE_SECONDS = int(os.environ.get("INKDROP_MANGA_METADATA_GUARD_MIN_REPAIR_AGE_SECONDS", "600") or "600")
MAX_ARCHIVES_DEFAULT = int(os.environ.get("INKDROP_MANGA_METADATA_GUARD_MAX_ARCHIVES", "250") or "250")
SKIP_IF_STATUS_YOUNGER_SECONDS = int(os.environ.get("INKDROP_MANGA_METADATA_GUARD_SKIP_IF_STATUS_YOUNGER_SECONDS", "900") or "900")
MANGA_LIBRARY_NAME = "Manga"


def now_slug():
    return time.strftime("%Y%m%d-%H%M%S")


def json_dump(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def xml_text(value):
    return str(value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def xml_node(name, value):
    if value is None or value == "":
        return ""
    return f"  <{name}>{xml_text(value)}</{name}>\n"


def normalize_number(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return None
    number = match.group(0)
    if "." in number:
        return f"{float(number):06.2f}".rstrip("0").rstrip(".")
    return f"{int(number):03d}"


def volume_number_from_name(path):
    text = path.name
    patterns = [
        r"\bv(?:ol(?:ume)?)?[\s._-]*(\d{1,4})(?:\D|$)",
        r"\bvolume[\s._-]*(\d{1,4})(?:\D|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return normalize_number(match.group(1))
    return None


def year_from_text(text):
    match = re.search(r"\b(19\d{2}|20\d{2})\b", str(text or ""))
    return match.group(1) if match else ""


def obvious_volume_context(path):
    number = volume_number_from_name(path)
    try:
        rel_parts = path.relative_to(MANGA_ROOT).parts
    except ValueError:
        return None
    if not number or not rel_parts:
        return None
    if len(rel_parts) == 2:
        return {"series": rel_parts[0], "number": number, "year": year_from_text(path.name)}
    if len(rel_parts) >= 3:
        parent = path.parent.name
        parent_number = volume_number_from_name(path.parent)
        if parent_number == number:
            return {
                "series": rel_parts[0],
                "number": number,
                "year": year_from_text(parent) or year_from_text(path.name),
            }
    return None


def path_has_internal_segment(path):
    try:
        rel = Path(path).relative_to(MANGA_ROOT)
    except ValueError:
        return False
    return any(part.startswith(INTERNAL_PREFIX) for part in rel.parts)


def top_level_internal_dirs():
    if not MANGA_ROOT.exists():
        return []
    return sorted(
        path for path in MANGA_ROOT.iterdir()
        if path.is_dir() and path.name.startswith(INTERNAL_PREFIX)
    )


def archive_info(path):
    info = {
        "path": str(path),
        "exists": path.exists(),
        "is_cbz": path.suffix.lower() == ".cbz",
        "comicinfo": False,
        "series": None,
        "title": None,
        "number": None,
        "volume": None,
        "format": None,
        "language": None,
        "error": None,
    }
    if not info["exists"] or not info["is_cbz"]:
        return info
    try:
        with zipfile.ZipFile(path, "r") as archive:
            name = next((n for n in archive.namelist() if n.lower().endswith("comicinfo.xml")), None)
            if not name:
                return info
            info["comicinfo"] = True
            root = ET.fromstring(read_bounded_comicinfo(archive, name))
            for tag in ["Series", "Title", "Number", "Volume", "Format", "LanguageISO"]:
                node = root.find(tag)
                if node is not None and node.text is not None:
                    info[tag.lower() if tag != "LanguageISO" else "language"] = node.text.strip()
    except Exception as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info


def manga_volume_comicinfo(series, number, year=""):
    display = int(float(number))
    return (
        "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n"
        "<ComicInfo>\n"
        f"{xml_node('Series', series)}"
        f"{xml_node('Title', f'{series} v{display:02d}')}"
        f"{xml_node('Volume', display)}"
        f"{xml_node('Year', year)}"
        "  <Format>Manga</Format>\n"
        "  <LanguageISO>en</LanguageISO>\n"
        "</ComicInfo>\n"
    )


def remove_number_from_volume_comicinfo(path, info):
    with zipfile.ZipFile(path, "r") as src:
        comicinfo_name = next((n for n in src.namelist() if n.lower().endswith("comicinfo.xml")), None)
        if not comicinfo_name:
            return False
        root = ET.fromstring(read_bounded_comicinfo(src, comicinfo_name))
        number_node = root.find("Number")
        if number_node is not None:
            root.remove(number_node)
        if root.find("Volume") is None and info.get("volume"):
            volume = ET.SubElement(root, "Volume")
            volume.text = str(info["volume"])
        if root.find("Format") is None:
            fmt = ET.SubElement(root, "Format")
            fmt.text = "Manga"
        if root.find("LanguageISO") is None:
            lang = ET.SubElement(root, "LanguageISO")
            lang.text = "en"
        xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        tmp = path.with_suffix(path.suffix + ".metadata-tmp")
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as dst:
            for member in src.infolist():
                if member.filename.lower().endswith("comicinfo.xml"):
                    continue
                dst.writestr(member, src.read(member.filename))
            dst.writestr("ComicInfo.xml", xml_bytes)
    tmp.replace(path)
    return True


def add_volume_comicinfo(path, series, number, year=""):
    with zipfile.ZipFile(path, "a", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        if any(name.lower().endswith("comicinfo.xml") for name in archive.namelist()):
            return False
        archive.writestr("ComicInfo.xml", manga_volume_comicinfo(series, number, year))
    return True


def replace_volume_comicinfo(path, series, number, year=""):
    tmp = path.with_suffix(path.suffix + ".metadata-tmp")
    with zipfile.ZipFile(path, "r") as src, zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as dst:
        for member in src.infolist():
            if member.filename.lower().endswith("comicinfo.xml"):
                continue
            dst.writestr(member, src.read(member.filename))
        dst.writestr("ComicInfo.xml", manga_volume_comicinfo(series, number, year))
    tmp.replace(path)
    return True


def load_importer():
    spec = importlib.util.spec_from_file_location("inkdrop_completed_import", IMPORTER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def trigger_scan(folder, errors):
    try:
        module = load_importer()
        return module.trigger_kavita_scan_folder(folder)
    except Exception as exc:
        errors.append({"scan_folder": str(folder), "error": f"{type(exc).__name__}: {exc}"})
        return None


def stale_duplicate_chapter_rows():
    if not KAVITA_DB.exists():
        return []
    query = """
    select c_bad.Id as chapter_id, s.Name as series, c_bad.Number as number, mf_bad.FilePath as file_path
    from Chapter c_bad
    join MangaFile mf_bad on mf_bad.ChapterId = c_bad.Id
    join Volume v on v.Id = c_bad.VolumeId
    join Series s on s.Id = v.SeriesId
    join Library l on l.Id = s.LibraryId
    join Chapter c_ok on c_ok.VolumeId = c_bad.VolumeId and c_ok.Number = '-100000'
    join MangaFile mf_ok on mf_ok.ChapterId = c_ok.Id and mf_ok.FilePath = mf_bad.FilePath
    where l.Name = ? and c_bad.Number != '-100000'
    order by s.Name, c_bad.Number, mf_bad.FilePath
    """
    conn = sqlite3.connect(KAVITA_DB)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(query, (MANGA_LIBRARY_NAME,)).fetchall()]
    finally:
        conn.close()


def internal_kavita_file_rows():
    if not KAVITA_DB.exists():
        return []
    root_prefix = str(KAVITA_MANGA_ROOT).rstrip("/") + "/"
    query = """
    select mf.Id as manga_file_id, c.Id as chapter_id, v.Id as volume_id, s.Id as series_id,
           s.Name as series, c.Number as chapter_number, mf.FilePath as file_path
    from MangaFile mf
    join Chapter c on c.Id = mf.ChapterId
    join Volume v on v.Id = c.VolumeId
    join Series s on s.Id = v.SeriesId
    join Library l on l.Id = s.LibraryId
    where l.Name = ?
      and substr(coalesce(mf.FilePath, ''), 1, ?) = ?
      and substr(coalesce(mf.FilePath, ''), ? + 1, 1) = ?
    order by s.Name, c.Number, mf.FilePath
    """
    conn = sqlite3.connect(KAVITA_DB)
    conn.row_factory = sqlite3.Row
    try:
        prefix_len = len(root_prefix)
        params = (MANGA_LIBRARY_NAME, prefix_len, root_prefix, prefix_len, INTERNAL_PREFIX)
        return [dict(row) for row in conn.execute(query, params).fetchall()]
    finally:
        conn.close()


def prune_internal_kavita_rows(rows, backup_dir):
    if not rows:
        return {"manga_files_deleted": 0, "chapters_deleted": 0, "volumes_deleted": 0}
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / "kavita.db.bak-before-internal-path-prune"
    if not backup.exists():
        shutil.copy2(KAVITA_DB, backup)
    conn = sqlite3.connect(KAVITA_DB)
    try:
        file_ids = [(int(row["manga_file_id"]),) for row in rows]
        conn.executemany("delete from MangaFile where Id = ?", file_ids)
        chapters_deleted = conn.execute(
            """
            delete from Chapter
            where Id in (
              select c.Id
              from Chapter c
              join Volume v on v.Id = c.VolumeId
              join Series s on s.Id = v.SeriesId
              join Library l on l.Id = s.LibraryId
              where l.Name = ?
                and not exists (select 1 from MangaFile mf where mf.ChapterId = c.Id)
            )
            """,
            (MANGA_LIBRARY_NAME,),
        ).rowcount
        volumes_deleted = conn.execute(
            """
            delete from Volume
            where Id in (
              select v.Id
              from Volume v
              join Series s on s.Id = v.SeriesId
              join Library l on l.Id = s.LibraryId
              where l.Name = ?
                and not exists (select 1 from Chapter c where c.VolumeId = v.Id)
            )
            """,
            (MANGA_LIBRARY_NAME,),
        ).rowcount
        conn.commit()
        return {
            "manga_files_deleted": len(file_ids),
            "chapters_deleted": int(chapters_deleted or 0),
            "volumes_deleted": int(volumes_deleted or 0),
        }
    finally:
        conn.close()


def delete_stale_duplicate_chapter_rows(rows, backup_dir):
    if not rows:
        return 0
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / "kavita.db.bak-before-stale-chapter-cleanup"
    if not backup.exists():
        shutil.copy2(KAVITA_DB, backup)
    ids = [int(row["chapter_id"]) for row in rows]
    conn = sqlite3.connect(KAVITA_DB)
    try:
        conn.executemany("delete from Chapter where Id = ?", [(chapter_id,) for chapter_id in ids])
        conn.commit()
    finally:
        conn.close()
    return len(ids)


def move_internal_dirs(repair, run_id):
    moved = []
    candidates = top_level_internal_dirs()
    if not repair:
        return {"candidates": [str(path) for path in candidates], "moved": moved}
    dest_root = OUTSIDE_LIBRARY_ROOT / run_id
    dest_root.mkdir(parents=True, exist_ok=True)
    for src in candidates:
        dest = dest_root / src.name
        if dest.exists():
            dest = dest_root / f"{src.name}-{int(time.time())}"
        shutil.move(str(src), str(dest))
        moved.append({"source": str(src), "dest": str(dest)})
    return {"candidates": [str(path) for path in candidates], "moved": moved}


def inspect_and_repair_archives(repair, *, max_archives=None):
    stats = {
        "scanned": 0,
        "bad_volume_number_nodes": [],
        "repaired_volume_number_nodes": [],
        "missing_comicinfo_obvious": [],
        "added_comicinfo": [],
        "missing_comicinfo_ambiguous": [],
        "archive_errors": [],
        "repaired_malformed_comicinfo": [],
        "skipped_internal": 0,
        "skipped_recent": 0,
        "scan_limited": False,
    }
    cutoff = time.time() - MIN_REPAIR_AGE_SECONDS
    for path in sorted(MANGA_ROOT.rglob("*.cbz")):
        if max_archives is not None and stats["scanned"] >= max_archives:
            stats["scan_limited"] = True
            break
        if path_has_internal_segment(path):
            stats["skipped_internal"] += 1
            continue
        try:
            if path.stat().st_mtime > cutoff:
                stats["skipped_recent"] += 1
                continue
        except OSError:
            continue
        stats["scanned"] += 1
        info = archive_info(path)
        if info.get("error"):
            context = obvious_volume_context(path)
            if context:
                item = {"path": str(path), "error": info.get("error"), **context}
                if repair:
                    try:
                        replace_volume_comicinfo(path, context["series"], context["number"], context.get("year", ""))
                        stats["repaired_malformed_comicinfo"].append(item)
                    except Exception as exc:
                        item["repair_error"] = f"{type(exc).__name__}: {exc}"
                        stats["archive_errors"].append(item)
                else:
                    stats["archive_errors"].append(info)
                continue
            stats["archive_errors"].append(info)
            continue
        fmt = (info.get("format") or "").strip().lower()
        if info.get("comicinfo") and fmt == "manga" and info.get("number") and info.get("volume"):
            stats["bad_volume_number_nodes"].append(info)
            if repair:
                remove_number_from_volume_comicinfo(path, info)
                stats["repaired_volume_number_nodes"].append(str(path))
            continue
        if not info.get("comicinfo"):
            context = obvious_volume_context(path)
            if context:
                item = {"path": str(path), **context}
                stats["missing_comicinfo_obvious"].append(item)
                if repair:
                    add_volume_comicinfo(path, context["series"], context["number"], context.get("year", ""))
                    stats["added_comicinfo"].append(item)
            else:
                stats["missing_comicinfo_ambiguous"].append({"path": str(path), "number": volume_number_from_name(path)})
    return stats


def status_recent_enough(path, max_age_seconds):
    if not max_age_seconds:
        return False
    try:
        return Path(path).exists() and time.time() - Path(path).stat().st_mtime < max_age_seconds
    except OSError:
        return False


def main():
    parser = argparse.ArgumentParser(description="Guard InkDrop manga volume/chapter metadata and Kavita scan state")
    parser.add_argument("--repair", action="store_true", help="Apply safe repairs and move internal folders out of the Manga library root")
    parser.add_argument("--status-json", default=str(STATUS_PATH))
    parser.add_argument("--no-scan", action="store_true")
    parser.add_argument("--max-archives", type=int, default=MAX_ARCHIVES_DEFAULT, help="Maximum archives to inspect per run; use 0 for no limit")
    parser.add_argument("--skip-if-status-younger-seconds", type=int, default=SKIP_IF_STATUS_YOUNGER_SECONDS)
    args = parser.parse_args()

    started = time.time()
    run_id = now_slug()
    status_path = Path(args.status_json)
    if status_recent_enough(status_path, args.skip_if_status_younger_seconds):
        status = {
            "updated_at": started,
            "updated_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
            "state": "SKIPPED",
            "reason": "recent_status",
            "status_path": str(status_path),
            "skip_if_status_younger_seconds": int(args.skip_if_status_younger_seconds or 0),
        }
        print(json.dumps(status, sort_keys=True))
        return
    backup_dir = BACKUP_ROOT / f"{run_id}-inkdrop-manga-guard"
    errors = []

    internal = move_internal_dirs(args.repair, run_id)
    max_archives = int(args.max_archives or 0)
    archive_stats = inspect_and_repair_archives(args.repair, max_archives=max_archives if max_archives > 0 else None)
    internal_kavita_rows = internal_kavita_file_rows()
    internal_kavita_pruned = prune_internal_kavita_rows(internal_kavita_rows, backup_dir) if args.repair else {
        "manga_files_deleted": 0,
        "chapters_deleted": 0,
        "volumes_deleted": 0,
    }
    stale_rows = stale_duplicate_chapter_rows()
    stale_deleted = delete_stale_duplicate_chapter_rows(stale_rows, backup_dir) if args.repair else 0

    scans = []
    if args.repair and not args.no_scan:
        if (
            internal["moved"]
            or archive_stats["repaired_volume_number_nodes"]
            or archive_stats["added_comicinfo"]
            or stale_deleted
            or internal_kavita_pruned["manga_files_deleted"]
        ):
            scans.append({"folder": str(MANGA_ROOT), "result": trigger_scan(MANGA_ROOT, errors)})

    state = "OK"
    if errors or archive_stats["archive_errors"]:
        state = "WARN"
    if archive_stats["bad_volume_number_nodes"] and not args.repair:
        state = "WATCH"
    if internal["candidates"] and not args.repair:
        state = "WATCH"
    if stale_rows and not args.repair:
        state = "WATCH"
    if internal_kavita_rows and not args.repair:
        state = "WATCH"

    status = {
        "updated_at": started,
        "updated_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "state": state,
        "repair": bool(args.repair),
        "manga_root": str(MANGA_ROOT),
        "outside_library_root": str(OUTSIDE_LIBRARY_ROOT),
        "internal_folder_candidates": internal["candidates"],
        "internal_folders_moved": internal["moved"],
        "archives_scanned": archive_stats["scanned"],
        "archive_scan_limited": archive_stats["scan_limited"],
        "max_archives": max_archives,
        "bad_volume_number_count": len(archive_stats["bad_volume_number_nodes"]),
        "bad_volume_number_samples": archive_stats["bad_volume_number_nodes"][:10],
        "repaired_volume_number_count": len(archive_stats["repaired_volume_number_nodes"]),
        "repaired_volume_number_paths": archive_stats["repaired_volume_number_nodes"][:20],
        "missing_comicinfo_obvious_count": len(archive_stats["missing_comicinfo_obvious"]),
        "added_comicinfo_count": len(archive_stats["added_comicinfo"]),
        "missing_comicinfo_ambiguous_count": len(archive_stats["missing_comicinfo_ambiguous"]),
        "missing_comicinfo_ambiguous_samples": archive_stats["missing_comicinfo_ambiguous"][:10],
        "skipped_internal_archives": archive_stats["skipped_internal"],
        "skipped_recent_archives": archive_stats["skipped_recent"],
        "archive_error_count": len(archive_stats["archive_errors"]),
        "archive_error_samples": archive_stats["archive_errors"][:10],
        "repaired_malformed_comicinfo_count": len(archive_stats["repaired_malformed_comicinfo"]),
        "repaired_malformed_comicinfo": archive_stats["repaired_malformed_comicinfo"][:10],
        "stale_duplicate_chapter_row_count": len(stale_rows),
        "stale_duplicate_chapter_rows_deleted": stale_deleted,
        "stale_duplicate_chapter_samples": stale_rows[:20],
        "internal_kavita_file_row_count": len(internal_kavita_rows),
        "internal_kavita_file_rows_deleted": internal_kavita_pruned["manga_files_deleted"],
        "internal_kavita_chapters_deleted": internal_kavita_pruned["chapters_deleted"],
        "internal_kavita_volumes_deleted": internal_kavita_pruned["volumes_deleted"],
        "internal_kavita_file_samples": internal_kavita_rows[:20],
        "scans": scans,
        "errors": errors,
        "elapsed_seconds": round(time.time() - started, 3),
    }
    json_dump(status_path, status)
    print(json.dumps(status, sort_keys=True))


if __name__ == "__main__":
    main()
