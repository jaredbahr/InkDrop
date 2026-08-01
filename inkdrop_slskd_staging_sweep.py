"""Resumable wrapper around `inkdrop_completed_import.py --slskd-staging`.

Problem: a plain `--all-series --slskd-staging` run has to walk and evaluate
every file in the SLSKD staging root before it can decide what's new, which
on the real backlog takes far longer than this host's containers currently
stay up between redeploys. A restart mid-scan throws all of that work away
and the next run starts over from zero.

Fix: process one file at a time via the *existing, unmodified*
`inkdrop_completed_import.py --source-file <path> --all-series --slskd-staging`
invocation (that flag already exists and already bypasses the broad
directory walk for a single explicit file), and persist each file's outcome
to a checkpoint table keyed on (path, size, mtime) immediately after it's
decided -- not batched at the end. A file already checkpointed with an
unchanged size/mtime is skipped without touching the expensive matching /
hashing / archive-validation path again. This does not change any matching,
duplicate-detection, or import logic at all -- it only decides which files
are worth asking `inkdrop_completed_import.py` about on a given run.
"""
import fcntl
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import time

import inkdrop_runtime_config


def extract_issue_number(filename):
    """Best-effort volume/issue number extraction from a release filename.

    Mirrors the pattern used during the backlog audit tonight -- explicit
    '#032' style markers first, then a bare number with a plausible
    boundary. Not perfect, but --trusted-issue only helps precision; if this
    comes back empty the file is still processed, just without that extra
    signal, matching what a human operator would do without it.
    """
    m = re.search(r"#\s*0*(\d{1,4})", filename)
    if m:
        return m.group(1)
    m = re.search(r"\bv(?:ol(?:ume)?)?\.?\s*0*(\d{1,4})\b", filename, re.I)
    if m:
        return m.group(1)
    m = re.search(r"\b0*(\d{2,4})\b(?=\s*\(|\.\w+$|\s)", filename)
    if m:
        return m.group(1)
    return None

# Resolved through inkdrop_runtime_config rather than written out, so no
# operator's home directory or NAS mount is baked into the source. These used to
# be literal defaults; that made the file unshippable -- the public release
# gates correctly refuse to export a private path -- and it silently tied the
# sweep to one host. The env vars still win where set, which is how this
# deployment configures every one of them, so resolution here is unchanged.
SLSKD_ROOT = os.environ.get("INKDROP_SLSKD_DOWNLOAD_ROOT") or str(
    inkdrop_runtime_config.staging_dir() / "slskd"
)
LEDGER_DB = os.environ.get("INKDROP_IMPORTED_FILES_DB") or str(
    inkdrop_runtime_config.imported_files_db_path()
)
STATE_DB = os.environ.get("INKDROP_STATE_DB") or str(
    inkdrop_runtime_config.state_db_path()
)
# Confirmed live: SLSKD's own upstream matching regularly stages real,
# already-verified candidates (source_attempts status='staged_file_ready')
# well before this sweep gets around to them -- on a ~1,350-file backlog
# with random processing order, a fresh legitimate match can sit unprocessed
# for 12+ hours purely on shuffle luck. Since these are the one class of
# file this sweep already KNOWS are worth trying (upstream already decided
# they match a wanted item), give them first crack at the per-run budget
# instead of leaving them to chance alongside the rest of the backlog.
PRIORITY_LOOKBACK_SECONDS = int(os.environ.get("INKDROP_SLSKD_SWEEP_PRIORITY_LOOKBACK_SECONDS", 7 * 24 * 3600))
ARCHIVE_EXT = {".cbz", ".cbr", ".zip", ".pdf"}
CHECKPOINT_MAX_AGE_SECONDS = int(os.environ.get("INKDROP_SLSKD_SWEEP_CHECKPOINT_MAX_AGE_SECONDS", 7 * 24 * 3600))
# Comics-kind files have no cheap live-existence check the way manga does
# (find_existing_manga_unit_file scans the target folder directly; comics
# falls through to the sha256 ledger, which requires hashing the whole
# candidate file first regardless of whether it turns out new or duplicate).
# Measured live: a known-duplicate comics file still took >120s to resolve,
# and a genuinely new one exceeded the original 180s default outright,
# timing out (uncheckpointed, so retried every run) before it ever finished.
# 900s carries real margin above every wall-clock cost observed tonight,
# including the 8-minute page-by-page validation on a new manga import.
PER_FILE_TIMEOUT_SECONDS = int(os.environ.get("INKDROP_SLSKD_SWEEP_PER_FILE_TIMEOUT_SECONDS", 900))
TOTAL_BUDGET_SECONDS = int(os.environ.get("INKDROP_SLSKD_SWEEP_BUDGET_SECONDS", 2400))
MAX_IMPORTS = int(os.environ.get("INKDROP_SLSKD_SWEEP_MAX_IMPORTS", 10))
# A single file that hangs or errors repeatedly (observed live: one file blocked
# in uninterruptible I/O for 900s+ with a verified-valid archive underneath it --
# a real hang, not slow validation) sits first in enumeration order on every run
# and burns its entire per-file timeout before any other file gets a turn,
# blocking the whole batch's progress run after run, not just its own. Two
# independent mitigations: shuffle processing order so a stuck file doesn't
# always claim the front of the queue, and set aside a file after repeated
# failures for a cooldown window instead of retrying it every single run.
MAX_CONSECUTIVE_ERRORS = int(os.environ.get("INKDROP_SLSKD_SWEEP_MAX_CONSECUTIVE_ERRORS", 3))
ERROR_COOLDOWN_SECONDS = int(os.environ.get("INKDROP_SLSKD_SWEEP_ERROR_COOLDOWN_SECONDS", 3600))
COMPLETED_IMPORT_SCRIPT = os.environ.get("INKDROP_COMPLETED_IMPORT_SCRIPT", "/app/inkdrop_completed_import.py")
PYTHON_BIN = os.environ.get("PYTHON_BIN", sys.executable or "python3")
DRY_RUN = os.environ.get("INKDROP_SLSKD_SWEEP_DRY_RUN", "0") == "1"
# inkdrop-comics-import.lock is shared with completed-import-comics, the web
# UI's manual-source-import action, and the queue runner (inkdrop_web.py:
# with_import_lock, queue_runner_import_cmd) -- confirmed live tonight that
# the scheduler previously held this SAME lock for this whole sweep's entire
# run (up to TOTAL_BUDGET_SECONDS, ~40 minutes) via an outer `flock` wrapping
# the whole process, not scoped to any one file. That blocked every other
# consumer of this lock -- scheduled jobs, manual web imports, and the queue
# runner -- for the full duration regardless of which specific file the
# sweep happened to be on. Acquired here per-file instead, held only for the
# one subprocess call that actually needs it, so unrelated files (and
# unrelated consumers of the same lock) aren't blocked by one slow file.
# A private, sweep-only lock (see main()) still prevents two sweep instances
# from running concurrently -- that's a different concern from this one.
COMICS_IMPORT_LOCK_PATH = os.environ.get("INKDROP_COMICS_IMPORT_LOCK_PATH") or str(
    inkdrop_runtime_config.lock_path("inkdrop-comics-import.lock")
)
COMICS_IMPORT_LOCK_WAIT_SECONDS = int(os.environ.get("INKDROP_COMICS_IMPORT_LOCK_WAIT_SECONDS", 30))


def acquire_comics_import_lock(wait_seconds=COMICS_IMPORT_LOCK_WAIT_SECONDS):
    os.makedirs(os.path.dirname(COMICS_IMPORT_LOCK_PATH), exist_ok=True)
    lock_file = open(COMICS_IMPORT_LOCK_PATH, "a")
    deadline = time.time() + wait_seconds
    while True:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return lock_file
        except OSError:
            if time.time() >= deadline:
                lock_file.close()
                return None
            time.sleep(1)


def release_comics_import_lock(lock_file):
    if lock_file is None:
        return
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        lock_file.close()


def ensure_checkpoint_table(con):
    con.execute(
        """
        create table if not exists slskd_staging_scan_checkpoint (
            path text primary key,
            size integer,
            mtime real,
            decision text,
            reason text,
            dest text,
            checked_at real
        )
        """
    )
    con.execute(
        """
        create table if not exists slskd_staging_scan_error_counts (
            path text primary key,
            error_count integer,
            last_error_at real,
            last_reason text
        )
        """
    )
    con.commit()


def load_error_counts(con):
    rows = con.execute("select path, error_count, last_error_at from slskd_staging_scan_error_counts").fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


def record_error(con, path, reason):
    con.execute(
        """
        insert into slskd_staging_scan_error_counts (path, error_count, last_error_at, last_reason)
        values (?, 1, ?, ?)
        on conflict(path) do update set
            error_count = error_count + 1,
            last_error_at = excluded.last_error_at,
            last_reason = excluded.last_reason
        """,
        (path, time.time(), reason),
    )
    con.commit()


def clear_error_count(con, path):
    con.execute("delete from slskd_staging_scan_error_counts where path = ?", (path,))
    con.commit()


def enumerate_files(root):
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in filenames:
            ext = os.path.splitext(name)[1].lower()
            if ext in ARCHIVE_EXT:
                full = os.path.join(dirpath, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                files.append((full, st.st_size, st.st_mtime))
    return files


def load_checkpoints(con):
    rows = con.execute("select path, size, mtime, checked_at from slskd_staging_scan_checkpoint").fetchall()
    return {(r[0], r[1], r[2]): r[3] for r in rows}


def load_priority_paths():
    """Real file paths SLSKD's own matching already confirmed as a staged,
    ready-to-import candidate for some wanted item -- these are worth trying
    before spending budget on the rest of the (mostly unmatched) backlog."""
    try:
        con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=10)
        con.execute("pragma busy_timeout = 10000")
    except sqlite3.OperationalError:
        return set()
    try:
        cutoff = time.time() - PRIORITY_LOOKBACK_SECONDS
        rows = con.execute(
            """
            select raw_json from source_attempts
            where lower(source) = 'slskd' and status = 'staged_file_ready'
              and coalesce(completed_at, started_at, 0) > ?
            """,
            (cutoff,),
        ).fetchall()
    except sqlite3.OperationalError:
        return set()
    finally:
        con.close()

    paths = set()
    for (raw_json,) in rows:
        try:
            raw = json.loads(raw_json or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(raw, dict):
            continue
        path = raw.get("staged_path") or raw.get("dest_path") or raw.get("path") or raw.get("local_path")
        if isinstance(path, str) and path.startswith("/") and os.path.exists(path):
            paths.add(path)
    return paths


def process_one_file(path):
    cmd = [
        PYTHON_BIN,
        "-B",
        COMPLETED_IMPORT_SCRIPT,
        "--kind",
        "comics",
        "--slskd-staging",
        "--all-series",
        "--source-file",
        path,
        # Without this the child polls the library scan for up to
        # SOURCE_FILE_SCAN_TIMEOUT_SECONDS (420) per file -- and this function
        # holds the shared comics-import lock for the child's entire lifetime,
        # so that poll blocks every other importer too. Measured live: one .cbr
        # sat at 545s and climbing while the lock probe came back HELD on three
        # of three samples, 27 files had already recorded per_file_timeout, and
        # only 11 files had ever been resolved. A dry run of the same shape of
        # file costs 2.4s, so essentially all of that time was the wait.
        # Against a 2400s budget that is two or three files a run, on a backlog
        # of roughly 1,300 archives.
        #
        # Dropping the poll does not drop a check. With wait_for_library_scan
        # false the child simply never calls verify_imported_items, so nothing
        # is recorded as verified without proof; arrival is still confirmed
        # afterwards by the folder-presence backfill and by
        # verified-import-projection. The same flag is already how
        # import_status_queue_backed_source_file_child() expects a per-file
        # child to be invoked.
        "--no-wait-for-library-scan",
    ]
    issue_number = extract_issue_number(os.path.basename(path))
    if issue_number:
        cmd.extend(["--trusted-issue", issue_number])
    if DRY_RUN:
        cmd.append("--dry-run")

    lock_file = acquire_comics_import_lock()
    if lock_file is None:
        # Transient -- something else legitimately using the shared lock
        # right now (not this file's fault), so this isn't checkpointed or
        # counted toward the file's own error cooldown; it's simply tried
        # again on a later run like any other not-yet-processed candidate.
        return {"decision": "lock_busy", "reason": "comics_import_lock_busy", "dest": None}
    try:
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=PER_FILE_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return {"decision": "error", "reason": "per_file_timeout", "dest": None}
    finally:
        release_comics_import_lock(lock_file)

    if proc.returncode != 0:
        return {"decision": "error", "reason": f"rc={proc.returncode} stderr_tail={proc.stderr[-500:]}", "dest": None}
    try:
        payload = json.loads(proc.stdout)
    except (ValueError, TypeError):
        return {"decision": "error", "reason": "unparseable_output", "dest": None}
    imported = payload.get("imported") or []
    skipped = payload.get("skipped") or []
    if imported:
        entry = imported[0]
        return {"decision": "imported", "reason": None, "dest": entry.get("dest")}
    if skipped:
        entry = skipped[0]
        reason = entry.get("skip_reason") or entry.get("event") or "skipped"
        return {"decision": "skipped", "reason": reason, "dest": entry.get("dest")}
    return {"decision": "skipped", "reason": "no_decision_returned", "dest": None}


def main():
    started = time.time()
    con = sqlite3.connect(LEDGER_DB, timeout=30)
    con.execute("pragma busy_timeout = 30000")
    ensure_checkpoint_table(con)
    checkpoints = load_checkpoints(con)
    error_counts = load_error_counts(con)

    all_files = enumerate_files(SLSKD_ROOT)
    now = time.time()
    to_process = []
    skipped_via_checkpoint = 0
    skipped_via_error_cooldown = 0
    for path, size, mtime in all_files:
        key = (path, size, mtime)
        checked_at = checkpoints.get(key)
        if checked_at is not None and (now - checked_at) < CHECKPOINT_MAX_AGE_SECONDS:
            skipped_via_checkpoint += 1
            continue
        err_count, last_error_at = error_counts.get(path, (0, None))
        if err_count >= MAX_CONSECUTIVE_ERRORS and last_error_at is not None and (now - last_error_at) < ERROR_COOLDOWN_SECONDS:
            skipped_via_error_cooldown += 1
            continue
        to_process.append((path, size, mtime))

    # Don't let a single repeatedly-hanging file always claim the first slot
    # and burn the whole per-file timeout before anything else gets a turn --
    # confirmed live tonight: one file blocked in uninterruptible I/O for
    # 900s+ while sitting first in (deterministic) directory-walk order.
    random.shuffle(to_process)

    # Files SLSKD's own upstream matching already staged as a real candidate
    # jump the queue -- confirmed live tonight: real matches were sitting
    # unprocessed for 12+ hours on pure shuffle bad luck against a ~1,350-file
    # backlog dominated by non-matching content. This doesn't change what
    # counts as a match, only which known-good files get tried first.
    priority_paths = load_priority_paths()
    if priority_paths:
        priority_items = [item for item in to_process if item[0] in priority_paths]
        rest_items = [item for item in to_process if item[0] not in priority_paths]
        to_process = priority_items + rest_items

    summary = {
        "total_files_on_disk": len(all_files),
        "skipped_via_checkpoint": skipped_via_checkpoint,
        "skipped_via_error_cooldown": skipped_via_error_cooldown,
        "priority_candidates_this_run": len(priority_paths),
        "candidates_to_process_this_run": len(to_process),
        "processed_this_run": 0,
        "imported_this_run": 0,
        "skipped_this_run": 0,
        "errors_this_run": 0,
        "lock_busy_this_run": 0,
        "stopped_reason": None,
    }

    imports_done = 0
    for path, size, mtime in to_process:
        if time.time() - started > TOTAL_BUDGET_SECONDS:
            summary["stopped_reason"] = "time_budget_exhausted"
            break
        if imports_done >= MAX_IMPORTS:
            summary["stopped_reason"] = "max_imports_reached"
            break

        result = process_one_file(path)

        if result["decision"] == "lock_busy":
            # Something else legitimately holding the shared comics-import
            # lock right now -- not this file's fault and not a decision
            # about the file at all, so it doesn't count toward processed/
            # error tallies or the file's own error cooldown. Just retried
            # on a later run like anything else not yet processed.
            summary["lock_busy_this_run"] += 1
            continue

        summary["processed_this_run"] += 1
        if result["decision"] == "imported":
            summary["imported_this_run"] += 1
            imports_done += 1
        elif result["decision"] == "error":
            summary["errors_this_run"] += 1
        else:
            summary["skipped_this_run"] += 1

        if result["decision"] == "error" or result["reason"] == "no_decision_returned":
            # Not confident this is terminal -- don't checkpoint it, but do
            # track how many times in a row it's failed so a genuinely
            # broken/hanging file gets set aside for a cooldown instead of
            # eating a full timeout slot on every single future run too.
            record_error(con, path, result["reason"])
            continue

        clear_error_count(con, path)
        con.execute(
            """
            insert or replace into slskd_staging_scan_checkpoint
                (path, size, mtime, decision, reason, dest, checked_at)
            values (?, ?, ?, ?, ?, ?, ?)
            """,
            (path, size, mtime, result["decision"], result["reason"], result["dest"], time.time()),
        )
        con.commit()

    if summary["stopped_reason"] is None:
        summary["stopped_reason"] = "all_candidates_processed"

    summary["elapsed_seconds"] = round(time.time() - started, 1)
    print(json.dumps(summary, indent=2))
    con.close()


if __name__ == "__main__":
    main()
