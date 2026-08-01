#!/usr/bin/env python3
"""Register a pre-existing comic/manga library without downloading or moving anything.

Adoption has two gated phases:

  1. `build_adoption_plan` (read-only) walks a folder the user already owns, groups
     files by top-level series folder, guesses series/issue/volume identity from
     filenames, and checks the guess against series InkDrop already knows about.
     Nothing is written. Callers may pass `search_fn` to enrich each unmatched
     folder with metadata-provider candidates (e.g. inkdrop_web.comicvine_search_volumes
     or inkdrop_web.mangadex_search_manga) so a human can pick the right series.

  2. `apply_adoption_folder` (write) takes one folder plus a human-confirmed
     `series_selection` and an explicit `confirmed_files` list, and registers those
     files as already-owned (`wanted_items.status = "satisfied"`). It never touches
     the filesystem and never creates queue/download work, so autopilot and search
     workers will not see adopted issues as wanted. A file that was not explicitly
     confirmed is left alone rather than guessed at write time.

This module only depends on inkdrop_state (pure helpers + upsert/connect primitives)
and the standard library, so it can be exercised standalone via the CLI below without
wiring into inkdrop_web.py.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

import inkdrop_state


# ---------------------------------------------------------------------------
# Filename identity parsing
#
# These mirror the proven regex shapes already used by the download-import path
# (inkdrop_completed_import.extract_issue_number, inkdrop_missing_acquire.parse_series_issue,
# inkdrop_manga_metadata_guard.volume_number_from_name/year_from_text) but are kept
# local rather than imported, since those files are large and under active edit
# elsewhere in this repo and none of them expose a stable "parse this arbitrary
# on-disk file" entry point independent of an active download/target context.
# ---------------------------------------------------------------------------

_COMIC_ISSUE_PATTERNS = (
    r"(?:^|[\s._\-\(\[])#\s*(\d{1,5}(?:\.\d+)?)\b",
    r"\bissue[\s._-]*(\d{1,5}(?:\.\d+)?)\b",
    r"\b(?:volume|vol|v)[\s._-]*0*1[\s._-]*issue[\s._-]*(\d{1,5}(?:\.\d+)?)\b",
    r"\b(?:volume|vol|v)[\s._-]*(?:19|20)\d{2}[\s._-]+0*(\d{1,3}(?:\.\d+)?)\b",
    r"\b(?:volume|vol|v)[\s._-]*(?!(?:19|20)\d{2}\b)(\d{1,5}(?:\.\d+)?)\b",
    r"(?:^|[\s._-])0*(\d{1,5}(?:\.\d+)?)\b",
)

_MANGA_VOLUME_PATTERNS = (
    r"\bv(?:ol(?:ume)?)?[\s._-]*(\d{1,4})(?:\D|$)",
    r"\bvolume[\s._-]*(\d{1,4})(?:\D|$)",
)

_MANGA_CHAPTER_PATTERNS = (
    r"\bc(?:h(?:apter)?)?[\s._-]*(\d{1,5}(?:\.\d+)?)(?:\D|$)",
    r"\bchapter[\s._-]*(\d{1,5}(?:\.\d+)?)(?:\D|$)",
)

_YEAR_PATTERN = r"\b(19\d{2}|20\d{2})\b"

_EDITION_NOISE = (
    r"\b(?:library|deluxe|expanded|anniversary|collector'?s?)\s+edition\b",
    r"\b(?:hardcover|paperback|trade paperback|tpb|hc|digital|scan|webrip)\b",
)


def _strip_edition_noise(title):
    text = str(title or "")
    for pattern in _EDITION_NOISE:
        text = re.sub(pattern, " ", text, flags=re.I)
    text = re.sub(r"[\[\(][^\]\)]*[\]\)]", " ", text)
    text = text.replace(":", " ").replace("-", " ").replace("_", " ").replace(".", " ")
    return re.sub(r"\s+", " ", text).strip()


def parse_comic_identity(path):
    """Best-effort (series_title, issue_number) guess for a single comic file."""
    stem = Path(path).stem
    parent = Path(path).parent.name
    haystack = f"{stem} {parent}"
    number = None
    for pattern in _COMIC_ISSUE_PATTERNS:
        match = re.search(pattern, haystack, re.I)
        if match:
            try:
                number = float(match.group(1))
            except ValueError:
                continue
            title_guess = _strip_edition_noise(haystack[: match.start()])
            if number >= 0:
                break
    else:
        title_guess = _strip_edition_noise(stem)
    if number is None:
        title_guess = _strip_edition_noise(stem)
    return {
        "title_guess": title_guess or None,
        "issue_number": number,
        "unit_type": "issue",
    }


def parse_manga_identity(path):
    """Best-effort (series_title, volume_number|chapter_number) guess for one manga file."""
    stem = Path(path).stem
    parent = Path(path).parent.name
    haystack = f"{stem} {parent}"
    volume = None
    chapter = None
    for pattern in _MANGA_VOLUME_PATTERNS:
        match = re.search(pattern, haystack, re.I)
        if match:
            volume = match.group(1).lstrip("0") or "0"
            break
    for pattern in _MANGA_CHAPTER_PATTERNS:
        match = re.search(pattern, haystack, re.I)
        if match:
            chapter = match.group(1)
            break
    cut = len(haystack)
    for pattern in (_MANGA_VOLUME_PATTERNS[0], _MANGA_CHAPTER_PATTERNS[0]):
        match = re.search(pattern, haystack, re.I)
        if match:
            cut = min(cut, match.start())
    title_guess = _strip_edition_noise(haystack[:cut]) or _strip_edition_noise(stem)
    if volume is not None:
        return {"title_guess": title_guess or None, "volume_number": volume, "unit_type": "volume"}
    if chapter is not None:
        return {"title_guess": title_guess or None, "chapter_number": chapter, "unit_type": "chapter"}
    return {"title_guess": title_guess or None, "unit_type": "unknown"}


def parse_file_identity(path, media_type):
    if str(media_type or "").strip().lower() == "manga":
        return parse_manga_identity(path)
    return parse_comic_identity(path)


def year_from_text(text):
    match = re.search(_YEAR_PATTERN, str(text or ""))
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# Filesystem scan (arbitrary root — NOT limited to the configured InkDrop
# media_management.comic_root/manga_root; adoption reads a folder the user
# points us at and only ever registers issues by database row, it never
# requires that folder to be InkDrop's managed library root).
# ---------------------------------------------------------------------------


def scan_adoption_root(root, media_type, *, max_files=20000, sample_limit=500):
    root_path = Path(root)
    max_files = max(1, min(int(max_files or 20000), 100000))
    result = {"root": str(root_path), "media_type": media_type, "exists": root_path.exists(), "files": [], "truncated": False, "errors": []}
    if not root_path.exists():
        result["errors"].append("root_missing")
        return result
    if not root_path.is_dir():
        result["errors"].append("root_not_a_directory")
        return result
    import os

    for current_root, dirnames, filenames in os.walk(root_path):
        dirnames[:] = [
            name
            for name in dirnames
            if name.lower() not in inkdrop_state.MEDIA_LIBRARY_SCAN_SKIP_DIRS and not name.startswith(".")
        ]
        for filename in filenames:
            if len(result["files"]) >= max_files:
                result["truncated"] = True
                break
            candidate = Path(current_root) / filename
            extension = inkdrop_state.media_library_archive_extension(candidate)
            if not extension:
                continue
            snapshot = inkdrop_state.media_file_path_snapshot(candidate)
            result["files"].append(
                {
                    "path": str(candidate),
                    "normalized_path": inkdrop_state.media_file_normalized_path(candidate),
                    "extension": extension,
                    "size_bytes": snapshot.get("size_bytes"),
                    "mtime": snapshot.get("mtime"),
                }
            )
        if result["truncated"]:
            break
    return result


def _top_level_folder(file_path, root_path):
    try:
        rel = Path(file_path).relative_to(Path(root_path))
    except ValueError:
        return str(root_path), Path(root_path).name, "outside_root"
    parts = [part for part in rel.parts if part not in ("", ".")]
    if len(parts) <= 1:
        return str(root_path), "", "root_file"
    return str(Path(root_path) / parts[0]), parts[0], ""


def group_files_by_folder(scan, media_type):
    root_path = scan["root"]
    grouped = {}
    for file_row in scan["files"]:
        folder_path, folder_name, reason = _top_level_folder(file_row["path"], root_path)
        key = inkdrop_state.media_file_normalized_path(folder_path)
        group = grouped.setdefault(
            key,
            {
                "folder_path": folder_path,
                "folder_name": folder_name or Path(folder_path).name,
                "media_type": media_type,
                "reason": reason,
                "files": [],
            },
        )
        group["files"].append(file_row)
    return list(grouped.values())


# ---------------------------------------------------------------------------
# Matching against series InkDrop already knows about
# ---------------------------------------------------------------------------


def _known_series_indexes(con):
    if not inkdrop_state.table_exists(con, "series"):
        return {}, {}
    by_path, by_title = {}, {}
    for row in con.execute(
        "select id, title, media_type, year, publisher, metadata_provider, metadata_id, library_path from series"
    ):
        row = dict(row)
        path_key = inkdrop_state.media_file_normalized_path(row.get("library_path"))
        if path_key:
            by_path.setdefault(path_key, []).append(row)
        title_key = inkdrop_state.normalize_key(row.get("title"))
        if title_key:
            by_title.setdefault((str(row.get("media_type") or "").lower(), title_key), []).append(row)
    return by_path, by_title


def folder_title_guess(folder):
    file_titles = [parse_file_identity(f["path"], folder["media_type"]).get("title_guess") for f in folder["files"]]
    file_titles = [t for t in file_titles if t]
    if file_titles:
        counts = {}
        for title in file_titles:
            key = inkdrop_state.normalize_key(title)
            counts.setdefault(key, {"title": title, "count": 0})
            counts[key]["count"] += 1
        best = max(counts.values(), key=lambda item: item["count"])
        return best["title"]
    return _strip_edition_noise(folder["folder_name"]) or folder["folder_name"]


def classify_folder(con, folder, series_by_path, series_by_title):
    folder_key = inkdrop_state.media_file_normalized_path(folder["folder_path"])
    media_type = str(folder["media_type"] or "").lower()
    path_matches = series_by_path.get(folder_key, [])
    if path_matches:
        return "already_imported", path_matches
    title_key = inkdrop_state.normalize_key(folder_title_guess(folder))
    title_matches = series_by_title.get((media_type, title_key), [])
    if title_matches:
        return "existing_series_folder_candidate", title_matches
    return "new_series_candidate", []


# ---------------------------------------------------------------------------
# Plan (read-only)
# ---------------------------------------------------------------------------


def build_adoption_plan(db_path, root, media_type, *, search_fn=None, max_files=20000, sample_limit=200, max_metadata_lookups=25):
    """Read-only. Returns candidate folders with a proposed identity for a human to confirm.

    `search_fn`, if given, is called as `search_fn(query_title)` and must return a list of
    metadata-provider candidate dicts (e.g. the output of comicvine_search_volumes /
    mangadex_search_manga). It is only called for folders InkDrop doesn't already
    recognize, and only up to `max_metadata_lookups` times, to bound provider API usage
    on a large first-time library scan.
    """
    media_type = str(media_type or "comic").strip().lower()
    scan = scan_adoption_root(root, media_type, max_files=max_files, sample_limit=sample_limit)
    if not scan["exists"] or scan["errors"]:
        return {"ok": False, "root": scan["root"], "media_type": media_type, "errors": scan["errors"]}
    folders = group_files_by_folder(scan, media_type)
    with inkdrop_state.connect_read(db_path) as con:
        series_by_path, series_by_title = _known_series_indexes(con)
        candidates = []
        metadata_lookups_used = 0
        summary = {"folders": 0, "already_imported": 0, "existing_series_folder_candidates": 0, "new_series_candidates": 0, "loose_root_files": 0}
        for folder in sorted(folders, key=lambda f: inkdrop_state.normalize_key(f["folder_name"])):
            if folder["reason"] == "root_file":
                summary["loose_root_files"] += len(folder["files"])
                continue
            status, matches = classify_folder(con, folder, series_by_path, series_by_title)
            summary["folders"] += 1
            summary[status if status != "new_series_candidate" else "new_series_candidates"] = summary.get(
                status if status != "new_series_candidate" else "new_series_candidates", 0
            )
            if status == "already_imported":
                summary["already_imported"] += 1
                continue  # nothing to propose; caller can skip these entirely
            if status == "existing_series_folder_candidate":
                summary["existing_series_folder_candidates"] += 1
            else:
                summary["new_series_candidates"] += 1
            title_guess = folder_title_guess(folder)
            files = []
            for file_row in folder["files"]:
                identity = parse_file_identity(file_row["path"], media_type)
                needs_review = (
                    (media_type != "manga" and identity.get("issue_number") is None)
                    or (media_type == "manga" and identity.get("unit_type") == "unknown")
                )
                files.append({**file_row, **identity, "needs_review": needs_review})
            metadata_matches = []
            if search_fn is not None and status == "new_series_candidate" and metadata_lookups_used < max_metadata_lookups:
                try:
                    metadata_matches = list(search_fn(title_guess) or [])[:5]
                except Exception as exc:  # provider errors must not break the scan
                    metadata_matches = []
                    folder_error = str(exc)[:200]
                else:
                    folder_error = None
                metadata_lookups_used += 1
            else:
                folder_error = None
            candidates.append(
                {
                    "id": inkdrop_state.stable_id("library_adoption_candidate", media_type, folder["folder_path"]),
                    "media_type": media_type,
                    "folder_path": folder["folder_path"],
                    "folder_name": folder["folder_name"],
                    "status": status,
                    "title_guess": title_guess,
                    "year_guess": year_from_text(folder["folder_name"]),
                    "file_count": len(files),
                    "files_needing_review": sum(1 for f in files if f["needs_review"]),
                    "existing_series_matches": [{"id": r["id"], "title": r["title"], "metadata_provider": r.get("metadata_provider"), "metadata_id": r.get("metadata_id")} for r in matches],
                    "metadata_matches": metadata_matches,
                    "metadata_lookup_error": folder_error,
                    "files": files,
                }
            )
    return {
        "ok": True,
        "schema": "inkdrop.library_adoption_plan.v1",
        "checked_at": time.time(),
        "root": scan["root"],
        "media_type": media_type,
        "truncated": scan["truncated"],
        "summary": summary,
        "candidates": candidates,
    }


# ---------------------------------------------------------------------------
# Apply (write, gated on explicit human confirmation)
# ---------------------------------------------------------------------------


class AdoptionAlreadyImported(Exception):
    """Raised when the folder is already linked to a series (race with a concurrent import/adoption)."""


def apply_adoption_folder(db_path, backup_path, *, root, folder_path, media_type, series_selection, confirmed_files, now=None):
    """Register `confirmed_files` from `folder_path` as already-owned.

    `series_selection` must be one of:
      {"existing_series_id": "<series id>"}                                   -- attach to a known series
      {"metadata_provider": "comicvine"|"mangadex", "metadata_id": "...",
       "title": "...", "year": ..., "publisher": ...}                        -- create/attach via provider id
      {"title": "..."}                                                        -- manual, no external metadata id

    `confirmed_files` is a list of {"path": ..., "issue_number": ...} (comic) or
    {"path": ..., "volume_number": ...} / {"path": ..., "chapter_number": ...} (manga) that
    the caller has already had a human confirm (typically the plan's per-file guess, after
    review). Files not present in this list are left untouched -- adoption never guesses
    at write time, only at plan time.

    Never touches the filesystem: no copy, move, rename, or delete of the user's files.
    Never creates queue_items/download_tasks -- adopted issues are written directly as
    wanted_items.status="satisfied" so search/autopilot workers never pick them up.
    """
    if not confirmed_files:
        raise ValueError("confirmed_files must be a non-empty explicit list")
    media_type = str(media_type or "comic").strip().lower()
    now = time.time() if now is None else now
    db_path = Path(db_path)
    backup_path = Path(backup_path)
    if backup_path.exists() or backup_path.resolve() == db_path.resolve():
        raise ValueError("backup path must be new and different from the database")
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    import sqlite3

    source = sqlite3.connect(db_path, timeout=30)
    dest = sqlite3.connect(backup_path, timeout=30)
    source.backup(dest)
    dest.close()
    source.row_factory = sqlite3.Row
    try:
        inkdrop_state.init_schema(source)
        series_by_path, series_by_title = _known_series_indexes(source)
        folder_key = inkdrop_state.media_file_normalized_path(folder_path)
        if series_by_path.get(folder_key):
            raise AdoptionAlreadyImported(f"{folder_path} is already linked to series {series_by_path[folder_key][0]['id']}")

        source.execute("begin immediate")
        payload = {
            "source": "library_adoption",
            "enabled": True,
            "autoGrab": False,
            "monitorNew": True,
            "library_path": folder_path,
            "library_root": str(root),
            "library_path_source": "user",
            "created_at": now,
        }
        if "existing_series_id" in series_selection:
            existing = source.execute("select * from series where id=?", (series_selection["existing_series_id"],)).fetchone()
            if not existing:
                raise ValueError(f"existing_series_id {series_selection['existing_series_id']!r} not found")
            payload["name"] = existing["title"]
            payload["metadataProvider"] = existing["metadata_provider"]
            payload["metadataId"] = existing["metadata_id"]
        else:
            payload["name"] = series_selection.get("title")
            if series_selection.get("metadata_provider"):
                payload["metadataProvider"] = series_selection["metadata_provider"]
                payload["metadataId"] = series_selection.get("metadata_id")
            payload["publisher"] = series_selection.get("publisher")
            payload["year"] = series_selection.get("year")
        if not payload.get("name"):
            raise ValueError("series_selection must resolve to a title")

        series_id = inkdrop_state.upsert_series(source, payload, now)

        registered = []
        skipped = []
        for entry in confirmed_files:
            path = entry.get("path")
            if not path:
                skipped.append({"path": path, "reason": "missing_path"})
                continue
            if media_type == "manga" and entry.get("volume_number") is not None:
                issue_payload = {"issueNumber": entry["volume_number"], "unit_type": "volume", "adopted_path": path}
            elif media_type == "manga" and entry.get("chapter_number") is not None:
                issue_payload = {"issueNumber": entry["chapter_number"], "unit_type": "chapter", "adopted_path": path}
            elif entry.get("issue_number") is not None:
                issue_payload = {"issueNumber": entry["issue_number"], "unit_type": "issue", "adopted_path": path}
            else:
                skipped.append({"path": path, "reason": "no_confirmed_number"})
                continue
            issue_id = inkdrop_state.upsert_issue(source, series_id, issue_payload, now)
            wanted_id = inkdrop_state.upsert_wanted(
                source,
                series_id,
                issue_id,
                "library_adoption",
                "satisfied",
                {"adopted_path": path, "adopted_at": now, "created_at": now},
                now,
            )
            registered.append({"path": path, "issue_id": issue_id, "wanted_id": wanted_id})

        event_id = inkdrop_state.stable_id("library_adoption", series_id, folder_path, now)
        source.execute(
            "insert into history_events(id,entity_type,entity_id,series_id,event_type,source,message,created_at,raw_json,outcome,display_phase) "
            "values(?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                "series",
                series_id,
                series_id,
                "library_adoption_applied",
                "inkdrop_library_adoption",
                f"Adopted {len(registered)} file(s) from existing library folder without downloading",
                now,
                json.dumps({"folder_path": folder_path, "registered": registered, "skipped": skipped}, separators=(",", ":"), sort_keys=True, default=str),
                "adopted",
                "library_adoption",
            ),
        )
        source.commit()
        return {
            "ok": True,
            "series_id": series_id,
            "folder_path": folder_path,
            "registered": registered,
            "skipped": skipped,
            "backup_path": str(backup_path),
            "audit_event_id": event_id,
        }
    except Exception:
        source.rollback()
        raise
    finally:
        source.close()


def main():
    parser = argparse.ArgumentParser(description="Scan an existing comic/manga folder and propose (never apply automatically) an InkDrop adoption plan")
    parser.add_argument("--db", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--media-type", choices=("comic", "manga"), default="comic")
    parser.add_argument("--max-files", type=int, default=20000)
    args = parser.parse_args()
    plan = build_adoption_plan(args.db, args.root, args.media_type, max_files=args.max_files)
    print(json.dumps(plan, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
