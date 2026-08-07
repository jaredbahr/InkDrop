#!/usr/bin/env python3
import hashlib
import contextlib
import io
import json
import shutil
import sqlite3
import tempfile
import time
import zipfile
import zlib
from pathlib import Path

from core import inkdrop_artifact_acceptance as acceptance
from core import inkdrop_candidate_matching as matching
from core import inkdrop_completed_import as importer
from core import inkdrop_incident_recovery as recovery
from core import inkdrop_pack_import as pack_import
from core import inkdrop_state

INCIDENT_SHA = "ce15d2dabbeb3e6e3a1f95a400eec17c84b654d56d22c9df5247eb6f4fd85c34"


def require(value, message):
    if not value:
        raise AssertionError(message)


def png(seed=b"x"):
    def chunk(name, data):
        return len(data).to_bytes(4, "big") + name + data + zlib.crc32(name + data).to_bytes(4, "big")
    raw = b"".join(b"\0" + seed[:1] * 3 * 16 for _ in range(16))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", b"\0\0\0\x10\0\0\0\x10\x08\x02\0\0\0") + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def archive(path, member_prefix, seed=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as out:
        for page in range(20):
            out.writestr(f"{member_prefix}_{page:03d}.png", png(seed))
    return path


def repack_with_generated_comicinfo(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source) as old, zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as new:
        for page, info in enumerate(old.infolist()):
            new.writestr(f"renamed/Dispatch_Page_{page:03d}.png", old.read(info.filename))
        new.writestr("ComicInfo.xml", "<ComicInfo><Series>Dispatch</Series><Number>1</Number></ComicInfo>")
    return destination


def seed_state(db, media_root, dest, *, pre_retracted=False, duplicate_proof=False):
    now = time.time()
    with inkdrop_state.connect(db) as con:
        inkdrop_state.init_schema(con)
        con.execute("insert into app_settings(key,scope,label,value_json,description,source,updated_at) values(?,?,?,?,?,?,?)", ("media_management.comic_root","media_management","Comic root",json.dumps(str(media_root)),"fixture","test",now))
        con.execute("insert into series(id,title,media_type,metadata_provider,metadata_id,raw_json) values(?,?,?,?,?,?)", ("comicvine:169497","Dispatch","comic","comicvine","169497","{}"))
        con.execute("insert into issues(id,series_id,issue_number,normalized_number,title,raw_json) values(?,?,?,?,?,?)", ("dispatch:1","comicvine:169497","1","1","Splash","{}"))
        con.execute("insert into wanted_items(id,series_id,issue_id,status,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?)", ("wanted:dispatch:1","comicvine:169497","dispatch:1","satisfied",now,now,"{}"))
        con.execute("insert into queue_items(id,wanted_id,series_id,issue_id,state,active,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?)", ("queue:dispatch:1","wanted:dispatch:1","comicvine:169497","dispatch:1","verified",0,now,now,"{}"))
        import_truth = ("stale_folder_proof_retracted", 0, "failed", "retry_later", "missing_file", 0) if pre_retracted else ("folder_verified", 1, "complete", "verified", "folder", 1)
        con.execute("insert into import_results(id,queue_id,series_id,issue_id,source_path,dest_path,status,verified,outcome,display_phase,completion_truth,folder_imported,imported_count,skipped_count,created_at,raw_json) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("import:dispatch:1","queue:dispatch:1","comicvine:169497","dispatch:1","staged.cbz",str(dest),*import_truth,1,0,now,"{}"))
        if duplicate_proof:
            con.execute("insert into import_results(id,queue_id,series_id,issue_id,source_path,dest_path,status,verified,outcome,display_phase,completion_truth,folder_imported,imported_count,skipped_count,created_at,raw_json) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("import:dispatch:duplicate","queue:dispatch:1","comicvine:169497","dispatch:1","staged.cbz",str(dest),"folder_verified",1,"complete","verified","folder",1,1,0,now + 1,"{}"))
        con.execute("insert into download_tasks(id,queue_id,wanted_id,series_id,issue_id,source,title,status,state,local_path,started_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?,?,?,?,?)", ("task:dispatch:1","queue:dispatch:1","wanted:dispatch:1","comicvine:169497","dispatch:1","direct",str(dest),"verified","verified",str(dest),now,now,"{}"))
        con.execute("insert into series(id,title,media_type,raw_json) values(?,?,?,?)", ("comicvine:other","Other","comic","{}"))
        con.execute("insert into issues(id,series_id,issue_number,normalized_number,title,raw_json) values(?,?,?,?,?,?)", ("other:1","comicvine:other","1","1","One","{}"))
        con.execute("insert into import_results(id,series_id,issue_id,dest_path,status,verified,completion_truth,folder_imported,created_at,raw_json) values(?,?,?,?,?,?,?,?,?,?)", ("import:other","comicvine:other","other:1",str(media_root / "Other #1.cbz"),"folder_verified",1,"folder",1,now,"{}"))
        con.execute("insert into media_files(id,path,normalized_path,media_type,series_id,issue_id,queue_id,import_result_id,status,active,completion_truth,folder_imported,size_bytes,mtime,first_seen_at,last_seen_at,raw_json) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("media:dispatch",str(dest),inkdrop_state.media_file_normalized_path(dest),"comic","comicvine:169497","dispatch:1","queue:dispatch:1","import:dispatch:1","present",1,"folder",1,123,now,now,now,"{}"))
        con.execute("drop index idx_media_files_normalized_path")
        con.execute("insert into media_files(id,path,normalized_path,media_type,series_id,issue_id,queue_id,import_result_id,status,active,completion_truth,folder_imported,first_seen_at,last_seen_at,raw_json) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("media:unrelated-owner",str(dest),inkdrop_state.media_file_normalized_path(dest),"comic","comicvine:other","other:1",None,"import:other","present",1,"folder",1,now,now,"{}"))
        con.execute("insert into media_files(id,path,normalized_path,media_type,series_id,issue_id,queue_id,import_result_id,status,active,completion_truth,folder_imported,first_seen_at,last_seen_at,raw_json) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("media:legacy-dispatch",str(dest),inkdrop_state.media_file_normalized_path(dest),"comic","comicvine:169497","dispatch:1","queue:dispatch:1",None,"present",1,"folder",1,now,now,"{}"))
        con.execute("insert into media_files(id,path,normalized_path,media_type,series_id,issue_id,queue_id,import_result_id,status,active,completion_truth,folder_imported,first_seen_at,last_seen_at,raw_json) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("media:legacy-wrong-queue",str(dest),inkdrop_state.media_file_normalized_path(dest),"comic","comicvine:169497","dispatch:1","queue:other",None,"present",1,"folder",1,now,now,"{}"))
        con.commit()


def main():
    with tempfile.TemporaryDirectory(prefix="dispatch-recovery-closure-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp); completion = root / "imported-files.sqlite3"
        bad = archive(root / "Dispatch!! v01 c03.cbz", "Dispatch_v01_c03")
        renamed = root / "renamed" / "Dispatch v01.cbz"; renamed.parent.mkdir(); shutil.copy2(bad, renamed)
        repacked = repack_with_generated_comicinfo(bad, root / "repacked" / "Dispatch #001.cbz")
        safe = archive(root / "safe" / "Dispatch v01.cbz", "Dispatch_001", seed=b"y")
        same_names_safe = archive(root / "same-names-safe" / "Dispatch #001.cbz", "Dispatch_v01_c03", seed=b"z")
        target = {"title":"Dispatch","series":"Dispatch","media_type":"comic","unit_type":"issue","issue_number":"1","normalized_number":"1"}
        check = lambda p: {"ok":True,"page_count":20,"payload_size":p.stat().st_size}
        for path in (bad, renamed):
            decision = acceptance.decide_acceptance(path, target=target, archive_check=check(path))
            require(
                decision["decision"] in {"rejected_wrong_unit_type", "rejected_source_identity"},
                decision,
            )
            require(
                set(decision.get("reason_codes") or [])
                & {"wrong_unit_type_chapter_for_comic_issue", "issue_target_cannot_use_chapter"},
                decision,
            )
            require(not (root / "library" / path.name).exists(), "rejected artifact created destination")
        require(not importer.classify_import_filename_safety(bad, target, trusted_issue="1")["ok"], "manual/common filename gate admitted incident")
        automatic = matching.candidate_compatibility({"title":"Dispatch!! v01 c03","filename":bad.name}, {"series_title":"Dispatch","media_type":"comic","unit_type":"issue","issue_number":"1"})
        require(automatic["status"] == "blocked", automatic)
        for name in ("Dispatch #001.cbz", "Dispatch 001.cbz", "Dispatch Issue 1.cbz", "Dispatch #001.cbr"):
            require(importer.classify_import_filename_safety(Path(name), target, trusted_issue="1", comicinfo={})["ok"], name)
        require(matching.candidate_compatibility({"title":"Dispatch #001"}, {"series_title":"Dispatch","media_type":"comic","unit_type":"issue","issue_number":"1"})["status"] == "compatible", "authoritative Dispatch issue rejected")

        old_db = importer.DB_PATH; importer.DB_PATH = completion
        conn = importer.connect()
        try:
            bad_digest = importer.sha256(bad)
            importer.record_artifact_bad_content_memory(conn, bad_digest, bad, acceptance.decide_acceptance(bad, target=target, archive_check=check(bad)))
            importer.record_known_bad_content_sha(conn, INCIDENT_SHA)
            require(importer.find_artifact_bad_content_memory(conn, renamed, file_sha256=bad_digest), "renamed bytes bypassed memory")
            require(importer.sha256(repacked) != bad_digest, "repack fixture did not change raw SHA")
            bad_manifest = acceptance.page_manifest(bad)
            repacked_manifest = acceptance.page_manifest(repacked)
            same_names_manifest = acceptance.page_manifest(same_names_safe)
            require(repacked_manifest["ordered_page_manifest_hash"] == bad_manifest["ordered_page_manifest_hash"], "repack fixture changed page content")
            require(repacked_manifest["archive_member_manifest_hash"] != bad_manifest["archive_member_manifest_hash"], "repack fixture did not rename members")
            require(importer.find_artifact_bad_content_memory(conn, repacked), "same pages with generated ComicInfo bypassed manifest memory")
            memory_row = conn.execute("select content_manifest_hash,archive_member_manifest_hash,raw_json from artifact_bad_content_memory where file_sha256=?", (bad_digest,)).fetchone()
            require(memory_row[0] and memory_row[1], "stable manifests were not persisted internally")
            require("manifest_hash" not in memory_row[2], "protected manifest leaked into sanitized raw decision")
            require(importer.find_artifact_bad_content_memory(conn, safe) is None, "different safe content with same basename was blocked")
            require(same_names_manifest["archive_member_manifest_hash"] == bad_manifest["archive_member_manifest_hash"], "same-name fixture did not preserve member names")
            require(same_names_manifest["ordered_page_manifest_hash"] != bad_manifest["ordered_page_manifest_hash"], "same-name fixture did not change page content")
            require(importer.find_artifact_bad_content_memory(conn, same_names_safe) is None, "filename-only member manifest falsely blocked safe content")
            require(importer.find_artifact_bad_content_memory(conn, safe, file_sha256=INCIDENT_SHA), "exact incident SHA was not recognized")
        finally:
            conn.close(); importer.DB_PATH = old_db

        old_db = importer.DB_PATH
        old_load_targets = importer.load_comic_targets
        old_state_dir, old_log_path = importer.STATE_DIR, importer.LOG_PATH
        importer.DB_PATH = completion
        importer.STATE_DIR, importer.LOG_PATH = root, root / "import.log"
        target.update({"id":"169497","native_series_id":"comicvine:169497","folder":str(root / "library" / "Dispatch")})
        importer.load_comic_targets = lambda *_args, **_kwargs: [target]
        try:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                importer.import_files(
                    "comics", dry_run=True, min_age_seconds=0, ignore_cutoff=True,
                    all_series=True, source_files=[repacked], trusted_series_id="comicvine:169497",
                    trusted_issue="1", wait_for_library_scan=False,
                )
            require("known_bad_artifact_content" in output.getvalue(), "ordinary/manual-source ingress did not consult content memory")
            require(not Path(target["folder"]).exists(), "known-bad ingress created a destination")
        finally:
            importer.DB_PATH = old_db
            importer.STATE_DIR, importer.LOG_PATH = old_state_dir, old_log_path
            importer.load_comic_targets = old_load_targets

        old_pack_log = pack_import.PACK_LOG
        pack_import.PACK_LOG = root / "pack.log"
        old_db = importer.DB_PATH; importer.DB_PATH = completion
        try:
            pack_result = pack_import.import_matched_files(
                importer,
                [(repacked, target, {"source_unit":"volume","issue_number":"1","missing_issue":{}})],
                True, 1, "dispatch-manifest-memory", wait_for_library_scan=False,
            )
            require(pack_result["bad_archives"][0]["skip_reason"] == "known_bad_artifact_content", "pack ingress bypassed manifest memory")
        finally:
            importer.DB_PATH = old_db
            pack_import.PACK_LOG = old_pack_log

        media_root = root / "library"; media_root.mkdir(exist_ok=True); missing_dest = media_root / "Dispatch #001.cbz"
        state_db = root / "state.sqlite3"; seed_state(state_db, media_root, missing_dest)
        retracted_db = root / "pre-retracted-state.sqlite3"
        seed_state(retracted_db, media_root, missing_dest, pre_retracted=True)
        pre_dry = recovery.recover_exact_artifact(
            state_db=retracted_db, completion_db=root / "pre-retracted-completion.sqlite3",
            series_id="comicvine:169497", issue_number="1", expected_sha256="1" * 64,
        )
        require(pre_dry["reconciliation"]["import_results"] == 0, pre_dry)
        require(pre_dry["reconciliation"]["media_files"] == 2, pre_dry)
        with sqlite3.connect(retracted_db) as con:
            before = con.execute("select status,active,completion_truth,folder_imported,size_bytes,mtime from media_files where id='media:dispatch'").fetchone()
            require(before[:5] == ("present",1,"folder",1,123) and before[5] is not None, "dry-run mutated pre-retracted media")
        pre_applied = recovery.recover_exact_artifact(
            state_db=retracted_db, completion_db=root / "pre-retracted-completion.sqlite3",
            series_id="comicvine:169497", issue_number="1", expected_sha256="1" * 64,
            apply=True,
        )
        require(pre_applied["reconciliation"] == {"import_results":0,"queue_items":0,"download_tasks":0,"media_files":2}, pre_applied)
        with sqlite3.connect(retracted_db) as con:
            require(con.execute("select status,active,completion_truth,folder_imported,size_bytes,mtime from media_files where id='media:dispatch'").fetchone() == ("missing",0,None,0,None,None), "pre-retracted direct media was not fully repaired")
            require(con.execute("select status,active from media_files where id='media:legacy-dispatch'").fetchone() == ("missing",0), "pre-retracted legacy media was not repaired")
            require(con.execute("select status,active from media_files where id='media:unrelated-owner'").fetchone() == ("present",1), "pre-retracted repair crossed ownership")
            require(con.execute("select status,active from media_files where id='media:legacy-wrong-queue'").fetchone() == ("present",1), "pre-retracted repair crossed legacy queue ownership")
        pre_replay = recovery.recover_exact_artifact(
            state_db=retracted_db, completion_db=root / "pre-retracted-completion.sqlite3",
            series_id="comicvine:169497", issue_number="1", expected_sha256="1" * 64,
            apply=True,
        )
        require(pre_replay["reconciliation"]["media_files"] == 0, pre_replay)
        duplicate_db = root / "duplicate-proof-state.sqlite3"
        seed_state(duplicate_db, media_root, missing_dest, duplicate_proof=True)
        duplicate_dry = recovery.recover_exact_artifact(
            state_db=duplicate_db, completion_db=root / "duplicate-proof-completion.sqlite3",
            series_id="comicvine:169497", issue_number="1", expected_sha256="2" * 64,
        )
        expected_duplicate = {"import_results":2,"queue_items":1,"download_tasks":1,"media_files":2}
        require(duplicate_dry["reconciliation"] == expected_duplicate, duplicate_dry)
        duplicate_applied = recovery.recover_exact_artifact(
            state_db=duplicate_db, completion_db=root / "duplicate-proof-completion.sqlite3",
            series_id="comicvine:169497", issue_number="1", expected_sha256="2" * 64,
            apply=True,
        )
        require(duplicate_applied["reconciliation"] == expected_duplicate, duplicate_applied)
        old_db = importer.DB_PATH; importer.DB_PATH = completion
        try:
            with inkdrop_state.connect(state_db) as con:
                direct_gate = inkdrop_state.direct_import_destination_unit_gate(
                    con,
                    {"series_id":"comicvine:169497","issue_id":"dispatch:1"},
                    str(repacked),
                    {},
                )
            require(direct_gate and direct_gate["reason"] == "known_bad_artifact_content", "direct replay bypassed manifest memory")
        finally:
            importer.DB_PATH = old_db
        staged_root = root / "staging"; staged_root.mkdir(); staged = staged_root / "candidate.cbz"; shutil.copy2(bad, staged)
        digest = hashlib.sha256(staged.read_bytes()).hexdigest(); quarantine = root / "quarantine"
        dry = recovery.recover_exact_artifact(state_db=state_db,completion_db=completion,series_id="comicvine:169497",issue_number="1",expected_sha256=digest,staged_path=staged,allowed_roots=[staged_root],quarantine_root=quarantine)
        require(dry["dry_run"] and staged.exists(), dry)
        try:
            recovery.recover_exact_artifact(state_db=state_db,completion_db=completion,series_id="comicvine:169497",issue_number="1",expected_sha256="0"*64,staged_path=staged,allowed_roots=[staged_root],quarantine_root=quarantine)
            raise AssertionError("hash mismatch accepted")
        except ValueError as exc:
            require("hash" in str(exc), exc)
        try:
            recovery.recover_exact_artifact(state_db=state_db,completion_db=completion,series_id="comicvine:169497",issue_number="1",expected_sha256=digest,staged_path=staged,allowed_roots=[root / "elsewhere"],quarantine_root=quarantine)
            raise AssertionError("path outside root accepted")
        except ValueError as exc:
            require("root" in str(exc), exc)
        operation_id = recovery._operation_id("comicvine:169497", "1", digest)
        preexisting_quarantine = quarantine / operation_id / staged.name
        preexisting_quarantine.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(staged, preexisting_quarantine)
        applied = recovery.recover_exact_artifact(state_db=state_db,completion_db=completion,series_id="comicvine:169497",issue_number="1",expected_sha256=digest,staged_path=staged,allowed_roots=[staged_root],quarantine_root=quarantine,apply=True)
        require(applied["quarantined"] and not staged.exists(), applied)
        require(len(list((quarantine / operation_id).glob("candidate*.cbz"))) == 2, "equal-hash quarantine destination left staged source in place")
        expected_ordinary = {"import_results":1,"queue_items":1,"download_tasks":1,"media_files":2}
        require(dry["reconciliation"] == expected_ordinary, dry)
        require(applied["reconciliation"] == expected_ordinary, applied)
        journal = quarantine / ".operations" / f"{applied['operation_id']}.json"
        journal_before = journal.read_bytes()
        with sqlite3.connect(completion) as con:
            seen_before = con.execute("select seen_count from artifact_bad_content_memory where file_sha256=?", (digest,)).fetchone()[0]
        replay = recovery.recover_exact_artifact(state_db=state_db,completion_db=completion,series_id="comicvine:169497",issue_number="1",expected_sha256=digest,staged_path=staged,allowed_roots=[staged_root],quarantine_root=quarantine,apply=True)
        require(replay["reconciliation"]["import_results"] == 0, replay)
        require(journal.read_bytes() == journal_before, "idempotent replay rewrote operation journal")
        with sqlite3.connect(completion) as con:
            require(con.execute("select seen_count from artifact_bad_content_memory where file_sha256=?", (digest,)).fetchone()[0] == seen_before, "idempotent replay rewrote bad-content memory")
        with sqlite3.connect(state_db) as con:
            require(con.execute("select status from wanted_items where id='wanted:dispatch:1'").fetchone()[0] == "wanted", "Wanted not reopened")
            require(con.execute("select status,verified from import_results where id='import:dispatch:1'").fetchone() == ("stale_folder_proof_retracted",0), "import proof not retracted")
            require(con.execute("select status,active from media_files where id='media:dispatch'").fetchone() == ("missing",0), "media truth remained active")
            require(con.execute("select status,active from media_files where id='media:legacy-dispatch'").fetchone() == ("missing",0), "owned legacy media truth remained active")
            require(con.execute("select status,active from media_files where id='media:unrelated-owner'").fetchone() == ("present",1), "unrelated media owner changed by path fallback")
            require(con.execute("select status,active from media_files where id='media:legacy-wrong-queue'").fetchone() == ("present",1), "legacy path fallback crossed queue ownership")
            require(con.execute("select status,verified from import_results where id='import:other'").fetchone() == ("folder_verified",1), "unrelated row changed")
            require(con.execute("select count(*) from history_events where event_type='exact_artifact_incident_recovery'").fetchone()[0] == 1, "audit history not idempotent")
            audit_raw = con.execute("select raw_json from history_events where event_type='exact_artifact_incident_recovery'").fetchone()[0]
            require(digest not in audit_raw and INCIDENT_SHA not in audit_raw, "full content SHA leaked into incident history")
            audit = json.loads(audit_raw)
            require(audit.get("content_identity_kind") == "sha256" and len(audit.get("content_fingerprint") or "") == 12, "redacted audit identity missing")
    print("dispatch recovery closure smoke: PASS")


if __name__ == "__main__":
    main()
