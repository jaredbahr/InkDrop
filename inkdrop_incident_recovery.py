#!/usr/bin/env python3
"""Exact-target, hash-bound recovery for a known bad imported artifact."""

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
from pathlib import Path

import inkdrop_completed_import as importer
import inkdrop_runtime_config
import inkdrop_state


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _under(path, roots):
    candidate = Path(path)
    if candidate.is_symlink():
        raise ValueError("staged_path_must_be_a_regular_file")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError("staged_path_must_be_a_regular_file")
    for root in roots:
        try:
            resolved.relative_to(Path(root).resolve(strict=True))
            return resolved
        except (ValueError, FileNotFoundError):
            continue
    raise ValueError("staged_path_is_outside_allowed_roots")


def _operation_id(series_id, issue_number, digest):
    value = f"{series_id}|{issue_number}|{digest}".encode("utf-8")
    return "incident-" + hashlib.sha256(value).hexdigest()[:20]


def _content_fingerprint(digest):
    return hashlib.sha256(str(digest).encode("ascii")).hexdigest()[:12]


def _target_exists(con, series_id, issue_number):
    return con.execute(
        """
        select i.id
          from issues i join series s on s.id=i.series_id
         where s.id=? and (i.issue_number=? or i.normalized_number=?)
         limit 1
        """,
        (series_id, issue_number, issue_number),
    ).fetchone()


def recover_exact_artifact(
    *, state_db, completion_db, series_id, issue_number, expected_sha256,
    staged_path=None, allowed_roots=None, quarantine_root=None, apply=False, now=None,
):
    series_id = str(series_id or "").strip().lower()
    issue_number = str(issue_number or "").strip()
    digest = str(expected_sha256 or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9_-]+:\d+", series_id):
        raise ValueError("canonical_series_identity_required")
    if not issue_number or not re.fullmatch(r"\d+(?:\.\d+)?", issue_number):
        raise ValueError("exact_issue_number_required")
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("expected_sha256_must_be_64_lowercase_hex")
    now = float(now or time.time())
    operation_id = _operation_id(series_id, issue_number, digest)
    quarantine_root = Path(quarantine_root or inkdrop_runtime_config.quarantine_dir() / "incident-recovery")
    journal = quarantine_root / ".operations" / f"{operation_id}.json"
    prior = {}
    if journal.is_file():
        try:
            prior = json.loads(journal.read_text(encoding="utf-8"))
        except Exception:
            prior = {}

    source = None
    if staged_path:
        path = Path(staged_path)
        if path.exists():
            roots = list(allowed_roots or [inkdrop_runtime_config.staging_dir(), inkdrop_runtime_config.manual_inbox_dir()])
            source = _under(path, roots)
            if _sha256(source) != digest:
                raise ValueError("staged_file_hash_does_not_match_expected_content")
        elif apply:
            recovered_destination = quarantine_root / operation_id / path.name
            if recovered_destination.is_file() and _sha256(recovered_destination) == digest:
                prior = {**prior, "status": "applied", "quarantine_recovered": True}
            elif prior.get("status") != "applied":
                raise ValueError("staged_file_not_found")
        else:
            raise ValueError("staged_file_not_found")

    state_db = Path(state_db)
    completion_db = Path(completion_db)
    if not state_db.is_file():
        raise ValueError("state_database_not_found")
    state_uri = state_db.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(state_uri, uri=True) as con:
        con.row_factory = sqlite3.Row
        if not _target_exists(con, series_id, issue_number):
            raise ValueError("exact_series_issue_target_not_found")
        planned = con.execute(
            """
            select count(*)
              from import_results ir join issues i on i.id=ir.issue_id
             where ir.series_id=? and (i.issue_number=? or i.normalized_number=?)
               and coalesce(ir.verified,0)=1 and coalesce(ir.dest_path,'')<>''
            """,
            (series_id, issue_number, issue_number),
        ).fetchone()[0]
        planned_reconciliation = inkdrop_state.cleanup_missing_folder_verified_import_proofs(
            con, now, series_id=series_id, issue_number=issue_number, dry_run=True
        )
    result = {
        "ok": True,
        "dry_run": not apply,
        "operation_id": operation_id,
        "series_id": series_id,
        "issue_number": issue_number,
        "staged_file_verified": bool(source),
        "planned_verified_proofs": int(planned or 0),
        "known_bad_memory_recorded": False,
        "quarantined": bool(prior.get("quarantine_recovered")),
        "reconciliation": planned_reconciliation,
    }
    if not apply:
        return result

    completion_db.parent.mkdir(parents=True, exist_ok=True)
    old_db = importer.DB_PATH
    importer.DB_PATH = completion_db
    try:
        memory = importer.connect()
        try:
            existing_memory = memory.execute(
                "select identity from artifact_bad_content_memory where file_sha256=? limit 1",
                (digest,),
            ).fetchone()
            if not existing_memory:
                if source:
                    archive_check = importer.validate_comic_archive(source, min_pages=1, min_payload_bytes=1)
                    decision = importer.artifact_acceptance_decision(
                        source,
                        target={"title": "recovery target", "unit_type": "issue", "issue_number": issue_number},
                        archive_check=archive_check,
                    )
                    importer.record_artifact_bad_content_memory(memory, digest, source, decision)
                else:
                    importer.record_known_bad_content_sha(memory, digest, reason="incident_recovery")
            result["known_bad_memory_recorded"] = True
        finally:
            memory.close()
    finally:
        importer.DB_PATH = old_db

    if source:
        destination = quarantine_root / operation_id / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if _sha256(destination) != digest:
                raise RuntimeError("quarantine_destination_conflict")
            suffix = 2
            while True:
                candidate = destination.with_name(f"{destination.stem}-{suffix}{destination.suffix}")
                if not candidate.exists():
                    destination = candidate
                    break
                suffix += 1
        shutil.move(str(source), str(destination))
        if source.exists() or not destination.is_file() or _sha256(destination) != digest:
            raise RuntimeError("quarantine_move_verification_failed")
        result["quarantined"] = True

    with inkdrop_state.connect(state_db) as con:
        inkdrop_state.init_schema(con)
        reconciliation = inkdrop_state.cleanup_missing_folder_verified_import_proofs(
            con, now, series_id=series_id, issue_number=issue_number
        )
        raw = {
            "operation_id": operation_id,
            "series_id": series_id,
            "issue_number": issue_number,
            "content_identity_kind": "sha256",
            "content_fingerprint": _content_fingerprint(digest),
            "staged_file_verified": bool(source),
            "quarantined": bool(result["quarantined"]),
            "reconciliation": reconciliation,
        }
        con.execute(
            """
            insert or ignore into history_events(
                id,entity_type,entity_id,series_id,issue_id,event_type,source,message,
                outcome,display_phase,created_at,raw_json
            ) values(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                inkdrop_state.stable_id("exact_artifact_incident_recovery", operation_id),
                "series", series_id, series_id,
                _target_exists(con, series_id, issue_number)[0],
                "exact_artifact_incident_recovery", "maintenance",
                "Known bad artifact quarantined and exact target reconciled",
                "repaired", "cleanup", now, inkdrop_state.json_dumps(raw),
            ),
        )
        con.commit()
    result["reconciliation"] = reconciliation

    if prior.get("status") != "applied":
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal_payload = {"status": "applied", "operation_id": operation_id, "updated_at": now}
        temp = journal.with_name(f".{journal.name}.{os.getpid()}.tmp")
        temp.write_text(json.dumps(journal_payload, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temp, journal)
    return result


def main():
    parser = argparse.ArgumentParser(description="Safely reconcile one exact known-bad imported artifact")
    parser.add_argument("--series-id", required=True)
    parser.add_argument("--issue", required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--staged-path")
    parser.add_argument("--allowed-root", action="append")
    parser.add_argument("--quarantine-root")
    parser.add_argument("--state-db", default=str(inkdrop_runtime_config.state_dir() / inkdrop_state.STATE_DB_NAME))
    parser.add_argument("--completion-db", default=str(inkdrop_runtime_config.state_dir() / "imported-files.sqlite3"))
    parser.add_argument("--apply", action="store_true", help="Apply after reviewing the default dry-run")
    args = parser.parse_args()
    try:
        result = recover_exact_artifact(
            state_db=args.state_db, completion_db=args.completion_db,
            series_id=args.series_id, issue_number=args.issue,
            expected_sha256=args.expected_sha256, staged_path=args.staged_path,
            allowed_roots=args.allowed_root, quarantine_root=args.quarantine_root,
            apply=args.apply,
        )
    except (ValueError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "reason": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
