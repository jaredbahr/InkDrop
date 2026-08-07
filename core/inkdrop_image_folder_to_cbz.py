#!/usr/bin/env python3
"""Build a validated CBZ from one directory of JPEG or PNG pages."""

from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))


import argparse
import codecs
import hashlib
import io
import json
import os
import re
import secrets
import stat
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from PIL import Image, UnidentifiedImageError


SCHEMA = "inkdrop.image_folder_to_cbz.v1"
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
EXPECTED_IMAGE_FORMATS = {
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".png": "PNG",
}
MAX_COMICINFO_BYTES = 1024 * 1024
TEMP_NAME_ATTEMPTS = 32
XML_DECLARATION_ENCODING = re.compile(
    br"<\?xml\b[^>]*\bencoding\s*=\s*['\"]([A-Za-z][A-Za-z0-9._-]*)['\"]",
    re.IGNORECASE,
)


class ImageFolderError(Exception):
    def __init__(self, reason, detail=None):
        super().__init__(reason)
        self.reason = str(reason)
        self.detail = detail


def natural_sort_key(value):
    """Return a deterministic key that puts page2 before page10."""
    text = str(value)
    parts = []
    for part in re.split(r"(\d+)", text):
        if part.isdigit():
            parts.append((0, int(part), len(part)))
        else:
            parts.append((1, part.casefold()))
    parts.append((2, text.casefold(), text))
    return tuple(parts)


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _decode_xml_for_safety(data, *, source):
    if data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        encoding = "utf-32"
    elif data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encoding = "utf-16"
    elif data.startswith(codecs.BOM_UTF8):
        encoding = "utf-8-sig"
    elif data.startswith(b"\x00\x00\x00<"):
        encoding = "utf-32-be"
    elif data.startswith(b"<\x00\x00\x00"):
        encoding = "utf-32-le"
    elif data.startswith(b"\x00<"):
        encoding = "utf-16-be"
    elif data.startswith(b"<\x00"):
        encoding = "utf-16-le"
    else:
        declaration = XML_DECLARATION_ENCODING.search(data[:1024])
        encoding = declaration.group(1).decode("ascii") if declaration else "utf-8"
    try:
        return data.decode(encoding)
    except (LookupError, UnicodeDecodeError) as exc:
        raise ImageFolderError(
            "comicinfo_invalid_encoding",
            {"path": str(source), "encoding": encoding, "error": str(exc)},
        ) from exc


def _validate_xml(data, *, source):
    if not data:
        raise ImageFolderError("comicinfo_empty", str(source))
    if len(data) > MAX_COMICINFO_BYTES:
        raise ImageFolderError(
            "comicinfo_too_large",
            {"path": str(source), "bytes": len(data), "maximum": MAX_COMICINFO_BYTES},
        )
    decoded = _decode_xml_for_safety(data, source=source)
    upper = decoded.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise ImageFolderError("comicinfo_unsafe_declaration", str(source))
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise ImageFolderError("comicinfo_invalid_xml", str(exc)) from exc
    root_name = str(root.tag).rsplit("}", 1)[-1]
    if root_name != "ComicInfo":
        raise ImageFolderError("comicinfo_wrong_root", root_name)
    return root


def _fully_decode_image(opener):
    with opener() as image:
        verified_format = str(image.format or "").upper()
        verified_size = image.size
        image.verify()
    with opener() as image:
        image.load()
        decoded_format = str(image.format or "").upper()
        decoded_size = image.size
    if decoded_format != verified_format or decoded_size != verified_size:
        raise OSError("image metadata changed between verification and full decode")
    return decoded_format, decoded_size


def generate_comicinfo_stub(
    source,
    page_count,
    *,
    series=None,
    title=None,
    number=None,
    volume=None,
    comic_format=None,
):
    source = Path(source)
    series_value = str(series if series is not None else source.name).strip()
    if not series_value:
        raise ImageFolderError("comicinfo_series_required")

    root = ElementTree.Element("ComicInfo")
    fields = (
        ("Series", series_value),
        ("Title", title),
        ("Number", number),
        ("Volume", volume),
        ("Format", comic_format),
        ("PageCount", str(int(page_count))),
    )
    for name, value in fields:
        if value is None or str(value).strip() == "":
            continue
        ElementTree.SubElement(root, name).text = str(value).strip()
    ElementTree.indent(root, space="  ")
    data = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
    _validate_xml(data, source="generated ComicInfo.xml")
    return data + b"\n"


def _validate_image(path):
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in ALLOWED_IMAGE_EXTENSIONS:
        raise ImageFolderError("unsupported_file", path.name)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ImageFolderError("image_unreadable", f"{path.name}: {exc}") from exc
    if size <= 0:
        raise ImageFolderError("image_empty", path.name)

    try:
        detected_format, (width, height) = _fully_decode_image(lambda: Image.open(path))
    except (
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
        Image.DecompressionBombError,
    ) as exc:
        raise ImageFolderError("image_invalid", f"{path.name}: {exc}") from exc

    expected_format = EXPECTED_IMAGE_FORMATS[suffix]
    if detected_format != expected_format:
        raise ImageFolderError(
            "image_extension_mismatch",
            {"file": path.name, "extension": suffix, "detected_format": detected_format},
        )
    if width <= 0 or height <= 0:
        raise ImageFolderError("image_dimensions_invalid", path.name)

    try:
        digest = _sha256_file(path)
    except OSError as exc:
        raise ImageFolderError("image_unreadable", f"{path.name}: {exc}") from exc
    return {
        "path": path,
        "source_name": path.name,
        "source_bytes": size,
        "source_sha256": digest,
        "width": width,
        "height": height,
        "format": detected_format,
        "extension": suffix,
    }


def inspect_image_folder(source):
    source_input = Path(source).expanduser()
    if source_input.is_symlink():
        raise ImageFolderError("source_symlink_not_allowed", str(source_input))
    try:
        source_path = source_input.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ImageFolderError("source_not_found", str(source_input)) from exc
    except OSError as exc:
        raise ImageFolderError("source_unreadable", f"{source_input}: {exc}") from exc
    if not source_path.is_dir():
        raise ImageFolderError("source_not_a_directory", str(source_path))

    pages = []
    comicinfo_paths = []
    try:
        entries = sorted(source_path.iterdir(), key=lambda item: natural_sort_key(item.name))
    except OSError as exc:
        raise ImageFolderError("source_unreadable", f"{source_path}: {exc}") from exc
    for entry in entries:
        if entry.is_symlink():
            raise ImageFolderError("source_member_symlink_not_allowed", entry.name)
        if entry.is_dir():
            raise ImageFolderError("source_has_subdirectories", entry.name)
        if not entry.is_file():
            raise ImageFolderError("source_member_not_a_file", entry.name)
        if entry.name.casefold() == "comicinfo.xml":
            comicinfo_paths.append(entry)
            continue
        pages.append(_validate_image(entry))

    if not pages:
        raise ImageFolderError("no_page_images")
    if len(comicinfo_paths) > 1:
        raise ImageFolderError("multiple_comicinfo_files", [path.name for path in comicinfo_paths])

    comicinfo = None
    if comicinfo_paths:
        comicinfo_path = comicinfo_paths[0]
        try:
            comicinfo = comicinfo_path.read_bytes()
        except OSError as exc:
            raise ImageFolderError("comicinfo_unreadable", str(exc)) from exc
        _validate_xml(comicinfo, source=comicinfo_path)

    return {
        "source": source_path,
        "pages": pages,
        "comicinfo": comicinfo,
        "comicinfo_path": comicinfo_paths[0] if comicinfo_paths else None,
    }


def _destination_path(source, destination):
    source = Path(source)
    if destination is None:
        destination = source.parent / f"{source.name}.cbz"
    destination_input = Path(destination).expanduser()
    if destination_input.suffix.lower() != ".cbz":
        raise ImageFolderError("destination_must_be_cbz", str(destination_input))
    if destination_input.is_symlink():
        raise ImageFolderError("destination_symlink_not_allowed", str(destination_input))
    try:
        destination = destination_input.resolve(strict=False)
    except OSError as exc:
        raise ImageFolderError("destination_invalid", f"{destination}: {exc}") from exc
    try:
        destination.relative_to(source)
    except ValueError:
        pass
    else:
        raise ImageFolderError("destination_inside_source", str(destination))
    return destination


def _archive_page_manifest(pages):
    width = max(4, len(str(len(pages))))
    manifest = []
    for index, page in enumerate(pages, 1):
        stored_name = f"{index:0{width}d}{page['extension']}"
        manifest.append({**page, "stored_name": stored_name})
    return manifest


def _write_archive(path, pages, comicinfo):
    try:
        with zipfile.ZipFile(
            path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
            strict_timestamps=False,
        ) as archive:
            for page in pages:
                archive.write(page["path"], page["stored_name"])
            if comicinfo is not None:
                archive.writestr("ComicInfo.xml", comicinfo)
    except (OSError, ValueError, zipfile.LargeZipFile) as exc:
        raise ImageFolderError("archive_write_failed", str(exc)) from exc


def _create_archive_temp(destination):
    """Create the staging file with normal output permissions, including umask."""
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    for _ in range(TEMP_NAME_ATTEMPTS):
        candidate = destination.parent / (
            f".{destination.name}.{secrets.token_hex(12)}.tmp"
        )
        try:
            descriptor = os.open(candidate, flags, 0o666)
        except FileExistsError:
            continue
        except OSError as exc:
            raise ImageFolderError("destination_temp_create_failed", str(exc)) from exc
        try:
            os.close(descriptor)
        except OSError as exc:
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                pass
            raise ImageFolderError("destination_temp_create_failed", str(exc)) from exc
        return candidate
    raise ImageFolderError("destination_temp_name_exhausted", str(destination.parent))


def _preserve_overwrite_mode(temp_path, destination_path):
    """Keep POSIX permission bits; replacement ownership remains with the caller."""
    if os.name != "posix":
        return
    try:
        destination_stat = destination_path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(destination_stat.st_mode):
        raise ImageFolderError("destination_symlink_not_allowed", str(destination_path))
    if not stat.S_ISREG(destination_stat.st_mode):
        raise ImageFolderError("destination_not_a_file", str(destination_path))
    try:
        temp_path.chmod(stat.S_IMODE(destination_stat.st_mode))
    except OSError as exc:
        raise ImageFolderError("destination_mode_preserve_failed", str(exc)) from exc


def validate_cbz(path, pages, comicinfo=None):
    path = Path(path)
    expected_names = [page["stored_name"] for page in pages]
    if comicinfo is not None:
        expected_names.append("ComicInfo.xml")
    try:
        with zipfile.ZipFile(path, "r") as archive:
            names = archive.namelist()
            if names != expected_names:
                raise ImageFolderError(
                    "output_member_order_mismatch",
                    {"expected": expected_names, "found": names},
                )
            if len(names) != len(set(names)):
                raise ImageFolderError("output_duplicate_member")
            for page in pages:
                member = archive.getinfo(page["stored_name"])
                if member.file_size != page["source_bytes"]:
                    raise ImageFolderError(
                        "output_page_size_mismatch",
                        {
                            "member": page["stored_name"],
                            "expected": page["source_bytes"],
                            "found": member.file_size,
                        },
                    )
            if comicinfo is not None:
                member = archive.getinfo("ComicInfo.xml")
                if member.file_size != len(comicinfo):
                    raise ImageFolderError("output_comicinfo_size_mismatch")
            bad_member = archive.testzip()
            if bad_member is not None:
                raise ImageFolderError("output_crc_failed", bad_member)
            for page in pages:
                data = archive.read(page["stored_name"])
                if _sha256_bytes(data) != page["source_sha256"]:
                    raise ImageFolderError("output_page_content_mismatch", page["stored_name"])
                try:
                    output_format, output_size = _fully_decode_image(
                        lambda: Image.open(io.BytesIO(data))
                    )
                except (
                    UnidentifiedImageError,
                    OSError,
                    SyntaxError,
                    ValueError,
                    Image.DecompressionBombError,
                ) as exc:
                    raise ImageFolderError(
                        "output_page_invalid", f"{page['stored_name']}: {exc}"
                    ) from exc
                if output_format != page["format"] or output_size != (page["width"], page["height"]):
                    raise ImageFolderError("output_page_metadata_mismatch", page["stored_name"])
            if comicinfo is not None:
                output_comicinfo = archive.read("ComicInfo.xml")
                if output_comicinfo != comicinfo:
                    raise ImageFolderError("output_comicinfo_content_mismatch")
                _validate_xml(output_comicinfo, source="output ComicInfo.xml")
    except ImageFolderError:
        raise
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise ImageFolderError("output_archive_invalid", str(exc)) from exc
    return {
        "ok": True,
        "page_count": len(pages),
        "members": expected_names,
        "comicinfo": comicinfo is not None,
        "bytes": path.stat().st_size,
        "sha256": _sha256_file(path),
    }


def convert_image_folder(
    source,
    destination=None,
    *,
    overwrite=False,
    generate_comicinfo=False,
    series=None,
    title=None,
    number=None,
    volume=None,
    comic_format=None,
):
    inspection = inspect_image_folder(source)
    source_path = inspection["source"]
    destination_path = _destination_path(source_path, destination)

    metadata_values = (series, title, number, volume, comic_format)
    if any(value is not None for value in metadata_values) and not generate_comicinfo:
        raise ImageFolderError("comicinfo_flag_required_for_metadata")
    if inspection["comicinfo"] is not None and generate_comicinfo:
        raise ImageFolderError("comicinfo_already_present", str(inspection["comicinfo_path"]))

    comicinfo = inspection["comicinfo"]
    comicinfo_generated = False
    if generate_comicinfo:
        comicinfo = generate_comicinfo_stub(
            source_path,
            len(inspection["pages"]),
            series=series,
            title=title,
            number=number,
            volume=volume,
            comic_format=comic_format,
        )
        comicinfo_generated = True

    if destination_path.is_symlink():
        raise ImageFolderError("destination_symlink_not_allowed", str(destination_path))
    if destination_path.exists() and not overwrite:
        raise ImageFolderError("destination_exists", str(destination_path))
    if destination_path.exists() and not destination_path.is_file():
        raise ImageFolderError("destination_not_a_file", str(destination_path))

    try:
        destination_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ImageFolderError("destination_parent_unwritable", str(exc)) from exc

    pages = _archive_page_manifest(inspection["pages"])
    temp_path = None
    try:
        temp_path = _create_archive_temp(destination_path)
        _write_archive(temp_path, pages, comicinfo)
        validation = validate_cbz(temp_path, pages, comicinfo)
        if overwrite:
            _preserve_overwrite_mode(temp_path, destination_path)
            os.replace(temp_path, destination_path)
        else:
            # A same-directory hard link is the portable atomic no-clobber
            # primitive here. Unsupported filesystems fail closed rather than
            # falling back to a check-then-replace race.
            os.link(temp_path, destination_path)
    except ImageFolderError:
        raise
    except FileExistsError as exc:
        raise ImageFolderError("destination_exists", str(destination_path)) from exc
    except OSError as exc:
        raise ImageFolderError("destination_publish_failed", str(exc)) from exc
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    public_pages = [
        {
            key: value
            for key, value in page.items()
            if key not in {"path"}
        }
        for page in pages
    ]
    return {
        "ok": True,
        "schema": SCHEMA,
        "source": str(source_path),
        "destination": str(destination_path),
        "page_count": len(public_pages),
        "pages": public_pages,
        "comicinfo": comicinfo is not None,
        "comicinfo_generated": comicinfo_generated,
        "source_preserved": True,
        "validation": validation,
    }


def build_parser():
    parser = argparse.ArgumentParser(
        description="Build a validated CBZ from one folder of JPG, JPEG, or PNG pages.",
        epilog=(
            "Atomic publication without --overwrite requires hard-link support in "
            "the destination directory."
        ),
    )
    parser.add_argument("source", help="Directory containing the page images.")
    parser.add_argument("destination", nargs="?", help="Output .cbz path. Defaults beside the source folder.")
    parser.add_argument("-o", "--output", help="Output .cbz path. Cannot be combined with positional destination.")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help=(
            "Replace an existing output after validation. On POSIX, mode bits are "
            "preserved; replacement ownership belongs to the caller."
        ),
    )
    parser.add_argument("--comicinfo", action="store_true", help="Generate a minimal ComicInfo.xml when the source does not contain one.")
    parser.add_argument("--series", help="ComicInfo Series. Defaults to the source folder name.")
    parser.add_argument("--title", help="Optional ComicInfo Title.")
    parser.add_argument("--number", help="Optional ComicInfo Number.")
    parser.add_argument("--volume", help="Optional ComicInfo Volume.")
    parser.add_argument("--comic-format", help="Optional ComicInfo Format, such as Comic or Manga.")
    parser.add_argument("--json", action="store_true", help="Print a machine-readable result.")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.destination and args.output:
        parser.error("use either positional destination or --output, not both")
    destination = args.output or args.destination
    try:
        result = convert_image_folder(
            args.source,
            destination,
            overwrite=args.overwrite,
            generate_comicinfo=args.comicinfo,
            series=args.series,
            title=args.title,
            number=args.number,
            volume=args.volume,
            comic_format=args.comic_format,
        )
    except ImageFolderError as exc:
        payload = {"ok": False, "schema": SCHEMA, "reason": exc.reason}
        if exc.detail is not None:
            payload["detail"] = exc.detail
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            suffix = f": {exc.detail}" if exc.detail is not None else ""
            print(f"{exc.reason}{suffix}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        comicinfo_text = " with ComicInfo.xml" if result["comicinfo"] else ""
        print(
            f"created {result['destination']} from {result['page_count']} pages{comicinfo_text}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
