#!/usr/bin/env python3
"""Clear ONE known-bad content entry, by content hash, so a file can be re-judged.

WHY THIS EXISTS
    `artifact_bad_content_memory` is keyed on CONTENT HASH and the import loop
    consults it BEFORE acceptance -- `inkdrop_completed_import.py` computes the
    digest, calls `find_artifact_bad_content_memory()`, and on a hit logs
    `skip_known_bad_artifact_content` and `continue`s, never reaching
    `match_comic_target()` or `artifact_acceptance_decision()`. A code fix does
    not change the bytes, so a file recorded bad under an old rule can never be
    re-judged under a new one. Tracker #956.

    Until this file there was NO delete path anywhere in `core/` or `tools/`.

BULK IS IMPOSSIBLE HERE, BY CONSTRUCTION AND NOT BY DISCOURAGEMENT
    One `--sha256`, taking a single 64-hex value. No `append`, no `nargs`, no
    list file, no stdin, no glob, no `--all`, no pattern. The value is validated
    against `^[0-9a-f]{64}$` before anything opens the database. There is no code
    path in this file that can affect an entry the caller did not name.

    That is deliberate: this memory is what stops InkDrop re-downloading
    genuinely bad files forever, and 331 wanted units have touched it. It is
    never cleared wholesale.

THE SECOND GATE, WHICH DELAYS THE FIRST ONE BY UP TO A WEEK
    The slskd staging sweep keeps its own checkpoint, `slskd_staging_scan_checkpoint`,
    keyed on (path, size, mtime), and skips any file already in it -- a fully
    checkpointed set completes in 0.1s without re-deciding anything.

    That skip is BOUNDED, not permanent: `CHECKPOINT_MAX_AGE_SECONDS` defaults
    to 7 days, so a checkpointed file is re-examined once its entry ages out.
    An earlier version of this note said no fix could EVER reach such a file.
    That was wrong and it is corrected here -- the falsifier is one line,
    `inkdrop_slskd_staging_sweep.py`'s CHECKPOINT_MAX_AGE_SECONDS, and the
    comparison at both call sites is `< CHECKPOINT_MAX_AGE_SECONDS`.

    What is true is the delay, and it is enough to matter: a file decided under
    an old rule keeps that decision for up to a week after the rule is fixed.
    Measured 2026-08-31 on the live host: 2,258 of 2,327 checkpoints were still
    inside the window, and the five files cleared in this tool's first real use
    were checkpointed on 2026-08-30 18:43-19:09, so they would not have been
    re-examined until 2026-09-06. Clearing the checkpoint makes that the next
    sweep instead of next week.

WHAT IT DOES NOT DO
    It does not clear by `content_manifest_hash`. The importer's lookup matches
    `file_sha256` OR `content_manifest_hash`, so an entry recorded under a
    manifest can still block a file whose sha is cleared. Rather than widen the
    blast radius, this REPORTS that case and tells you the manifest hash, so the
    second clear is a second deliberate act.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core import inkdrop_completed_import as importer  # noqa: E402
from core import inkdrop_runtime_config  # noqa: E402

SHA256 = re.compile(r"^[0-9a-f]{64}$")
JOURNAL_NAME = "known-bad-content-clears.jsonl"


def _live(last_seen_at):
    """Was this entry still blocking, or had it aged out?

    The importer's lookup discards a row older than the TTL, so a clear against
    an expired entry changes nothing about what gets imported. That distinction
    is invisible once the row is deleted, and it was the first question asked
    after the first real use -- so it is recorded, not inferred later.
    """
    try:
        age = time.time() - float(last_seen_at)
    except (TypeError, ValueError):
        return None, None
    return age <= importer.ARTIFACT_BAD_CONTENT_MEMORY_TTL_SECONDS, age / 86400.0


def _rows_for(conn, digest):
    importer.ensure_artifact_bad_content_memory_schema(conn)
    return conn.execute(
        "select identity, file_sha256, content_manifest_hash, decision, reason_codes,"
        " source_path, last_seen_at, seen_count"
        " from artifact_bad_content_memory where file_sha256=?",
        (digest,),
    ).fetchall()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    # ONE hash. Not a list, not a file, not a pattern -- see the module docstring.
    ap.add_argument("--sha256", required=True, help="the file's content hash, 64 hex characters")
    ap.add_argument("--reason", required=True, help="why this entry is being cleared; journalled")
    ap.add_argument("--by", default=os.environ.get("INKDROP_ACTOR") or "unattributed")
    ap.add_argument("--db", default=None, help="override the completion DB path")
    # WHERE TO LOOK, NOT WHAT TO CLEAR. The sweep checkpoint is keyed on path
    # and the known-bad row is what maps a hash to its path -- so once that row
    # is gone, a hash alone can no longer find the checkpoint that is still
    # skipping the file. This supplies that one path and nothing else: a single
    # value, matched exactly, with globs refused. It cannot name a second file,
    # and it never widens which known-bad entry is cleared -- that is still the
    # one --sha256 names, and only that one.
    ap.add_argument("--staging-path", default=None,
                    help="exact path whose staging checkpoint to clear, when the known-bad "
                         "row that would have supplied it is already gone")
    ap.add_argument("--apply", action="store_true", help="write; otherwise dry-run")
    a = ap.parse_args()

    digest = a.sha256.strip().lower()
    if not SHA256.match(digest):
        raise SystemExit(
            "refusing: --sha256 must be exactly 64 hex characters, got %d. This tool clears ONE "
            "named entry and has no bulk mode by construction." % len(digest))
    if not a.reason.strip():
        raise SystemExit("refusing: --reason is required and is journalled")
    staging_path = (a.staging_path or "").strip()
    if staging_path:
        # THE SCOPE GUARANTEE IS STRUCTURAL, NOT THIS CHECK. The delete below is
        # `where path=?`, an exact SQL equality, and nothing in this file globs
        # anything -- so a metacharacter in a path is inert here. This is a
        # tripwire against an operator who THINKS a pattern will work, and it
        # must not refuse real filenames.
        #
        # It did. `[` and `]` were rejected as glob characters, and every one of
        # the five files this argument was written for is named
        # `... #001 [2016-06].cbz`. Square brackets are ordinary in comic
        # filenames; a guard that refuses its own worked example is over-refusing,
        # which is the same defect as a guard that lets the wrong thing through.
        if (any(ch in staging_path for ch in "*?")
                or len(staging_path.splitlines()) > 1
                or staging_path in (".", "/")):
            raise SystemExit(
                "refusing: --staging-path takes ONE exact path, not a pattern. This tool has no "
                "bulk mode by construction and this argument does not add one.")

    db = Path(a.db) if a.db else importer.DB_PATH
    if not db.is_file():
        raise SystemExit("no completion database at %s" % db)
    conn = sqlite3.connect(str(db))
    try:
        rows = _rows_for(conn, digest)
        print("known-bad entries matching file_sha256 %s: %d" % (digest[:12], len(rows)))
        for r in rows:
            print("  identity=%s decision=%s reasons=%s seen=%s" % (str(r[0])[:28], r[3], r[4], r[7]))
            print("    source_path=%s" % str(r[5])[:96])
            live, age_days = _live(r[6])
            print("    last_seen=%s -> %s" % (
                "age %.1fd" % age_days if age_days is not None else "unreadable",
                "LIVE, this entry is blocking" if live else
                "EXPIRED past the %.0fd TTL, this entry was NOT blocking"
                % (importer.ARTIFACT_BAD_CONTENT_MEMORY_TTL_SECONDS / 86400.0)))
            if r[2]:
                print("    content_manifest_hash=%s (goes with this row)" % r[2][:16])
        if not rows and not staging_path:
            print("nothing to clear. This is not an error: the file may be blocked by a"
                  " content_manifest_hash entry instead, which this tool does not touch."
                  " If the known-bad row was already cleared and the staging checkpoint is"
                  " still skipping the file, pass --staging-path with that one file's path.")
            return 0
        if not rows and staging_path:
            print("no known-bad entry for this hash; proceeding for the staging checkpoint only.")
        if not a.apply:
            print("\nDRY RUN. Nothing written. Re-run with --apply.")
            return 0

        # Journal BEFORE the delete: a clear that is not recorded is a silent
        # edit to a safety mechanism, and this one has to be auditable later.
        journal = Path(inkdrop_runtime_config.state_dir()) / JOURNAL_NAME
        journal.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "cleared_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "by": a.by,
            "reason": a.reason.strip(),
            "file_sha256": digest,
            "staging_path_supplied_by_caller": staging_path or None,
            # The journal is written BEFORE the delete on purpose, so this records
            # what will be cleared, not a count read back afterwards.
            "staging_checkpoint_paths": sorted(
                {r[5] for r in rows if r[5]} | ({staging_path} if staging_path else set())),
            "entries": [
                {"identity": r[0], "decision": r[3], "reason_codes": r[4],
                 "source_path": r[5], "content_manifest_hash": r[2],
                 "last_seen_at": r[6],
                 "was_live_at_clear": _live(r[6])[0],
                 "age_days_at_clear": (None if _live(r[6])[1] is None
                                       else round(_live(r[6])[1], 3))}
                for r in rows
            ],
        }
        with journal.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

        cur = conn.execute("delete from artifact_bad_content_memory where file_sha256=?", (digest,))

        # The staging sweep's own "already decided" record for the SAME file.
        # Its path is taken from the row above, never from the caller, so this
        # cannot reach a file the caller did not name. Without it the sweep
        # never re-examines the file and the clear above achieves nothing.
        swept = 0
        checkpoint_paths = {r[5] for r in rows if r[5]}
        if staging_path:
            checkpoint_paths.add(staging_path)
        for source_path in sorted(checkpoint_paths):
            swept += conn.execute(
                "delete from slskd_staging_scan_checkpoint where path=?", (source_path,)).rowcount
        conn.commit()
        print("\ncleared %d entr%s. Journalled to %s"
              % (cur.rowcount, "y" if cur.rowcount == 1 else "ies", journal))

        # Read back through the SAME predicate every consumer uses, or the write
        # is only reported, not proven.
        left = _rows_for(conn, digest)
        if left:
            raise SystemExit("the entry is still present after the delete: %d row(s)" % len(left))
        # Only a SURVIVING row can still block. The first version warned whenever
        # the deleted row merely HAD a manifest hash, which fired on all five of
        # its first real uses while none of them was still blocked -- a warning
        # that cries wolf on every run trains the reader to skip it.
        survivors = []
        for manifest in sorted({r[2] for r in rows if r[2]}):
            n = conn.execute(
                "select count(*) from artifact_bad_content_memory where content_manifest_hash=?",
                (manifest,)).fetchone()[0]
            if n:
                survivors.append("%s (%d row%s)" % (manifest[:16], n, "" if n == 1 else "s"))
        if survivors:
            print("STILL BLOCKED: another row shares content_manifest_hash %s and the importer"
                  " matches EITHER key. Clear it separately and deliberately."
                  % ", ".join(survivors))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
