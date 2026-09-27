#!/usr/bin/env python3
"""Exact-target, hash-bound recovery for a known bad imported artifact.

RECOVERY HERE MEANS RECOVERING THE LIBRARY FROM A BAD FILE, NOT RECOVERING THE FILE.
    --apply RECORDS the file's content as known-bad in
    `artifact_bad_content_memory`, so every later import of those bytes is
    refused, QUARANTINES the staged file by moving it out of its staging root,
    and retracts this issue's stale verified import proofs. It never clears or
    un-rejects anything; `tools/inkdrop_clear_known_bad_content.py` does that.

    The name reads the other way: someone
    looking for a way to clear five known-bad staged files found this as the
    only file whose name suggested it, and the flags it asks for would have
    recorded them bad and moved them out of staging. The dry run used to open
    with `{` and report `"known_bad_memory_recorded": false`, true of that run
    and read as a promise about the tool. So the dry run's first line now says
    what --apply does (`dry_run_banner()`), and so does the first line of
    --help and of any run argparse refuses (`EFFECT_LINE`). A rename was the
    row's other arm and was not taken here: the module is named in the public
    export and release lists and the Docker allowlist, and a rename helps only
    someone who reads the filename, while the banner reaches the person who has
    already typed the command.
"""
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))


import argparse
import codecs
import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
from pathlib import Path

from core import inkdrop_completed_import as importer
from core import inkdrop_runtime_config
from core import inkdrop_state


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


def _quarantine_root(quarantine_root=None):
    return Path(quarantine_root or inkdrop_runtime_config.quarantine_dir() / "incident-recovery")


def dry_run_banner(staged_path=None, quarantine_root=None):
    """What --apply will do, in words, for the top of a dry run's output.

    The first line carries the whole warning on its own -- RECORDS known-bad,
    QUARANTINES the file -- because the first line is what a person scanning
    for "did it work" reads, and the JSON below it describes this run, where
    every action field is false. Built from the arguments alone, so a dry run
    that is refused prints it too: someone adjusting flags until the dry run
    passes sees it on the first attempt. Without --staged-path, --apply records
    the hash and moves nothing, and the line says so rather than promising a
    move.

    The opening says only what this run will not do, because it is printed
    before the run starts. It used to say "nothing was changed", which no run
    had yet earned, and a dry run on a state database with no -wal or -shm file
    does create them. What it can promise is that no known-bad entry is written
    and no file is moved, and both hold for a refused run too.
    """
    if staged_path:
        first = (
            "DRY RUN: this run writes no known-bad entry and moves no file. --apply does not clear or restore "
            "anything: it RECORDS this content as known-bad, so every future import of it is refused, and "
            f"QUARANTINES the file, moving {staged_path} into {_quarantine_root(quarantine_root)}."
        )
    else:
        first = (
            "DRY RUN: this run writes no known-bad entry and moves no file. --apply does not clear or restore "
            "anything: it RECORDS this content hash as known-bad, so every future import of it is refused. "
            "It QUARANTINES only a file named with --staged-path, and none was, so no file is moved."
        )
    return [
        first,
        "To clear a known-bad entry so a file can be judged again, use tools/inkdrop_clear_known_bad_content.py.",
        '--apply also retracts stale verified import proofs for this issue; a dry run that succeeds counts them under "reconciliation".',
    ]


# The first line of --help and of any run argparse refuses. No staged path is
# known there, so the sentence names the flag instead of a file.
EFFECT_LINE = (
    "This tool does not clear or restore anything: with --apply it RECORDS a file's content as known-bad, "
    "so every future import of it is refused, and QUARANTINES the file named with --staged-path, "
    "moving it into quarantine."
)


class _EffectFirstParser(argparse.ArgumentParser):
    """argparse with the effect sentence ahead of --help and of every refusal.

    A run missing a flag -- someone who has the staged file and has not hashed
    it yet -- printed only argparse's usage and error, and --help opened with
    the usage line. Neither said what the tool does.
    """

    def format_help(self):
        return EFFECT_LINE + "\n\n" + super().format_help()

    def error(self, message):
        _sys.stderr.write(EFFECT_LINE + "\n")
        super().error(message)


def _stdout_survives_any_name():
    """Print a staged name the stdout encoding cannot hold as escapes instead of dying.

    A redirected stdout on Windows encodes with the ANSI code page, so a staged
    path outside it raised UnicodeEncodeError on the dry run's first line,
    before any work; the JSON alone never did, because json.dumps escapes
    non-ASCII. A UTF-8 stdout using surrogateescape is left as it is, so
    undecodable bytes in a name still round-trip there.
    """
    stream = _sys.stdout
    try:
        encoding = codecs.lookup(stream.encoding).name
        errors = stream.errors
    except (AttributeError, LookupError, TypeError):
        return
    if (encoding, errors) == ("utf-8", "surrogateescape") or errors == "backslashreplace":
        return
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(errors="backslashreplace")


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
    quarantine_root = _quarantine_root(quarantine_root)
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
    # The description used to read "Safely reconcile one exact known-bad
    # imported artifact", which names no action at all.
    _stdout_survives_any_name()
    parser = _EffectFirstParser(
        description=(
            "Record one file's content as known-bad and move the staged file into quarantine. "
            "Dry run unless --apply. This never clears a known-bad entry; "
            "tools/inkdrop_clear_known_bad_content.py does."
        )
    )
    parser.add_argument("--series-id", required=True)
    parser.add_argument("--issue", required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--staged-path")
    parser.add_argument("--allowed-root", action="append")
    parser.add_argument("--quarantine-root")
    parser.add_argument("--state-db", default=str(inkdrop_runtime_config.state_dir() / inkdrop_state.STATE_DB_NAME))
    parser.add_argument("--completion-db", default=str(inkdrop_runtime_config.state_dir() / "imported-files.sqlite3"))
    parser.add_argument(
        "--apply", action="store_true",
        help="Record the content as known-bad and quarantine the staged file, after reviewing the default dry run",
    )
    args = parser.parse_args()
    if not args.apply:
        # Printed before the call so a refused dry run carries it too. Plain
        # text on stdout ahead of the JSON, as tools/inkdrop_clear_known_bad_content.py
        # does: nothing in the tree parses this CLI's output, and stderr was
        # rejected because a captured or merged stream can put it after the
        # JSON, which is the one place it does no good. --apply keeps printing
        # the JSON alone.
        print("\n".join(dry_run_banner(args.staged_path, args.quarantine_root)))
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
