#!/usr/bin/env python3
"""Rewrite CBR/RAR comics as CBZ so Kavita and Komga stop needing unrar.

InkDrop already converts CBRs on the way in: the managed import path repacks
`.cbr` to `.cbz` before a file ever lands in the library. What it has never had
is a way to fix the files that were already there — a library adopted from an
existing collection, or one built before InkDrop, is full of CBRs that both
readers open slowly or not at all depending on whether unrar is installed in
their container.

This module is that missing pass. It walks the library, finds every archive
that is really RAR-compressed, and rewrites each one as a plain zip:

- pages come out in natural order (page2 before page10, which plain string
  sorting gets wrong) and keep their original image bytes untouched,
- `ComicInfo.xml` is carried across verbatim, so series/volume/number metadata
  the reader depends on survives the rewrite,
- the new CBZ is validated before anything is replaced: the zip must open, its
  CRCs must pass, the page count must match the source, and every page's
  sha256 must equal the byte content extracted from the original,
- only then does the original move aside, to the quarantine directory outside
  the library so a reader rescan does not index it twice. Nothing is deleted
  unless you ask for it.

Anything unexpected fails that one file closed and leaves it alone: a name
collision, a nested archive inside the CBR, a partial extraction, a page that
does not survive validation. The rest of the run continues.

A `.cbr` that is actually a zip with the wrong extension is common in the wild
and is handled with no external tools at all. True RAR content needs `7z` or
`unrar` on PATH; the plan reports up front how many files need it and whether
it is there, rather than discovering it one failure at a time.

Nothing here touches the state database or the web app. It is a file-level
pass that can be run from the CLI on its own.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import inkdrop_runtime_config


ARCHIVE_CONVERSION_SCHEMA = "inkdrop.archive_conversion.v1"

COMIC_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}
# Archives that are legitimately allowed to appear inside a comic archive:
# none. A nested archive means the file is a pack, not a single issue, and a
# pack needs the import pipeline's judgement rather than a blind repack.
NESTED_ARCHIVE_EXTS = {".zip", ".rar", ".7z", ".cbz", ".cbr", ".cb7", ".tar", ".gz", ".pdf"}

CONVERTIBLE_SUFFIXES = {".cbr", ".rar"}
# A .cbz whose bytes are actually RAR is the other half of the same mess. It is
# reported by default and only rewritten when explicitly asked for, because the
# fix rewrites a file the reader currently believes is fine.
MISLABELED_SUFFIXES = {".cbz"}

EXTRACT_TIMEOUT_SECONDS = int(os.environ.get("INKDROP_CBR_EXTRACT_TIMEOUT_SECONDS") or 90)
LIST_TIMEOUT_SECONDS = int(os.environ.get("INKDROP_CBR_LIST_TIMEOUT_SECONDS") or 60)

INTERNAL_DIR_NAMES = {
    "_Incoming",
    "_quarantine",
    "_processed",
    "_failed",
    "_failed-proof-copies",
    "_manga-library-split-quarantine",
}

ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
RAR_MAGIC = (b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01\x00")
SEVENZIP_MAGIC = b"7z\xbc\xaf\x27\x1c"


def default_library_roots():
    return [
        Path(os.environ.get("INKDROP_COMIC_ROOT") or "/library/comics"),
        Path(os.environ.get("INKDROP_MANGA_ROOT") or "/library/manga"),
    ]


def default_originals_dir():
    return inkdrop_runtime_config.quarantine_dir() / "converted-originals"


def natural_sort_key(text):
    """Sort page names the way a person reads them: 2 before 10."""
    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"(\d+)", str(text))
    ]


def is_internal_library_path(path, root):
    try:
        parts = Path(path).relative_to(root).parts
    except ValueError:
        parts = Path(path).parts
    return any(part.startswith("_") or part in INTERNAL_DIR_NAMES for part in parts)


def archive_container_format(path):
    """What the bytes say the archive is, ignoring what the extension claims."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(8)
    except OSError:
        return "unreadable"
    if head.startswith(ZIP_MAGIC):
        return "zip"
    if any(head.startswith(magic) for magic in RAR_MAGIC):
        return "rar"
    if head.startswith(SEVENZIP_MAGIC):
        return "sevenzip"
    return "unknown"


def _resolve_tool(candidate):
    raw = str(candidate or "").strip()
    if not raw:
        return None
    path = Path(raw)
    if path.is_absolute() or any(separator in raw for separator in ("/", "\\")):
        return raw if path.exists() else None
    return shutil.which(raw)


def preferred_sevenzip_tool():
    for candidate in (os.environ.get("INKDROP_SEVENZIP_PATH"), "7z", "7za", "7zz"):
        resolved = _resolve_tool(candidate)
        if resolved:
            return resolved
    return None


def preferred_unrar_tool():
    for candidate in (
        os.environ.get("INKDROP_UNRAR_PATH"),
        os.environ.get("INKDROP_UNRAR"),
        "/usr/bin/unrar",
        "/usr/bin/unrar-free",
        "unrar",
        "unrar-free",
    ):
        resolved = _resolve_tool(candidate)
        if resolved:
            return resolved
    return None


def rar_tooling():
    sevenzip = preferred_sevenzip_tool()
    unrar = preferred_unrar_tool()
    return {
        "sevenzip": sevenzip,
        "unrar": unrar,
        "available": bool(sevenzip or unrar),
    }


def _run_bounded(args, timeout):
    popen_kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True
    with subprocess.Popen(args, **popen_kwargs) as proc:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise
    return proc.returncode, (stdout or b"").decode("utf-8", errors="replace"), (stderr or b"").decode("utf-8", errors="replace")


def _member_kind(name):
    suffix = Path(str(name).replace("\\", "/")).suffix.lower()
    if suffix in COMIC_IMAGE_EXTS:
        return "image"
    if str(name).replace("\\", "/").rsplit("/", 1)[-1].lower() == "comicinfo.xml":
        return "comicinfo"
    if suffix in NESTED_ARCHIVE_EXTS:
        return "nested_archive"
    return "other"


def _safe_relative_name(name):
    """Reject archive members that would write outside the extraction dir."""
    cleaned = str(name).replace("\\", "/").strip()
    if not cleaned or cleaned.endswith("/"):
        return None
    if cleaned.startswith("/") or re.match(r"^[A-Za-z]:", cleaned):
        return None
    parts = [part for part in cleaned.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        return None
    return "/".join(parts) if parts else None


class ConversionRefused(Exception):
    """This one file cannot be converted safely. The run continues."""

    def __init__(self, reason, detail=None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def list_archive_members(source, container):
    """Member names inside the archive, without extracting it."""
    if container == "zip":
        try:
            with zipfile.ZipFile(source) as archive:
                return [info.filename for info in archive.infolist() if not info.is_dir()]
        except (OSError, zipfile.BadZipFile) as exc:
            raise ConversionRefused("source_archive_unreadable", f"{type(exc).__name__}: {exc}")
    if container != "rar":
        raise ConversionRefused("source_container_unsupported", container)
    tools = rar_tooling()
    if not tools["sevenzip"]:
        # unrar can list too, but 7z's -slt output is the one the rest of
        # InkDrop already parses. Without either, the caller finds out here.
        if not tools["unrar"]:
            raise ConversionRefused("rar_tooling_missing")
        return _list_rar_members_unrar(source, tools["unrar"])
    code, stdout, stderr = _run_bounded([tools["sevenzip"], "l", "-slt", str(source)], LIST_TIMEOUT_SECONDS)
    if code != 0:
        raise ConversionRefused("source_listing_failed", (stderr or stdout).strip()[:400])
    names = []
    for line in stdout.splitlines():
        if line.startswith("Path = "):
            name = line[7:].strip()
            if name and name != str(source):
                names.append(name)
    return names


def _list_rar_members_unrar(source, unrar):
    code, stdout, stderr = _run_bounded([unrar, "lb", str(source)], LIST_TIMEOUT_SECONDS)
    if code != 0:
        raise ConversionRefused("source_listing_failed", (stderr or stdout).strip()[:400])
    return [line.strip() for line in stdout.splitlines() if line.strip()]


def _extract_zip(source, workdir):
    try:
        with zipfile.ZipFile(source) as archive:
            bad = archive.testzip()
            if bad is not None:
                raise ConversionRefused("source_member_crc_failed", bad)
            for info in archive.infolist():
                if info.is_dir():
                    continue
                relative = _safe_relative_name(info.filename)
                if relative is None:
                    raise ConversionRefused("source_member_path_unsafe", info.filename)
                destination = workdir / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as member, open(destination, "wb") as handle:
                    shutil.copyfileobj(member, handle)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ConversionRefused("source_archive_unreadable", f"{type(exc).__name__}: {exc}")
    return {"extractor": "zipfile"}


def _extract_rar(source, workdir):
    tools = rar_tooling()
    if not tools["available"]:
        raise ConversionRefused("rar_tooling_missing")
    attempts = []
    if tools["sevenzip"]:
        attempts.append(("7z", [tools["sevenzip"], "x", "-y", f"-o{workdir}", str(source)]))
    if tools["unrar"]:
        attempts.append(("unrar", [tools["unrar"], "x", "-o+", "-idq", str(source), str(workdir) + os.sep]))
    last = None
    for name, args in attempts:
        try:
            code, stdout, stderr = _run_bounded(args, EXTRACT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            last = {"extractor": name, "exit_code": None, "error": "timeout"}
            _empty_directory(workdir)
            continue
        if code == 0:
            return {"extractor": name, "exit_code": code}
        last = {"extractor": name, "exit_code": code, "error": (stderr or stdout).strip()[:400]}
        _empty_directory(workdir)
    raise ConversionRefused("source_extraction_failed", last)


def _empty_directory(directory):
    for child in sorted(directory.iterdir(), reverse=True):
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            try:
                child.unlink()
            except OSError:
                pass


def _extracted_files(workdir):
    return [item for item in workdir.rglob("*") if item.is_file() and not item.is_symlink()]


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_archive(source):
    """Classify an archive's members before deciding whether to touch it."""
    source = Path(source)
    container = archive_container_format(source)
    members = list_archive_members(source, container)
    counts = {"image": 0, "comicinfo": 0, "nested_archive": 0, "other": 0}
    nested = []
    other = []
    for name in members:
        kind = _member_kind(name)
        counts[kind] += 1
        if kind == "nested_archive":
            nested.append(name)
        elif kind == "other":
            other.append(name)
    return {
        "container": container,
        "member_count": len(members),
        "image_count": counts["image"],
        "has_comicinfo": counts["comicinfo"] > 0,
        "nested_archives": sorted(nested)[:10],
        "other_members": sorted(other)[:20],
    }


def build_cbz(source, dest_tmp, workdir):
    """Extract `source` into `workdir` and write a validated-ready CBZ at `dest_tmp`.

    Returns the page manifest the validator needs: every page name written and
    the sha256 of the bytes that went in, taken from the extracted file rather
    than from the archive we just produced, so validation compares two
    independent readings.
    """
    container = archive_container_format(source)
    if container == "zip":
        extract_meta = _extract_zip(source, workdir)
    elif container == "rar":
        extract_meta = _extract_rar(source, workdir)
    else:
        raise ConversionRefused("source_container_unsupported", container)

    extracted = _extracted_files(workdir)
    pages = []
    comicinfo_file = None
    dropped = []
    for item in extracted:
        relative = item.relative_to(workdir).as_posix()
        kind = _member_kind(relative)
        if kind == "image":
            if item.stat().st_size <= 0:
                raise ConversionRefused("source_page_empty", relative)
            pages.append(item)
        elif kind == "comicinfo":
            # Nested ComicInfo.xml files happen; the shallowest, shortest path
            # is the one readers treat as the archive's own.
            if comicinfo_file is None or (relative.count("/"), len(relative)) < (
                comicinfo_file.relative_to(workdir).as_posix().count("/"),
                len(comicinfo_file.relative_to(workdir).as_posix()),
            ):
                comicinfo_file = item
        elif kind == "nested_archive":
            raise ConversionRefused("nested_archive_member", relative)
        else:
            dropped.append(relative)

    if not pages:
        raise ConversionRefused("no_readable_pages")

    pages.sort(key=lambda item: natural_sort_key(item.relative_to(workdir).as_posix()))
    width = max(4, len(str(len(pages))))

    manifest = []
    dest_tmp.parent.mkdir(parents=True, exist_ok=True)
    if dest_tmp.exists():
        dest_tmp.unlink()
    with zipfile.ZipFile(dest_tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for index, page in enumerate(pages, 1):
            stored = f"{index:0{width}d}{page.suffix.lower()}"
            archive.write(page, stored)
            manifest.append(
                {
                    "stored_name": stored,
                    "source_name": page.relative_to(workdir).as_posix(),
                    "sha256": _sha256(page),
                    "bytes": page.stat().st_size,
                }
            )
        if comicinfo_file is not None:
            archive.writestr("ComicInfo.xml", comicinfo_file.read_bytes())

    return {
        "pages": manifest,
        "comicinfo_preserved": comicinfo_file is not None,
        "dropped_members": sorted(dropped),
        "source_format": container,
        "dest_format": "cbz",
        **extract_meta,
    }


def validate_cbz(path, build_meta):
    """Re-read the written archive and prove it holds what we meant to write."""
    path = Path(path)
    expected = {entry["stored_name"]: entry for entry in build_meta["pages"]}
    try:
        with zipfile.ZipFile(path) as archive:
            bad = archive.testzip()
            if bad is not None:
                return {"ok": False, "reason": "output_member_crc_failed", "detail": bad}
            names = [info.filename for info in archive.infolist() if not info.is_dir()]
            images = [name for name in names if _member_kind(name) == "image"]
            if len(images) != len(expected):
                return {
                    "ok": False,
                    "reason": "output_page_count_mismatch",
                    "detail": {"expected": len(expected), "found": len(images)},
                }
            for name in images:
                entry = expected.get(name)
                if entry is None:
                    return {"ok": False, "reason": "output_unexpected_page", "detail": name}
                data = archive.read(name)
                if not data:
                    return {"ok": False, "reason": "output_page_empty", "detail": name}
                if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                    return {"ok": False, "reason": "output_page_content_mismatch", "detail": name}
            if build_meta["comicinfo_preserved"] and "ComicInfo.xml" not in names:
                return {"ok": False, "reason": "output_comicinfo_missing"}
    except (OSError, zipfile.BadZipFile) as exc:
        return {"ok": False, "reason": "output_archive_unreadable", "detail": f"{type(exc).__name__}: {exc}"}
    return {
        "ok": True,
        "page_count": len(expected),
        "comicinfo_preserved": build_meta["comicinfo_preserved"],
        "bytes": path.stat().st_size,
    }


def _originals_destination(source, root, originals_dir):
    try:
        relative = Path(source).relative_to(root)
    except ValueError:
        relative = Path(Path(source).name)
    return Path(originals_dir) / Path(root).name / relative


def convert_archive(source, *, root=None, originals_dir=None, keep_original=True, dry_run=False):
    """Convert one archive. Never raises for a per-file problem; reports it."""
    source = Path(source)
    root = Path(root) if root else source.parent
    result = {
        "source": str(source),
        "converted": False,
        "dry_run": bool(dry_run),
    }
    if not source.is_file():
        return {**result, "reason": "source_missing"}

    dest = source.with_suffix(".cbz")
    result["dest"] = str(dest)
    if dest.exists() and dest.resolve() != source.resolve():
        return {**result, "reason": "destination_exists"}

    try:
        inspection = inspect_archive(source)
    except ConversionRefused as exc:
        return {**result, "reason": exc.reason, "detail": exc.detail}
    result["inspection"] = inspection

    if inspection["container"] not in {"zip", "rar"}:
        return {**result, "reason": "source_container_unsupported"}
    if inspection["nested_archives"]:
        return {**result, "reason": "nested_archive_member", "detail": inspection["nested_archives"]}
    if not inspection["image_count"]:
        return {**result, "reason": "no_image_members"}

    originals_target = None
    if keep_original:
        originals_target = _originals_destination(source, root, originals_dir or default_originals_dir())
        result["original_moved_to"] = str(originals_target)
        if originals_target.exists():
            return {**result, "reason": "original_archive_slot_taken"}

    if dry_run:
        return {**result, "reason": "convertible", "ok": True}

    started = time.time()
    tmp_dest = dest.with_suffix(dest.suffix + ".tmp")
    with tempfile.TemporaryDirectory(prefix="inkdrop-cbz-convert-") as tmp:
        workdir = Path(tmp) / "extract"
        workdir.mkdir(parents=True, exist_ok=True)
        try:
            build_meta = build_cbz(source, tmp_dest, workdir)
        except ConversionRefused as exc:
            tmp_dest.unlink(missing_ok=True)
            return {**result, "reason": exc.reason, "detail": exc.detail}
        except (OSError, subprocess.TimeoutExpired) as exc:
            tmp_dest.unlink(missing_ok=True)
            return {**result, "reason": "conversion_failed", "detail": f"{type(exc).__name__}: {exc}"}

        # Page counts come from two readings of the source: the archive listing
        # taken before extraction, and the files that actually landed on disk. A
        # RAR that only half-extracts passes the second and fails this.
        if len(build_meta["pages"]) < inspection["image_count"]:
            tmp_dest.unlink(missing_ok=True)
            return {
                **result,
                "reason": "partial_extraction",
                "detail": {"listed": inspection["image_count"], "extracted": len(build_meta["pages"])},
            }

        validation = validate_cbz(tmp_dest, build_meta)
        result["validation"] = validation
        if not validation["ok"]:
            tmp_dest.unlink(missing_ok=True)
            return {**result, "reason": validation["reason"], "detail": validation.get("detail")}

        if inspection["has_comicinfo"] and not build_meta["comicinfo_preserved"]:
            tmp_dest.unlink(missing_ok=True)
            return {**result, "reason": "comicinfo_lost_in_conversion"}

        # The original is retired first. If that fails there is still exactly
        # one readable file in the library folder: the untouched original.
        if keep_original:
            try:
                originals_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(originals_target))
            except OSError as exc:
                tmp_dest.unlink(missing_ok=True)
                return {**result, "reason": "original_retire_failed", "detail": f"{type(exc).__name__}: {exc}"}
        else:
            try:
                source.unlink()
            except OSError as exc:
                tmp_dest.unlink(missing_ok=True)
                return {**result, "reason": "original_delete_failed", "detail": f"{type(exc).__name__}: {exc}"}

        try:
            tmp_dest.replace(dest)
        except OSError as exc:
            # Put the original back rather than leaving the folder empty.
            if keep_original and originals_target.exists():
                try:
                    shutil.move(str(originals_target), str(source))
                except OSError:
                    pass
            tmp_dest.unlink(missing_ok=True)
            return {**result, "reason": "publish_failed", "detail": f"{type(exc).__name__}: {exc}"}

    result.update(
        {
            "converted": True,
            "ok": True,
            "reason": "converted",
            "page_count": len(build_meta["pages"]),
            "comicinfo_preserved": build_meta["comicinfo_preserved"],
            "dropped_members": build_meta["dropped_members"],
            "extractor": build_meta.get("extractor"),
            "source_format": build_meta["source_format"],
            "elapsed_seconds": round(time.time() - started, 3),
        }
    )
    return result


def scan_library(roots=None, *, include_mislabeled_cbz=False, progress=None):
    """Find every archive in the library that a CBZ-only reader would struggle with."""
    roots = [Path(root) for root in (roots or default_library_roots())]
    candidates = []
    skipped_roots = []
    scanned = 0
    for root in roots:
        if not root.is_dir():
            skipped_roots.append({"root": str(root), "reason": "root_missing"})
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            if is_internal_library_path(path, root):
                continue
            suffix = path.suffix.lower()
            if suffix not in CONVERTIBLE_SUFFIXES and suffix not in MISLABELED_SUFFIXES:
                continue
            scanned += 1
            if progress and scanned % 25 == 0:
                progress(scanned, len(candidates))
            container = archive_container_format(path)
            if suffix in CONVERTIBLE_SUFFIXES:
                if container == "zip":
                    reason = "zip_named_cbr"
                elif container == "rar":
                    reason = "rar"
                else:
                    candidates.append(
                        {"path": str(path), "root": str(root), "container": container, "classification": "unsupported_container", "convertible": False}
                    )
                    continue
                candidates.append(
                    {"path": str(path), "root": str(root), "container": container, "classification": reason, "convertible": True}
                )
                continue
            # .cbz that is not really a zip
            if container == "rar":
                candidates.append(
                    {
                        "path": str(path),
                        "root": str(root),
                        "container": container,
                        "classification": "rar_named_cbz",
                        "convertible": bool(include_mislabeled_cbz),
                    }
                )
    if progress:
        progress(scanned, len(candidates))
    tools = rar_tooling()
    needs_rar = [item for item in candidates if item["container"] == "rar" and item["convertible"]]
    return {
        "schema": ARCHIVE_CONVERSION_SCHEMA,
        "roots": [str(root) for root in roots],
        "skipped_roots": skipped_roots,
        "candidates": candidates,
        "convertible_count": sum(1 for item in candidates if item["convertible"]),
        "needs_rar_tooling": len(needs_rar),
        "rar_tooling": tools,
        "blocked_on_rar_tooling": bool(needs_rar) and not tools["available"],
    }


def convert_library(
    roots=None,
    *,
    dry_run=True,
    limit=None,
    keep_original=True,
    originals_dir=None,
    include_mislabeled_cbz=False,
    progress=None,
    scan_progress=None,
):
    """Batch pass over the library. Dry run by default; one file's failure is not the run's."""
    scan = scan_library(roots, include_mislabeled_cbz=include_mislabeled_cbz, progress=scan_progress)
    originals_dir = Path(originals_dir) if originals_dir else default_originals_dir()

    summary = {
        "schema": ARCHIVE_CONVERSION_SCHEMA,
        "dry_run": bool(dry_run),
        "roots": scan["roots"],
        "skipped_roots": scan["skipped_roots"],
        "rar_tooling": scan["rar_tooling"],
        "candidate_count": len(scan["candidates"]),
        "convertible_count": scan["convertible_count"],
        "needs_rar_tooling": scan["needs_rar_tooling"],
        "keep_original": bool(keep_original),
        "originals_dir": str(originals_dir) if keep_original else None,
        "results": [],
        "converted": 0,
        "failed": 0,
        "skipped": 0,
    }

    if scan["blocked_on_rar_tooling"] and not dry_run:
        # Refuse the whole run rather than convert the zip-named-cbr half and
        # leave a confusing partial result behind.
        summary["reason"] = "rar_tooling_missing"
        summary["ok"] = False
        return summary

    if not dry_run and keep_original:
        try:
            originals_dir.mkdir(parents=True, exist_ok=True)
            probe = originals_dir / ".inkdrop-write-probe"
            probe.write_bytes(b"")
            probe.unlink()
        except OSError as exc:
            summary["reason"] = "originals_dir_unwritable"
            summary["detail"] = f"{type(exc).__name__}: {exc}"
            summary["ok"] = False
            return summary

    attempted = 0
    for candidate in scan["candidates"]:
        if not candidate["convertible"]:
            summary["skipped"] += 1
            summary["results"].append(
                {"source": candidate["path"], "converted": False, "reason": candidate["classification"]}
            )
            continue
        if limit is not None and attempted >= limit:
            summary["skipped"] += 1
            summary["results"].append({"source": candidate["path"], "converted": False, "reason": "limit_reached"})
            continue
        attempted += 1
        result = convert_archive(
            candidate["path"],
            root=candidate["root"],
            originals_dir=originals_dir,
            keep_original=keep_original,
            dry_run=dry_run,
        )
        result["classification"] = candidate["classification"]
        summary["results"].append(result)
        if result.get("converted"):
            summary["converted"] += 1
        elif dry_run and result.get("ok"):
            pass
        else:
            summary["failed"] += 1
        if progress:
            progress(result)

    summary["attempted"] = attempted
    summary["ok"] = summary["failed"] == 0
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description="Rewrite CBR/RAR comics as CBZ, in place, with the originals kept.")
    parser.add_argument("--root", action="append", dest="roots", help="Library root to walk. Repeatable. Defaults to the comic and manga roots.")
    parser.add_argument("--apply", action="store_true", help="Actually convert. Without this the run only reports what it would do.")
    parser.add_argument("--limit", type=int, default=None, help="Convert at most this many archives.")
    parser.add_argument("--discard-originals", action="store_true", help="Delete each original after its CBZ validates, instead of moving it aside.")
    parser.add_argument("--originals-dir", default=None, help="Where retired originals go. Defaults to the quarantine directory.")
    parser.add_argument("--include-mislabeled-cbz", action="store_true", help="Also rewrite .cbz files whose bytes are actually RAR.")
    parser.add_argument("--json", action="store_true", help="Print the full result document instead of a summary.")
    args = parser.parse_args(argv)

    summary = convert_library(
        [Path(root) for root in args.roots] if args.roots else None,
        dry_run=not args.apply,
        limit=args.limit,
        keep_original=not args.discard_originals,
        originals_dir=args.originals_dir,
        include_mislabeled_cbz=args.include_mislabeled_cbz,
    )

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0 if summary.get("ok") else 1

    mode = "would convert" if summary["dry_run"] else "converted"
    print(f"roots: {', '.join(summary['roots'])}")
    for skipped in summary["skipped_roots"]:
        print(f"  skipped {skipped['root']}: {skipped['reason']}")
    tools = summary["rar_tooling"]
    print(f"rar tooling: 7z={tools['sevenzip'] or 'not found'} unrar={tools['unrar'] or 'not found'}")
    print(f"candidates: {summary['candidate_count']} ({summary['convertible_count']} convertible, {summary['needs_rar_tooling']} need rar tooling)")
    if summary.get("reason") and not summary.get("ok"):
        print(f"run refused: {summary['reason']} {summary.get('detail', '')}".strip())
        return 1
    print(f"{mode}: {summary['converted'] if not summary['dry_run'] else summary.get('attempted', 0)}")
    failures = [item for item in summary["results"] if not item.get("ok") and item.get("reason") not in {"zip_named_cbr", "rar_named_cbz"}]
    for failure in failures[:20]:
        print(f"  {failure['reason']}: {failure['source']}")
    if len(failures) > 20:
        print(f"  ... and {len(failures) - 20} more")
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
