#!/usr/bin/env python3
"""Prove CBR-to-CBZ conversion keeps every page and refuses anything it cannot verify.

The interesting cases are the ones where conversion must NOT happen: a name
collision, a pack hiding inside the archive, a member that would escape the
extraction directory, a half-extracted RAR. Those are all checked here against
real files on disk.

True RAR content needs 7z or unrar, which this test does not assume exist. The
RAR paths are covered two ways instead: the tooling-missing refusals run for
real, and the extract step is swapped for a stub that fakes a partial
extraction so the page-count guard is exercised end to end.
"""

import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

import inkdrop_archive_conversion as conversion


COMICINFO = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    "<ComicInfo>\n"
    "  <Series>Berserk</Series>\n"
    "  <Volume>3</Volume>\n"
    "  <Format>Manga</Format>\n"
    "</ComicInfo>\n"
)

# A 1x1 PNG. Small, but real bytes with a real signature.
PIXEL = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6360000002000100fdff03fa0000"
    "000049454e44ae426082"
)


def require(value, message):
    if not value:
        raise AssertionError(message)


def page_bytes(index):
    """Distinct bytes per page, so a swapped or duplicated page is detectable."""
    return PIXEL + f"page-{index}".encode("utf-8")


def write_zip_archive(path, members):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in members:
            archive.writestr(name, data)
    return path


def standard_pages():
    # Deliberately out of ASCII order: page10 sorts before page2 as a string.
    return [(f"page{index}.png", page_bytes(index)) for index in (1, 2, 10, 11)]


def fake_rar(path, payload=b""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"Rar!\x1a\x07\x00" + payload)
    return path


def test_zip_named_cbr_converts(root, originals):
    source = write_zip_archive(
        root / "Berserk" / "Berserk v03.cbr",
        standard_pages() + [("ComicInfo.xml", COMICINFO.encode("utf-8"))],
    )
    source_size = source.stat().st_size

    result = conversion.convert_archive(source, root=root, originals_dir=originals, keep_original=True)
    require(result["converted"], f"zip-named cbr should convert: {result}")
    require(result["reason"] == "converted", result["reason"])
    require(result["source_format"] == "zip", result["source_format"])

    dest = root / "Berserk" / "Berserk v03.cbz"
    require(dest.is_file(), "cbz written next to the original")
    require(not source.exists(), "original no longer sits in the library folder")

    retired = originals / root.name / "Berserk" / "Berserk v03.cbr"
    require(retired.is_file(), f"original retired to {retired}")
    require(retired.stat().st_size == source_size, "retired original is byte-identical in size")

    with zipfile.ZipFile(dest) as archive:
        names = archive.namelist()
        images = [name for name in names if name.lower().endswith(".png")]
        require(len(images) == 4, f"all four pages carried across: {names}")
        require(images == sorted(images), "stored page names sort correctly as strings")
        # Natural order: the page written as page10 must land third, not second.
        require(archive.read(images[0]) == page_bytes(1), "page 1 first")
        require(archive.read(images[1]) == page_bytes(2), "page 2 second, not page10")
        require(archive.read(images[2]) == page_bytes(10), "page 10 third")
        require(archive.read(images[3]) == page_bytes(11), "page 11 last")
        require(archive.read("ComicInfo.xml").decode("utf-8") == COMICINFO, "ComicInfo.xml carried across verbatim")
        require(archive.testzip() is None, "output zip passes its own CRC check")

    require(result["comicinfo_preserved"], "conversion reports the metadata it kept")
    require(result["page_count"] == 4, result["page_count"])
    require(result["validation"]["ok"], result["validation"])


def test_pages_are_byte_identical(root, originals):
    members = [("001.jpg", page_bytes(101)), ("002.jpg", page_bytes(102))]
    source = write_zip_archive(root / "Saga" / "Saga 001.cbr", members)
    digests = {name: hashlib.sha256(data).hexdigest() for name, data in members}

    result = conversion.convert_archive(source, root=root, originals_dir=originals, keep_original=True)
    require(result["converted"], result)

    with zipfile.ZipFile(root / "Saga" / "Saga 001.cbz") as archive:
        written = sorted(hashlib.sha256(archive.read(name)).hexdigest() for name in archive.namelist())
    require(written == sorted(digests.values()), "page bytes are untouched by the repack")


def test_destination_collision_refused(root, originals):
    source = write_zip_archive(root / "Akira" / "Akira v01.cbr", standard_pages())
    existing = write_zip_archive(root / "Akira" / "Akira v01.cbz", [("001.png", PIXEL)])
    existing_bytes = existing.read_bytes()

    result = conversion.convert_archive(source, root=root, originals_dir=originals, keep_original=True)
    require(not result["converted"], "must not overwrite an existing cbz")
    require(result["reason"] == "destination_exists", result["reason"])
    require(source.is_file(), "original left in place after a refusal")
    require(existing.read_bytes() == existing_bytes, "existing cbz untouched")


def test_nested_archive_refused(root, originals):
    source = write_zip_archive(
        root / "Packs" / "Berserk v01-v10.cbr",
        [("001.png", PIXEL), ("Berserk v02.cbz", b"PK\x03\x04nested")],
    )
    result = conversion.convert_archive(source, root=root, originals_dir=originals, keep_original=True)
    require(not result["converted"], "a pack is not a single issue")
    require(result["reason"] == "nested_archive_member", result["reason"])
    require(source.is_file(), "pack left alone")
    require(not (root / "Packs" / "Berserk v01-v10.cbz").exists(), "no half-written output")


def test_no_images_refused(root, originals):
    source = write_zip_archive(root / "Junk" / "notes.cbr", [("readme.txt", b"nothing to see")])
    result = conversion.convert_archive(source, root=root, originals_dir=originals, keep_original=True)
    require(not result["converted"], "an archive with no pages is not a comic")
    require(result["reason"] == "no_image_members", result["reason"])
    require(source.is_file(), "left alone")


def test_corrupt_source_refused(root, originals):
    source = root / "Broken" / "Broken 001.cbr"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"PK\x03\x04" + os.urandom(64))
    result = conversion.convert_archive(source, root=root, originals_dir=originals, keep_original=True)
    require(not result["converted"], "a corrupt zip must not produce a cbz")
    require(result["reason"] in {"source_archive_unreadable", "source_member_crc_failed"}, result["reason"])
    require(source.is_file(), "corrupt original left for the operator to look at")
    require(not (root / "Broken" / "Broken 001.cbz").exists(), "no output written")


def test_traversal_member_refused(root, originals):
    source = root / "Evil" / "Evil 001.cbr"
    source.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("001.png", PIXEL)
        archive.writestr("../../escaped.png", PIXEL)
    outside = root.parent / "escaped.png"

    result = conversion.convert_archive(source, root=root, originals_dir=originals, keep_original=True)
    require(not result["converted"], "a member escaping the extract directory must stop the conversion")
    require(result["reason"] == "source_member_path_unsafe", result["reason"])
    require(not outside.exists(), "nothing was written outside the extraction directory")


def test_empty_page_refused(root, originals):
    source = write_zip_archive(root / "Hollow" / "Hollow 001.cbr", [("001.png", PIXEL), ("002.png", b"")])
    result = conversion.convert_archive(source, root=root, originals_dir=originals, keep_original=True)
    require(not result["converted"], "a zero-byte page means the source is damaged")
    require(result["reason"] == "source_page_empty", result["reason"])
    require(source.is_file(), "left alone")


def test_dropped_members_are_reported(root, originals):
    source = write_zip_archive(
        root / "Extras" / "Extras 001.cbr",
        [("001.png", PIXEL), ("credits.txt", b"scanned by someone"), ("thumbs.db", b"junk")],
    )
    result = conversion.convert_archive(source, root=root, originals_dir=originals, keep_original=True)
    require(result["converted"], result)
    require(result["dropped_members"] == ["credits.txt", "thumbs.db"], result["dropped_members"])
    with zipfile.ZipFile(root / "Extras" / "Extras 001.cbz") as archive:
        require(archive.namelist() == ["0001.png"], archive.namelist())


def test_dry_run_changes_nothing(root, originals):
    source = write_zip_archive(root / "Dry" / "Dry 001.cbr", standard_pages())
    before = sorted(str(path.relative_to(root)) for path in root.rglob("*"))

    result = conversion.convert_archive(source, root=root, originals_dir=originals, keep_original=True, dry_run=True)
    require(result["ok"] and not result["converted"], result)
    require(result["reason"] == "convertible", result["reason"])
    require(result["dest"].endswith("Dry 001.cbz"), result["dest"])
    require(result["original_moved_to"].endswith(os.path.join("Dry", "Dry 001.cbr")), result["original_moved_to"])

    after = sorted(str(path.relative_to(root)) for path in root.rglob("*"))
    require(before == after, "dry run left the library exactly as it found it")


def test_discard_originals(root, originals):
    source = write_zip_archive(root / "Discard" / "Discard 001.cbr", standard_pages())
    result = conversion.convert_archive(source, root=root, originals_dir=originals, keep_original=False)
    require(result["converted"], result)
    require(not source.exists(), "original deleted when asked")
    require(not (originals / root.name / "Discard").exists(), "nothing retired when discarding")
    require((root / "Discard" / "Discard 001.cbz").is_file(), "cbz written")


def test_originals_slot_collision_refused(root, originals):
    source = write_zip_archive(root / "Twice" / "Twice 001.cbr", standard_pages())
    occupied = originals / root.name / "Twice" / "Twice 001.cbr"
    occupied.parent.mkdir(parents=True, exist_ok=True)
    occupied.write_bytes(b"an earlier run put something here")

    result = conversion.convert_archive(source, root=root, originals_dir=originals, keep_original=True)
    require(not result["converted"], "must not clobber a previously retired original")
    require(result["reason"] == "original_archive_slot_taken", result["reason"])
    require(source.is_file(), "source left alone")
    require(occupied.read_bytes() == b"an earlier run put something here", "earlier original untouched")


def test_validation_catches_a_bad_output():
    with tempfile.TemporaryDirectory(prefix="inkdrop-validate-") as tmp:
        tmp = Path(tmp)
        good = write_zip_archive(tmp / "good.cbz", [("0001.png", page_bytes(1)), ("0002.png", page_bytes(2))])
        build_meta = {
            "pages": [
                {"stored_name": "0001.png", "sha256": hashlib.sha256(page_bytes(1)).hexdigest()},
                {"stored_name": "0002.png", "sha256": hashlib.sha256(page_bytes(2)).hexdigest()},
            ],
            "comicinfo_preserved": False,
        }
        require(conversion.validate_cbz(good, build_meta)["ok"], "a correct archive validates")

        swapped = write_zip_archive(tmp / "swapped.cbz", [("0001.png", page_bytes(2)), ("0002.png", page_bytes(1))])
        verdict = conversion.validate_cbz(swapped, build_meta)
        require(not verdict["ok"] and verdict["reason"] == "output_page_content_mismatch", verdict)

        short = write_zip_archive(tmp / "short.cbz", [("0001.png", page_bytes(1))])
        verdict = conversion.validate_cbz(short, build_meta)
        require(not verdict["ok"] and verdict["reason"] == "output_page_count_mismatch", verdict)

        missing_info = write_zip_archive(tmp / "noinfo.cbz", [("0001.png", page_bytes(1)), ("0002.png", page_bytes(2))])
        verdict = conversion.validate_cbz(missing_info, {**build_meta, "comicinfo_preserved": True})
        require(not verdict["ok"] and verdict["reason"] == "output_comicinfo_missing", verdict)

        torn = tmp / "torn.cbz"
        shutil.copy2(good, torn)
        raw = bytearray(torn.read_bytes())
        # Flip a byte inside the first stored member so the stored CRC no longer matches.
        raw[40] ^= 0xFF
        torn.write_bytes(bytes(raw))
        verdict = conversion.validate_cbz(torn, build_meta)
        require(not verdict["ok"], f"a tampered archive must not validate: {verdict}")


def test_container_sniffing(root):
    zip_named_cbr = write_zip_archive(root / "Sniff" / "a.cbr", [("001.png", PIXEL)])
    rar_named_cbz = fake_rar(root / "Sniff" / "b.cbz")
    require(conversion.archive_container_format(zip_named_cbr) == "zip", "zip bytes recognised regardless of extension")
    require(conversion.archive_container_format(rar_named_cbz) == "rar", "rar bytes recognised regardless of extension")
    junk = root / "Sniff" / "c.cbr"
    junk.write_bytes(b"not an archive at all")
    require(conversion.archive_container_format(junk) == "unknown", "unknown bytes reported honestly")


def test_natural_sort():
    names = ["page10.png", "page2.png", "page1.png", "Page20.png"]
    ordered = sorted(names, key=conversion.natural_sort_key)
    require(ordered == ["page1.png", "page2.png", "page10.png", "Page20.png"], ordered)


def write_page_directory(path, names, comicinfo=False):
    path.mkdir(parents=True, exist_ok=True)
    for name in names:
        (path / name).write_bytes(PIXEL)
    if comicinfo:
        (path / "ComicInfo.xml").write_text("<ComicInfo><Series>Love and Rockets</Series></ComicInfo>")
    return path


def test_page_directory_conversion(root, originals):
    # Some Soulseek uploaders share a comic as the raw scans: one folder per
    # issue, page-by-page .jpg, no archive. Live example that InkDrop could not
    # read at all: "Love & Rockets v1 (1-50)/Love and rockets v1 001/" (280
    # images). The page-pack builder only takes HTTP URLs, and the archive
    # converter only takes archives.
    source = write_page_directory(
        root / "Pages" / "Love and rockets v1 001",
        ["10.jpg", "2.jpg", "1.jpg", "3.jpg"],
        comicinfo=True,
    )
    result = conversion.convert_page_directory(source, root=root, originals_dir=originals)
    require(result["converted"], result)
    dest = Path(result["dest"])
    require(dest.is_file() and dest.name == "Love and rockets v1 001.cbz", result["dest"])
    require(not source.exists(), "pages retired out of the library")
    require(Path(result["original_moved_to"]).is_dir(), result["original_moved_to"])

    # Page order is the whole point: lexical sort would read 1, 10, 2, 3.
    require(
        [entry["source_name"] for entry in result["build"]["pages"]]
        == ["1.jpg", "2.jpg", "3.jpg", "10.jpg"],
        result["build"]["pages"],
    )
    with zipfile.ZipFile(dest) as archive:
        names = archive.namelist()
    require([n for n in names if n.endswith(".jpg")] == ["0001.jpg", "0002.jpg", "0003.jpg", "0004.jpg"], names)
    require("ComicInfo.xml" in names, names)
    require(result["validation"]["ok"], result["validation"])


def test_page_directory_guards(root, originals):
    # A page folder is one issue. The live example sits inside a folder holding
    # 50 sibling issues, so descending would silently fuse a whole run into one
    # unreadable CBZ.
    nested = write_page_directory(root / "Guards" / "run", ["1.jpg", "2.jpg", "3.jpg"])
    (nested / "Love and rockets v1 002").mkdir()
    result = conversion.convert_page_directory(nested, root=root, originals_dir=originals)
    require(result.get("reason") == "page_directory_has_subdirectories", result)
    require(nested.is_dir(), "a refused folder is left exactly as it was")

    mixed = write_page_directory(root / "Guards" / "mixed", ["1.jpg", "2.jpg", "3.jpg"])
    write_zip_archive(mixed / "already.cbz", [("001.png", PIXEL)])
    require(
        conversion.convert_page_directory(mixed, root=root, originals_dir=originals).get("reason")
        == "nested_archive_member",
        "a folder that already holds an archive is ambiguous",
    )

    # A stray cover or a thumbnail folder is not an issue.
    thin = write_page_directory(root / "Guards" / "thin", ["cover.jpg"])
    require(
        conversion.convert_page_directory(thin, root=root, originals_dir=originals).get("reason") == "too_few_pages",
        "a single image is not a comic",
    )

    empty = (root / "Guards" / "empty")
    empty.mkdir(parents=True, exist_ok=True)
    (empty / "notes.txt").write_text("no scans here")
    require(
        conversion.convert_page_directory(empty, root=root, originals_dir=originals).get("reason") == "no_image_members",
        "no pages, no archive",
    )

    require(
        conversion.convert_page_directory(root / "Guards" / "absent", root=root, originals_dir=originals).get("reason")
        == "source_not_a_directory",
        "a missing folder is reported, not raised",
    )


def test_page_directory_dry_run_changes_nothing(root, originals):
    source = write_page_directory(root / "PagesDry" / "issue 001", ["1.jpg", "2.jpg", "3.jpg"])
    before = sorted(str(path.relative_to(root)) for path in root.rglob("*"))
    result = conversion.convert_page_directory(source, root=root, originals_dir=originals, dry_run=True)
    require(result["ok"] and not result["converted"], result)
    require(result["reason"] == "convertible", result["reason"])
    after = sorted(str(path.relative_to(root)) for path in root.rglob("*"))
    require(before == after, "dry run left the library exactly as it found it")


def test_scan_skips_internal_dirs(root):
    write_zip_archive(root / "_Incoming" / "staged.cbr", [("001.png", PIXEL)])
    write_zip_archive(root / "_quarantine" / "held.cbr", [("001.png", PIXEL)])
    write_zip_archive(root / "Visible" / "visible.cbr", [("001.png", PIXEL)])
    fake_rar(root / "Visible" / "true-rar.cbr")
    fake_rar(root / "Visible" / "mislabeled.cbz")
    write_zip_archive(root / "Visible" / "fine.cbz", [("001.png", PIXEL)])

    scan = conversion.scan_library([root])
    paths = {Path(item["path"]).name: item for item in scan["candidates"]}
    require("staged.cbr" not in paths, "internal _Incoming is not part of the library pass")
    require("held.cbr" not in paths, "internal _quarantine is not part of the library pass")
    require("fine.cbz" not in paths, "a healthy cbz is not a candidate")
    require(paths["visible.cbr"]["classification"] == "zip_named_cbr", paths["visible.cbr"])
    require(paths["true-rar.cbr"]["classification"] == "rar", paths["true-rar.cbr"])
    require(paths["mislabeled.cbz"]["classification"] == "rar_named_cbz", paths["mislabeled.cbz"])
    require(not paths["mislabeled.cbz"]["convertible"], "mislabeled cbz is reported but not touched by default")

    opted_in = conversion.scan_library([root], include_mislabeled_cbz=True)
    opted = {Path(item["path"]).name: item for item in opted_in["candidates"]}
    require(opted["mislabeled.cbz"]["convertible"], "mislabeled cbz becomes convertible when asked for")

    missing = conversion.scan_library([root / "does-not-exist"])
    require(missing["skipped_roots"][0]["reason"] == "root_missing", missing["skipped_roots"])
    require(not missing["candidates"], "a missing root contributes nothing")


def test_rar_without_tooling(root, originals, monkeypatched_no_tools):
    source = fake_rar(root / "NeedsRar" / "Vagabond v01.cbr")
    result = conversion.convert_archive(source, root=root, originals_dir=originals, keep_original=True)
    require(not result["converted"], "cannot convert a rar with no rar tooling")
    require(result["reason"] == "rar_tooling_missing", result["reason"])
    require(source.is_file(), "source left alone")

    summary = conversion.convert_library([root], dry_run=False, originals_dir=originals)
    require(not summary["ok"], "the whole run refuses rather than half-converting")
    require(summary["reason"] == "rar_tooling_missing", summary["reason"])
    require(summary["converted"] == 0, "nothing converted")
    require(source.is_file(), "still there after the refused run")

    plan = conversion.convert_library([root], dry_run=True, originals_dir=originals)
    require(plan["needs_rar_tooling"] >= 1, plan["needs_rar_tooling"])
    require(not plan["rar_tooling"]["available"], "the plan says the tooling is missing before anything runs")


def test_partial_rar_extraction_refused(root, originals):
    """A RAR that lists 4 pages but yields 2 must not become a 2-page CBZ."""
    source = fake_rar(root / "Partial" / "Partial v01.cbr")

    listed = ["page1.png", "page2.png", "page10.png", "page11.png"]

    def fake_list(path, container):
        return list(listed)

    def fake_extract(path, workdir):
        # Only half the pages survive, the way a damaged RAR behaves.
        for index in (1, 2):
            (workdir / f"page{index}.png").write_bytes(page_bytes(index))
        return {"extractor": "stub"}

    original_list = conversion.list_archive_members
    original_extract = conversion._extract_rar
    conversion.list_archive_members = fake_list
    conversion._extract_rar = fake_extract
    try:
        result = conversion.convert_archive(source, root=root, originals_dir=originals, keep_original=True)
    finally:
        conversion.list_archive_members = original_list
        conversion._extract_rar = original_extract

    require(not result["converted"], "half an issue is not an issue")
    require(result["reason"] == "partial_extraction", result["reason"])
    require(result["detail"] == {"listed": 4, "extracted": 2}, result["detail"])
    require(source.is_file(), "damaged source left for the operator")
    require(not (root / "Partial" / "Partial v01.cbz").exists(), "no truncated cbz left behind")


def test_stubbed_rar_conversion_succeeds(root, originals):
    """The RAR path end to end, with extraction stubbed so no rar tool is needed."""
    source = fake_rar(root / "Stubbed" / "Stubbed v01.cbr")
    listed = ["page1.png", "page2.png", "ComicInfo.xml"]

    def fake_list(path, container):
        return list(listed)

    def fake_extract(path, workdir):
        (workdir / "page1.png").write_bytes(page_bytes(1))
        (workdir / "page2.png").write_bytes(page_bytes(2))
        (workdir / "ComicInfo.xml").write_bytes(COMICINFO.encode("utf-8"))
        return {"extractor": "stub"}

    original_list = conversion.list_archive_members
    original_extract = conversion._extract_rar
    conversion.list_archive_members = fake_list
    conversion._extract_rar = fake_extract
    try:
        result = conversion.convert_archive(source, root=root, originals_dir=originals, keep_original=True)
    finally:
        conversion.list_archive_members = original_list
        conversion._extract_rar = original_extract

    require(result["converted"], result)
    require(result["source_format"] == "rar", result["source_format"])
    require(result["comicinfo_preserved"], "metadata survived the rar path too")
    dest = root / "Stubbed" / "Stubbed v01.cbz"
    with zipfile.ZipFile(dest) as archive:
        require(archive.read("ComicInfo.xml").decode("utf-8") == COMICINFO, "ComicInfo.xml verbatim")
        require(archive.read("0001.png") == page_bytes(1), "page 1 intact")
    require((originals / root.name / "Stubbed" / "Stubbed v01.cbr").is_file(), "original retired")


def test_batch_pass_and_limit(root, originals):
    for index in range(3):
        write_zip_archive(root / "Batch" / f"Batch {index:03d}.cbr", [("001.png", page_bytes(index))])

    plan = conversion.convert_library([root / "Batch"], dry_run=True, originals_dir=originals)
    require(plan["convertible_count"] == 3, plan["convertible_count"])
    require(plan["converted"] == 0 and plan["dry_run"], plan)
    require(all((root / "Batch" / f"Batch {index:03d}.cbr").is_file() for index in range(3)), "plan touched nothing")

    limited = conversion.convert_library([root / "Batch"], dry_run=False, limit=2, originals_dir=originals)
    require(limited["converted"] == 2, limited["converted"])
    require(limited["ok"], limited)
    remaining = sorted(path.name for path in (root / "Batch").glob("*.cbr"))
    require(len(remaining) == 1, f"limit respected, one left: {remaining}")
    require(len(sorted((root / "Batch").glob("*.cbz"))) == 2, "two cbz produced")
    limited_reasons = [item["reason"] for item in limited["results"]]
    require("limit_reached" in limited_reasons, limited_reasons)

    rest = conversion.convert_library([root / "Batch"], dry_run=False, originals_dir=originals)
    require(rest["converted"] == 1, rest["converted"])
    require(not sorted((root / "Batch").glob("*.cbr")), "nothing left to convert")

    settled = conversion.convert_library([root / "Batch"], dry_run=True, originals_dir=originals)
    require(settled["convertible_count"] == 0, "a converted library reports nothing left to do")


def test_batch_survives_one_bad_file(root, originals):
    write_zip_archive(root / "Mixed" / "good.cbr", [("001.png", PIXEL)])
    write_zip_archive(root / "Mixed" / "packed.cbr", [("001.png", PIXEL), ("inner.cbz", b"PK\x03\x04")])
    write_zip_archive(root / "Mixed" / "also-good.cbr", [("001.png", page_bytes(9))])

    summary = conversion.convert_library([root / "Mixed"], dry_run=False, originals_dir=originals)
    require(summary["converted"] == 2, summary["converted"])
    require(summary["failed"] == 1, summary["failed"])
    require(not summary["ok"], "the run reports that one file did not convert")
    require((root / "Mixed" / "packed.cbr").is_file(), "the bad file is still there, untouched")
    reasons = {Path(item["source"]).name: item.get("reason") for item in summary["results"]}
    require(reasons["packed.cbr"] == "nested_archive_member", reasons)
    require(json.dumps(summary), "the summary is JSON-serialisable for an endpoint to return")


def test_originals_dir_unwritable_refuses_run(root):
    write_zip_archive(root / "Locked" / "one.cbr", [("001.png", PIXEL)])
    blocked = root / "Locked" / "one.cbr"  # a file, so mkdir on it must fail
    summary = conversion.convert_library([root / "Locked"], dry_run=False, originals_dir=blocked / "under-a-file")
    require(not summary["ok"], "an unusable originals directory stops the run before any conversion")
    require(summary["reason"] == "originals_dir_unwritable", summary["reason"])
    require((root / "Locked" / "one.cbr").is_file(), "nothing converted")


class NoRarTools:
    def __enter__(self):
        self._saved = conversion.rar_tooling
        conversion.rar_tooling = lambda: {"sevenzip": None, "unrar": None, "available": False}
        return self

    def __exit__(self, *exc):
        conversion.rar_tooling = self._saved
        return False


def main():
    with tempfile.TemporaryDirectory(prefix="inkdrop-archive-conversion-") as tmp:
        base = Path(tmp)
        root = base / "library" / "manga"
        originals = base / "quarantine" / "converted-originals"
        root.mkdir(parents=True, exist_ok=True)

        test_zip_named_cbr_converts(root, originals)
        test_pages_are_byte_identical(root, originals)
        test_destination_collision_refused(root, originals)
        test_nested_archive_refused(root, originals)
        test_no_images_refused(root, originals)
        test_corrupt_source_refused(root, originals)
        test_traversal_member_refused(root, originals)
        test_empty_page_refused(root, originals)
        test_dropped_members_are_reported(root, originals)
        test_dry_run_changes_nothing(root, originals)
        test_discard_originals(root, originals)
        test_originals_slot_collision_refused(root, originals)
        test_validation_catches_a_bad_output()
        test_container_sniffing(root)
        test_natural_sort()
        test_partial_rar_extraction_refused(root, originals)
        test_stubbed_rar_conversion_succeeds(root, originals)
        test_batch_pass_and_limit(root, originals)
        test_batch_survives_one_bad_file(root, originals)
        test_originals_dir_unwritable_refuses_run(root)
        test_page_directory_conversion(root, originals)
        test_page_directory_guards(root, originals)
        test_page_directory_dry_run_changes_nothing(root, originals)

        scan_root = base / "library" / "scan"
        scan_root.mkdir(parents=True, exist_ok=True)
        test_scan_skips_internal_dirs(scan_root)

        rar_root = base / "library" / "rar"
        rar_root.mkdir(parents=True, exist_ok=True)
        with NoRarTools() as guard:
            test_rar_without_tooling(rar_root, originals, guard)

    print("archive conversion smoke: ok")


if __name__ == "__main__":
    main()
