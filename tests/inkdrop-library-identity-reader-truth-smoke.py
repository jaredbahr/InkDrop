#!/usr/bin/env python3
import json
import os
import tempfile
import threading
from pathlib import Path

from core import inkdrop_library_frontends as frontends
from core import inkdrop_library_identity as identity
from core import inkdrop_state
from core import inkdrop_completed_import as completed_import


def require(value, message):
    if not value:
        raise AssertionError(message)


for provider in ("prowlarr", "slskd", "suwayomi", "mangadex"):
    decision = identity.canonical_library_classification({"media_type": "manga", "source": provider, "filename": "Batman 001.cbz"})
    require(decision["library_type"] == "manga", (provider, decision))
western = identity.canonical_library_classification({"media_type": "comic", "title": "Western v01 c001", "publisher": "Viz-like name"})
require(western["library_type"] == "comics", western)
require(identity.canonical_library_classification({"media_type": "comic", "title": "Gantz"})["library_type"] == "comics", "comic identity lost")
require(identity.canonical_library_classification({"media_type": "manga", "title": "Gantz"})["library_type"] == "manga", "manga identity lost")
require(not identity.canonical_library_classification({"media_type": "manga", "work_media_type": "comic"})["ok"], "conflicting identity admitted")

naming_cases = (
    ({"unit_type": "volume", "volume_number": "1", "chapter_number": "1"}, "Series v01.cbz", "c001"),
    ({"unit_type": "volume", "volume_number": "009", "chapter_number": "009"}, "Series v09.cbz", "c009"),
    ({"unit_type": "volume", "volume_number": "52", "chapter_number": "52"}, "Series v52.cbz", "c052"),
    ({"unit_type": "volume", "volume_number": "101", "chapter_number": "101"}, "Series v101.cbz", "c101"),
    ({"unit_type": "volume", "volume_number": "10.5", "chapter_number": "10.5"}, "Series v10.5.cbz", "c10.5"),
    ({"unit_type": "chapter", "chapter_number": "52", "volume_number": "52"}, "Series c052.cbz", "v52"),
    ({"unit_type": "issue", "issue_number": "7"}, "Series #007.cbz", "c007"),
    ({"unit_type": "collected", "collected_number": "2"}, "Series Collected Edition 02.cbz", "c002"),
    ({"unit_type": "pack_member", "pack_member_number": "3"}, "Series Pack Member 003.cbz", "v03"),
)
for record, expected, forbidden in naming_cases:
    unit = identity.canonical_unit_identity(record)
    filename = identity.canonical_filename("Series", unit)
    require(filename == expected and forbidden not in filename, (record, filename))

states = {
    "imported": {"imported": True},
    "reader_scan_pending": {"imported": True, "reader_configured": True, "reader_required": True, "reader_scan_requested": True, "reader_visibility_status": "pending"},
    "reader_visible": {"imported": True, "reader_configured": True, "reader_required": True, "reader_visibility_status": "library_visible"},
    # This row used to read `scan_timeout -> reader_visibility_failed`, which pinned
    # the defect rather than the behaviour: a scan we stopped waiting for is not a
    # finding about the file. The two arms are now separate, and the adverse arm is
    # driven by a status that IS a finding about the file.
    "reader_visibility_timeout": {"imported": True, "reader_configured": True, "reader_required": True, "reader_visibility_status": "scan_timeout"},
    "reader_visibility_failed": {"imported": True, "reader_configured": True, "reader_required": True, "reader_visibility_status": "wrong_library"},
}
for expected, record in states.items():
    projection = identity.completion_projection(record)
    require(projection["state"] == expected, (expected, projection))
require(identity.completion_projection(states["reader_visible"])["complete"], "visible required reader item incomplete")
require(not identity.completion_projection(states["reader_scan_pending"])["complete"], "scan request treated as completion")
required_unavailable = identity.completion_projection({"imported": True, "reader_required": True, "reader_configured": False})
require(not required_unavailable["complete"] and required_unavailable["state"] == "reader_visibility_failed", required_unavailable)
require(identity.completion_projection({"imported": True, "reader_configured": True, "reader_required": False, "reader_visibility_status": "pending"})["complete"], "advisory reader blocked import completion")
stale = identity.completion_projection({
    "imported": True, "reader_configured": True, "reader_required": True,
    "reader_visibility_status": "missing_file", "historical_library_visible": True,
})
require(not stale["complete"] and stale["state"] != "reader_visible", "stale historical visibility overrode missing-file truth")


class FakeKavitaConn:
    def __init__(self, expected, association=(42, 7, 1, 1, "1")):
        self.expected = expected
        self.association = association
        self.params = None

    def execute(self, _sql, params):
        self.params = params
        return self

    def fetchone(self):
        if self.params == (self.expected, 42, 7):
            return self.association
        return (1,) if self.params == (self.expected,) else None


expected_path = "/data/manga/Gantz/Gantz v01.cbz"
reader_expectation = identity.reader_expectation(
    {
        "media_type": "manga", "work_id": "mangadex:gantz", "unit_type": "volume",
        "volume_number": "1", "kavita_series_id": 42, "kavita_library_id": 7,
    },
    filename="Gantz v01.cbz",
    series_folder="Gantz",
)
visible = frontends.kavita_file_visible_for_host_path(
    "/library/manga/Gantz/Gantz v01.cbz",
    conn=FakeKavitaConn(expected_path),
    comic_root="/library/comics",
    manga_root="/library/manga",
    kavita_comic_root="/data/comics",
    kavita_manga_root="/data/manga",
    expectation=reader_expectation,
)
require(visible.get("visible") and visible.get("matched_expectation"), visible)
wrong_library = frontends.kavita_file_visible_for_host_path(
    "/library/comics/Gantz/Gantz v01.cbz",
    conn=FakeKavitaConn("/data/comics/Gantz/Gantz v01.cbz"),
    comic_root="/library/comics",
    manga_root="/library/manga",
    kavita_comic_root="/data/comics",
    kavita_manga_root="/data/manga",
    expectation=reader_expectation,
)
require(not wrong_library.get("visible") and wrong_library.get("status") == "wrong_library", wrong_library)
wrong_unit_type = frontends.kavita_file_visible_for_host_path(
    "/library/manga/Gantz/Gantz v01.cbz",
    conn=FakeKavitaConn(expected_path, association=(42, 7, 2, 1, "1")),
    comic_root="/library/comics", manga_root="/library/manga",
    kavita_comic_root="/data/comics", kavita_manga_root="/data/manga",
    expectation=reader_expectation,
)
require(not wrong_unit_type.get("visible") and wrong_unit_type.get("status") == "wrong_unit", wrong_unit_type)
chapter_expectation = identity.reader_expectation(
    {
        "media_type": "manga", "work_id": "mangadex:gantz", "unit_type": "chapter",
        "chapter_number": "1", "kavita_series_id": 42, "kavita_library_id": 7,
    },
    filename="Gantz c001.cbz", series_folder="Gantz",
)
chapter_visible = frontends.kavita_file_visible_for_host_path(
    "/library/manga/Gantz/Gantz c001.cbz",
    conn=FakeKavitaConn("/data/manga/Gantz/Gantz c001.cbz", association=(42, 7, 2, 1, "1")),
    comic_root="/library/comics", manga_root="/library/manga",
    kavita_comic_root="/data/comics", kavita_manga_root="/data/manga",
    expectation=chapter_expectation,
)
require(chapter_visible.get("visible"), chapter_visible)
for unsupported_unit in ("collected", "pack_member"):
    unsupported_expectation = {**reader_expectation, "unit_type": unsupported_unit}
    unsupported = frontends.kavita_file_visible_for_host_path(
        "/library/manga/Gantz/Gantz v01.cbz",
        conn=FakeKavitaConn(expected_path),
        comic_root="/library/comics", manga_root="/library/manga",
        kavita_comic_root="/data/comics", kavita_manga_root="/data/manga",
        expectation=unsupported_expectation,
    )
    require(not unsupported.get("visible") and unsupported.get("status") == "unsupported_unit", unsupported)


class MappingConn:
    def __init__(self, folder="/data/manga/Gantz", library_root="/data/manga"):
        self.sql = ""
        self.folder = folder
        self.library_root = library_root

    def execute(self, sql):
        self.sql = sql
        return self

    def fetchall(self):
        if "FolderPath, LowestFolderPath" in self.sql:
            return [(42, self.folder, self.folder)]
        return [(7, self.library_root)]


unbound_expectation = completed_import.reader_expectation_for_import(
    {
        "media_type": "manga", "truth_model": "kavita_manga", "native_series_id": "mangadex:gantz",
        "source_unit": "volume", "volume_number": "1", "normalized_number": "1",
    },
    "/library/manga/Gantz/Gantz v01.cbz",
    MappingConn(),
)
require(
    unbound_expectation and not unbound_expectation["ok"]
    and unbound_expectation["candidate_reader_series_ids"] == [42]
    and unbound_expectation["binding_status"] == "manual_review_required",
    unbound_expectation,
)
failed_import_row = completed_import.media_management_import_row({}, {}, "Unknown 001.cbz", "comics")
require(
    not failed_import_row["library_classification"]["ok"]
    and "canonical_media_type" not in failed_import_row
    and failed_import_row["media_type"] == "unknown",
    failed_import_row,
)

with tempfile.TemporaryDirectory(prefix="inkdrop-library-identity-") as tmp:
    root = Path(tmp)
    db = root / "state.sqlite3"
    comic_root = root / "comics"
    manga_root = root / "manga"
    comic_root.mkdir()
    manga_root.mkdir()
    with inkdrop_state.connect(db) as con:
        inkdrop_state.init_schema(con)
        con.execute(
            "insert into series(id,title,sort_title,media_type,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?)",
            ("series:tmnt", "TMNT Journeys", "tmnt journeys", "comic", 1, 1, "{}"),
        )
        con.execute(
            "insert into import_results(id,series_id,status,created_at,raw_json) values(?,?,?,?,?)",
            ("import:1", "series:tmnt", "imported", 1, "{}"),
        )
        con.execute(
            "insert into issues(id,series_id,issue_number,normalized_number,title,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?)",
            ("issue:tmnt:1", "series:tmnt", "1", "1", "Issue 1", 1, 1, "{}"),
        )
        con.execute(
            "insert into wanted_items(id,series_id,issue_id,reason,status,priority,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?)",
            ("wanted:tmnt:1", "series:tmnt", "issue:tmnt:1", "smoke", "wanted", 50, 1, 1, "{}"),
        )
        con.execute(
            "insert into queue_items(id,wanted_id,series_id,issue_id,state,active,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?)",
            ("queue:tmnt:1", "wanted:tmnt:1", "series:tmnt", "issue:tmnt:1", "importing", 1, 1, 1, "{}"),
        )
        con.commit()
    first = inkdrop_state.persist_series_folder_identity(db, "series:tmnt", "TMNT Journeys", library_type="comics", now=2)
    drift = inkdrop_state.persist_series_folder_identity(db, "series:tmnt", "TMNT Journeys (2025)", library_type="comics", now=3)
    require(first["ok"] and not drift["ok"] and drift["series_folder"] == "TMNT Journeys", (first, drift))
    library_drift = inkdrop_state.persist_series_folder_identity(db, "series:tmnt", "TMNT Journeys", library_type="manga", now=3.5)
    require(not library_drift["ok"] and library_drift["library_type"] == "comics", library_drift)
    sequential_collision = inkdrop_state.persist_series_folder_identity(
        db, "series:other", "tmnt journeys", library_type="COMICS", now=3.6
    )
    require(not sequential_collision["ok"] and sequential_collision["reason"] == "canonical_series_folder_collision", sequential_collision)

    # Regression for the folder-identity-drift bug: a series whose earlier issues
    # already landed under one folder (imported before any canonical_library_identities
    # row existed for it -- e.g. before a comic's year was known) must not have its
    # very first lock silently adopt a differently-computed folder while those files
    # stay behind. The reader (Kavita) tracks by filesystem path, so a folder split
    # like this makes every relocated issue read as removed-then-added.
    with inkdrop_state.connect(db) as con:
        con.execute(
            "insert into series(id,title,sort_title,media_type,library_root,library_path,created_at,updated_at,raw_json) "
            "values(?,?,?,?,?,?,?,?,?)",
            (
                "series:established-drift", "Established Drift", "established drift", "comic",
                str(comic_root), str(comic_root / "Established Drift"), 1, 1, "{}",
            ),
        )
        for issue_number in (1, 2, 3):
            con.execute(
                "insert into media_files(id,path,normalized_path,media_type,series_id,status,active,first_seen_at,last_seen_at,raw_json) "
                "values(?,?,?,?,?,?,?,?,?,?)",
                (
                    f"media:established-drift:{issue_number}",
                    str(comic_root / "Established Drift" / f"Established Drift #{issue_number:03d}.cbz"),
                    str(comic_root / "Established Drift" / f"Established Drift #{issue_number:03d}.cbz"),
                    "comic", "series:established-drift", "present", 1, 1, 1, "{}",
                ),
            )
        con.commit()
    established_first = inkdrop_state.persist_series_folder_identity(
        db, "series:established-drift", "Established Drift (2025)", library_type="comics", now=3.61
    )
    require(
        established_first["ok"] and established_first["series_folder"] == "Established Drift",
        ("first lock must match where the series' files already are, not the fresh computation", established_first),
    )
    with inkdrop_state.connect_read(db) as con:
        established_row = con.execute(
            "select review_reason from canonical_library_identities where work_id=?", ("series:established-drift",)
        ).fetchone()
    require(
        established_row["review_reason"] == "locked_to_established_folder_over_fresh_computation",
        dict(established_row),
    )
    established_second = inkdrop_state.persist_series_folder_identity(
        db, "series:established-drift", "Established Drift (2025)", library_type="comics", now=3.62
    )
    require(
        not established_second["ok"] and established_second["series_folder"] == "Established Drift",
        ("subsequent imports must keep targeting the established folder, not re-drift", established_second),
    )

    # A series whose files sit in two equally populated folders gives no evidence
    # for either one. Reading media_files in whatever order SQLite hands them back
    # and taking max(count) used to break that tie by row order, and the winner was
    # written straight to series.library_path -- durable folder authority decided by
    # a coin flip. Both orders must now reach the same "needs review" answer, and
    # neither may leave a folder claim behind.
    # library_path is seeded to a folder that is deliberately not one of the
    # candidates, so a regression that quietly syncs it to a tied folder shows up
    # as a changed value rather than hiding behind an accidental match.
    split_seed_path = comic_root / "Unclaimed Split Path"

    def seed_split_series(work_id, folders, root):
        with inkdrop_state.connect(db) as con:
            con.execute(
                "insert into series(id,title,sort_title,media_type,library_root,library_path,created_at,updated_at,raw_json) "
                "values(?,?,?,?,?,?,?,?,?)",
                (work_id, "Split Candidate", "split candidate", "comic", str(root), str(split_seed_path), 1, 1, "{}"),
            )
            for index, folder in enumerate(folders):
                path = f"{Path(root).as_posix()}/{folder}/Split Candidate #{index + 1:03d}.cbz"
                con.execute(
                    "insert into media_files(id,path,normalized_path,media_type,series_id,status,active,first_seen_at,last_seen_at,raw_json) "
                    "values(?,?,?,?,?,?,?,?,?,?)",
                    (f"media:{work_id}:{index}", path, path, "comic", work_id, "present", 1, 1, 1, "{}"),
                )
            con.commit()

    def split_verdict(work_id, root):
        with inkdrop_state.connect_read(db) as con:
            return inkdrop_state.established_series_folder_from_media_files(con, work_id, str(root))

    seed_split_series("series:split-forward", ["Split Candidate", "Split Candidate (2025)"], comic_root)
    seed_split_series("series:split-reverse", ["Split Candidate (2025)", "Split Candidate"], comic_root)
    forward_verdict = split_verdict("series:split-forward", comic_root)
    reverse_verdict = split_verdict("series:split-reverse", comic_root)
    require(
        forward_verdict == reverse_verdict and forward_verdict["ambiguous"] and not forward_verdict["series_folder"],
        ("tied folders must produce one order-independent ambiguity verdict", forward_verdict, reverse_verdict),
    )
    require(
        [folder for folder, _count in forward_verdict["candidates"]] == ["Split Candidate", "Split Candidate (2025)"],
        forward_verdict,
    )
    split_claim = inkdrop_state.persist_series_folder_identity(
        db, "series:split-forward", "Split Candidate (2025)", library_type="comics", now=3.63
    )
    reverse_claim = inkdrop_state.persist_series_folder_identity(
        db, "series:split-reverse", "Split Candidate (2025)", library_type="comics", now=3.63
    )
    require(
        not split_claim["ok"] and split_claim["reason"] == "established_folder_split_requires_review"
        and split_claim["candidate_folders"] == ["Split Candidate", "Split Candidate (2025)"],
        split_claim,
    )
    require(
        reverse_claim["reason"] == split_claim["reason"]
        and reverse_claim["candidate_folders"] == split_claim["candidate_folders"],
        ("reversed insertion order must not change the verdict", split_claim, reverse_claim),
    )
    for work_id in ("series:split-forward", "series:split-reverse"):
        with inkdrop_state.connect_read(db) as con:
            split_row = con.execute(
                "select library_path, library_path_source, raw_json from series where id=?", (work_id,)
            ).fetchone()
            claimed = con.execute(
                "select series_folder from canonical_library_identities where work_id=?", (work_id,)
            ).fetchone()
        require(claimed is None, ("an ambiguous split must not claim a canonical folder", work_id, dict(claimed or {})))
        require(
            split_row["library_path"] == str(split_seed_path) and not split_row["library_path_source"],
            ("library_path must survive an ambiguous split untouched", work_id, dict(split_row)),
        )
        split_envelope = json.loads(split_row["raw_json"]).get("canonical_library_identity_v1") or {}
        require(
            split_envelope.get("split_folder_review_required") is True
            and "series_folder" not in split_envelope
            and [entry["series_folder"] for entry in split_envelope["split_folder_candidates"]]
            == ["Split Candidate", "Split Candidate (2025)"],
            (work_id, split_envelope),
        )
        require(
            not inkdrop_state.persisted_series_folder_identity(db, work_id),
            ("no folder may be readable as established after an ambiguous split", work_id),
        )

    # Candidates are canonicalized before they are counted. Two spellings of one
    # folder used to count as two folders, which turned a clear majority into a
    # phantom tie; a '..' hop used to invent a third folder; and a path that walks
    # back out of the library root used to pass containment on a plain startswith().
    seed_split_series(
        "series:split-canonical",
        ["Canon Folder", "Canon Folder/", "canon folder", "Other/../Canon Folder", "Runner Up", "../outside-root"],
        comic_root,
    )
    canonical_verdict = split_verdict("series:split-canonical", comic_root)
    require(
        canonical_verdict["series_folder"] == "Canon Folder" and not canonical_verdict["ambiguous"],
        ("spelling variants of one folder must not defeat a real majority", canonical_verdict),
    )
    require(
        canonical_verdict["candidates"] == [("Canon Folder", 4), ("Runner Up", 1)],
        ("paths outside the library root must be excluded, not counted", canonical_verdict),
    )
    seed_split_series("series:split-nested", ["Nested/Volume 01", "Nested/Volume 01"], comic_root)
    require(split_verdict("series:split-nested", comic_root)["series_folder"] == "Nested/Volume 01", "nested folder flattened")

    # A folder reached through a symlink and the folder it points at are one place
    # on disk, so they must not read as a two-way split. Windows needs elevation to
    # create a directory symlink, so this control only runs where one can be made.
    symlink_target = comic_root / "Symlink Target"
    symlink_target.mkdir()
    symlink_alias = comic_root / "Symlink Alias"
    try:
        symlink_alias.symlink_to(symlink_target, target_is_directory=True)
        symlink_supported = symlink_alias.is_dir()
    except (OSError, NotImplementedError):
        symlink_supported = False
    if symlink_supported:
        for folder in ("Symlink Target", "Symlink Alias"):
            os.makedirs(str(comic_root / folder), exist_ok=True)
        seed_split_series("series:split-symlink", ["Symlink Target", "Symlink Alias"], comic_root)
        symlink_verdict = split_verdict("series:split-symlink", comic_root)
        require(
            symlink_verdict["series_folder"] == "Symlink Target" and not symlink_verdict["ambiguous"],
            ("a symlink and its target are one folder, not a split", symlink_verdict),
        )
        seed_split_series("series:split-symlink-reverse", ["Symlink Alias", "Symlink Target"], comic_root)
        require(
            split_verdict("series:split-symlink-reverse", comic_root) == symlink_verdict,
            ("symlink collapsing must not depend on row order", symlink_verdict),
        )

    with inkdrop_state.connect(db) as con:
        con.execute(
            "insert into series(id,title,sort_title,media_type,library_root,library_path,created_at,updated_at,raw_json) "
            "values(?,?,?,?,?,?,?,?,?)",
            (
                "series:hunterx", "Hunter X Hunter", "hunter x hunter", "manga",
                str(manga_root), str(manga_root / "Hunter X Hunter"), 1, 1, "{}",
            ),
        )
        con.commit()
    with inkdrop_state.connect(db) as con:
        stale_path_before = con.execute("select library_path from series where id=?", ("series:hunterx",)).fetchone()["library_path"]
    require(stale_path_before == str(manga_root / "Hunter X Hunter"), stale_path_before)
    canonical_claim = inkdrop_state.persist_series_folder_identity(
        db, "series:hunterx", "Hunter X Hunter (2005)", library_type="manga", now=3.65
    )
    require(canonical_claim["ok"] and canonical_claim["series_folder"] == "Hunter X Hunter (2005)", canonical_claim)
    with inkdrop_state.connect(db) as con:
        synced_row = con.execute(
            "select library_path, library_path_source from series where id=?", ("series:hunterx",)
        ).fetchone()
    require(
        synced_row["library_path"] == f"{manga_root}/Hunter X Hunter (2005)"
        and synced_row["library_path_source"] == "canonical_identity_sync",
        dict(synced_row),
    )
    reconfirm = inkdrop_state.persist_series_folder_identity(
        db, "series:hunterx", "Hunter X Hunter (2005)", library_type="manga", now=3.66
    )
    require(reconfirm["ok"], reconfirm)
    with inkdrop_state.connect(db) as con:
        unchanged_row = con.execute(
            "select library_path, updated_at from series where id=?", ("series:hunterx",)
        ).fetchone()
    require(unchanged_row["library_path"] == f"{manga_root}/Hunter X Hunter (2005)", dict(unchanged_row))
    with inkdrop_state.connect(db) as con:
        con.execute(
            "insert into series(id,title,sort_title,media_type,library_root,library_path,library_path_source,created_at,updated_at,raw_json) "
            "values(?,?,?,?,?,?,?,?,?,?)",
            (
                "series:userpinned", "User Pinned Series", "user pinned series", "manga",
                str(manga_root), str(manga_root / "Custom Folder Name"), "user", 1, 1, "{}",
            ),
        )
        con.commit()
    user_claim = inkdrop_state.persist_series_folder_identity(
        db, "series:userpinned", "User Pinned Series (2020)", library_type="manga", now=3.67
    )
    require(user_claim["ok"], user_claim)
    with inkdrop_state.connect(db) as con:
        user_row = con.execute(
            "select library_path, library_path_source from series where id=?", ("series:userpinned",)
        ).fetchone()
    require(
        user_row["library_path"] == str(manga_root / "Custom Folder Name") and user_row["library_path_source"] == "user",
        dict(user_row),
    )
    concurrent = []
    barrier = threading.Barrier(2)

    def claim_concurrent(work_id):
        barrier.wait()
        concurrent.append(inkdrop_state.persist_series_folder_identity(
            db, work_id, "Shared Concurrent Folder", library_type="manga", now=3.7
        ))

    threads = [threading.Thread(target=claim_concurrent, args=(f"series:concurrent:{index}",)) for index in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    require(sum(1 for item in concurrent if item["ok"]) == 1, concurrent)
    require(sum(1 for item in concurrent if item.get("reason") == "canonical_series_folder_collision") == 1, concurrent)
    completed_import.INKDROP_STATE_DB = db
    binding = completed_import.approve_reader_binding("series:tmnt", 42, 7, now=3.8)
    require(
        binding["ok"] and binding["source"] == "operator_approved_cli"
        and binding["producer"] == "completed_import_operator_cli",
        binding,
    )
    bound_expectation = completed_import.reader_expectation_for_import(
        {
            "media_type": "comic", "native_series_id": "series:tmnt", "source_unit": "issue",
            "issue_number": "1", "normalized_number": "1",
        },
        str(completed_import.COMIC_ROOT / "TMNT Journeys" / "TMNT Journeys #001 (2025).cbz"),
        MappingConn("/data/comics/TMNT Journeys", "/data/comics"),
    )
    require(bound_expectation["ok"] and bound_expectation["reader_series_id"] == "42", bound_expectation)
    producer_verified = frontends.kavita_file_visible_for_host_path(
        str(completed_import.COMIC_ROOT / "TMNT Journeys" / "TMNT Journeys #001 (2025).cbz"),
        conn=FakeKavitaConn("/data/comics/TMNT Journeys/TMNT Journeys #001 (2025).cbz"),
        comic_root=completed_import.COMIC_ROOT, manga_root=completed_import.MANGA_ROOT,
        kavita_comic_root="/data/comics", kavita_manga_root="/data/manga", expectation=bound_expectation,
    )
    require(producer_verified.get("visible") and producer_verified.get("matched_expectation"), producer_verified)
    wrong_work = completed_import.reader_expectation_for_import(
        {
            "media_type": "comic", "native_series_id": "series:wrong-work", "source_unit": "issue",
            "issue_number": "1", "normalized_number": "1",
            "reader_binding_authoritative": True, "reader_binding_source": "operator_approved_cli",
            "kavita_series_id": 42, "kavita_library_id": 7,
        },
        str(completed_import.COMIC_ROOT / "TMNT Journeys" / "TMNT Journeys #001 (2025).cbz"),
        MappingConn("/data/comics/TMNT Journeys", "/data/comics"),
    )
    require(not wrong_work["ok"] and wrong_work["candidate_reader_series_ids"] == [42], wrong_work)
    wrong_work_visibility = frontends.kavita_file_visible_for_host_path(
        str(completed_import.COMIC_ROOT / "TMNT Journeys" / "TMNT Journeys #001 (2025).cbz"),
        conn=FakeKavitaConn("/data/comics/TMNT Journeys/TMNT Journeys #001 (2025).cbz"),
        comic_root=completed_import.COMIC_ROOT, manga_root=completed_import.MANGA_ROOT,
        kavita_comic_root="/data/comics", kavita_manga_root="/data/manga", expectation=wrong_work,
    )
    require(not wrong_work_visibility.get("visible") and wrong_work_visibility["status"] == "invalid_expectation", wrong_work_visibility)
    for work_id, folder in (
        ("series:reader-sequential:1", "Reader Sequential One"),
        ("series:reader-sequential:2", "Reader Sequential Two"),
        ("series:reader-concurrent:1", "Reader Concurrent One"),
        ("series:reader-concurrent:2", "Reader Concurrent Two"),
    ):
        claimed = inkdrop_state.persist_series_folder_identity(db, work_id, folder, library_type="comics", now=3.9)
        require(claimed["ok"], claimed)
    untrusted_binding = inkdrop_state.persist_reader_binding(
        db, "series:reader-sequential:2", "Series-99", "Library-9",
        source="exact_work_metadata", now=3.95,
    )
    require(not untrusted_binding["ok"] and untrusted_binding["reason"] == "reader_binding_not_authoritative", untrusted_binding)
    sequential_reader_first = completed_import.approve_reader_binding("series:reader-sequential:1", "Series-99", "Library-9", now=4.0)
    sequential_reader_second = completed_import.approve_reader_binding("series:reader-sequential:2", " series-99 ", " library-9 ", now=4.1)
    require(sequential_reader_first["ok"], sequential_reader_first)
    require(
        not sequential_reader_second["ok"] and sequential_reader_second["reason"] == "reader_binding_collision",
        sequential_reader_second,
    )
    concurrent_reader = []
    reader_barrier = threading.Barrier(2)

    def bind_concurrent_reader(work_id):
        reader_barrier.wait()
        concurrent_reader.append(completed_import.approve_reader_binding(work_id, "Series-100", "Library-9", now=4.2))

    reader_threads = [
        threading.Thread(target=bind_concurrent_reader, args=(f"series:reader-concurrent:{index}",))
        for index in (1, 2)
    ]
    for thread in reader_threads:
        thread.start()
    for thread in reader_threads:
        thread.join()
    require(sum(1 for item in concurrent_reader if item["ok"]) == 1, concurrent_reader)
    require(sum(1 for item in concurrent_reader if item.get("reason") == "reader_binding_collision") == 1, concurrent_reader)
    preview = inkdrop_state.media_management_destination_preview(
        db,
        {
            "native_series_id": "series:tmnt", "media_type": "comic", "series": "TMNT Journeys",
            "series_year": "2025", "unit_type": "issue", "issue_number": "1",
        },
        source_path=str(root / "source.cbz"),
        settings={"comic_root": str(comic_root), "manga_root": str(manga_root), "minimum_free_space_gb": 0},
        check_storage=False,
    )
    require(preview["series_folder"] == "TMNT Journeys" and preview["filename"] == "TMNT Journeys #001 (2025).cbz", preview)
    volume_preview = inkdrop_state.media_management_destination_preview(
        db,
        {
            "work_id": "series:fairy-tail", "media_type": "manga", "series": "Fairy Tail", "unit_type": "volume",
            "issue_number": "52", "chapter_number": "52", "volume_number": "52",
        },
        source_path=str(root / "Fairy Tail 52.cbz"),
        settings={"comic_root": str(comic_root), "manga_root": str(manga_root), "minimum_free_space_gb": 0},
        check_storage=False,
    )
    require(volume_preview["root"] == str(manga_root).replace("\\", "/"), volume_preview)
    require(volume_preview["filename"] == "Fairy Tail v52.cbz" and "c052" not in volume_preview["filename"], volume_preview)
    require(inkdrop_state.record_import_reconciliation_stage(
        db, "import:1", "reader_visibility_projection", status="failed",
        error_code="expected series not found", error_category="reader", now=4,
    ), "stage error not recorded")
    with inkdrop_state.connect_read(db) as con:
        raw = json.loads(con.execute("select raw_json from import_results where id='import:1'").fetchone()["raw_json"])
        history = con.execute("select raw_json from history_events where entity_id='import:1'").fetchone()
    require(raw["last_import_reconciliation_error"]["error_code"] == "expected_series_not_found", raw)
    require(history and "expected_series_not_found" in history["raw_json"], history)
    imported_file = comic_root / "TMNT Journeys" / "TMNT Journeys #001 (2025).cbz"
    imported_file.parent.mkdir(parents=True)
    imported_file.write_bytes(b"verified-smoke")
    recorded = inkdrop_state.record_direct_import_result(
        db,
        "queue:tmnt:1",
        source_path=str(imported_file),
        dest_path=str(imported_file),
        source="smoke",
        status="folder_verified",
        verified=True,
        raw={"media_management_preview": preview},
        created_at=5,
    )
    require(recorded.get("ok"), recorded)
    with inkdrop_state.connect_read(db) as con:
        direct_raw = json.loads(con.execute(
            "select raw_json from import_results where queue_id='queue:tmnt:1' order by created_at desc limit 1"
        ).fetchone()["raw_json"])
    executed = {item["stage"] for item in direct_raw.get("import_reconciliation_stages_v1", [])}
    require("file_placement" in executed and "final_completion_projection" in executed, direct_raw)
    with inkdrop_state.connect(db) as con:
        con.execute("drop index idx_canonical_reader_binding_unique")
        for index in (1, 2):
            con.execute(
                """
                insert into canonical_library_identities(
                    work_id,library_type,series_folder,normalized_library_type,normalized_series_folder,
                    reader_series_id,reader_library_id,normalized_reader_series_id,normalized_reader_library_id,
                    reader_binding_source,created_at,updated_at
                ) values(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"series:legacy-reader-duplicate:{index}", "comics", f"Legacy Reader Duplicate {index}",
                    "comics", f"legacy reader duplicate {index}",
                    "Legacy-Series" if index == 1 else " legacy-series ",
                    "Legacy-Library" if index == 1 else "LEGACY-LIBRARY",
                    "legacy-series", "legacy-library", "operator_approved_cli", 6, 6,
                ),
            )
        con.commit()
    require(
        not inkdrop_state.reader_binding_for_work(db, "series:legacy-reader-duplicate:1"),
        "legacy duplicate reader binding was consumed before reconciliation",
    )
    legacy_reader_duplicate = completed_import.approve_reader_binding("series:tmnt", 42, 7, now=6.5)
    require(
        not legacy_reader_duplicate["ok"]
        and legacy_reader_duplicate["reason"] == "existing_reader_binding_collision",
        legacy_reader_duplicate,
    )
    with inkdrop_state.connect(db) as con:
        con.execute("drop index idx_canonical_library_folder_unique")
        for work_id in ("series:legacy-duplicate:1", "series:legacy-duplicate:2"):
            con.execute(
                """
                insert into canonical_library_identities(
                    work_id,library_type,series_folder,normalized_library_type,normalized_series_folder,
                    created_at,updated_at
                ) values(?,?,?,?,?,?,?)
                """,
                (work_id, "comics", "Legacy Duplicate", "comics", "legacy duplicate", 6, 6),
            )
        con.commit()
    legacy_duplicate = inkdrop_state.persist_series_folder_identity(
        db, "series:after-legacy-duplicate", "Otherwise Unique", library_type="comics", now=7
    )
    require(
        not legacy_duplicate["ok"]
        and legacy_duplicate["reason"] == "existing_canonical_series_folder_collision",
        legacy_duplicate,
    )

print("INKDROP_LIBRARY_IDENTITY_READER_TRUTH_OK")
