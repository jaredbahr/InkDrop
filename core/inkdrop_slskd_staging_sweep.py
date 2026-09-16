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
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))

import fcntl
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import time

from core import inkdrop_runtime_config
from core import inkdrop_archive_conversion

try:
    from core import inkdrop_state
except Exception:
    inkdrop_state = None


def extract_issue_number(filename):
    """Best-effort issue number extraction from a release filename.

    The ordering is the point. An explicit `issue` marker outranks a volume
    marker, because a release like "Spawn (1992) Volume 01 Issue 169.cbz"
    states both and only one of them is the issue. Without that rule this
    returned the volume, and that value went out as --trusted-issue to a
    child that parses the same string with
    inkdrop_completed_import.extract_issue_number() -- which does read the
    `Issue` token. The two disagreed and trusted_issue_mismatch_reason()
    refused the file as "trusted_issue_mismatch:169!=001", on a Manual Review
    card that simultaneously showed the row's issue as not recorded. Both
    were true: nothing here ever came from the queue, despite the flag name.
    Measured over the live 2,393-file staging tree, 39 correct files were
    refused that way, across three unrelated series.

    The volume branch also refuses a year, mirroring the negative lookahead
    inkdrop_completed_import.extract_issue_number() already applies on its
    own volume pattern, so "... V2015 001 [09-2015].cbz" stops sending 2015
    as an issue number -- 16 such year-shaped values in the same tree.

    Only the singular `issue` is matched: "Issues 001-050" is a pack marker,
    not a unit, and still falls through to the branches below exactly as
    before.

    Not perfect, but --trusted-issue only helps precision; if this comes back
    empty the file is still processed, just without that extra signal,
    matching what a human operator would do without it.
    """
    m = re.search(r"\bissue[\s._#-]*0*(\d{1,4})\b", filename, re.I)
    if m:
        return m.group(1)
    m = re.search(r"#\s*0*(\d{1,4})", filename)
    if m:
        return m.group(1)
    m = re.search(r"\bv(?:ol(?:ume)?)?\.?\s*0*(?!(?:19|20)\d{2}\b)(\d{1,4})\b", filename, re.I)
    if m:
        return m.group(1)
    m = re.search(r"\b0*(\d{2,4})\b(?=\s*\(|\.\w+$|\s)", filename)
    if m:
        return m.group(1)
    return None

# Resolved through inkdrop_runtime_config rather than written out, so no
# host-specific home directory or NAS mount is baked into the source. The
# environment variables still take precedence wherever they are set.
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
# Deliberately much longer than PRIORITY_LOOKBACK_SECONDS above: a
# transfer_succeeded_missing_stage download_task is a *stuck* item still
# waiting to be found, not a fresh candidate -- the older it is, the more it
# needs the priority boost, not less. Confirmed live: the real Frieren case
# this priority path exists for was already 7.07 days old (i.e. already past
# a 7-day cutoff) by the time it was found stuck -- a same-length lookback
# here would have excluded the exact case it was written to fix.
MISSING_STAGE_PRIORITY_LOOKBACK_SECONDS = int(
    os.environ.get("INKDROP_SLSKD_SWEEP_MISSING_STAGE_PRIORITY_LOOKBACK_SECONDS", 90 * 24 * 3600)
)
ARCHIVE_EXT = {".cbz", ".cbr", ".zip", ".pdf"}
CHECKPOINT_MAX_AGE_SECONDS = int(os.environ.get("INKDROP_SLSKD_SWEEP_CHECKPOINT_MAX_AGE_SECONDS", 7 * 24 * 3600))
# A checkpoint records a decision, and the decision belongs to the code that made it.
# When a change lets the importer decide a refusal differently, name that refusal here
# under a new contract: files refused for it are judged once more by the new code
# instead of waiting out CHECKPOINT_MAX_AGE_SECONDS. Every other checkpoint, and every
# import, keeps its age. 2026-09-13: title-only files of single-issue works stopped being
# refused as weak filename evidence, and 25 of them sat in Manual Review on week-old
# refusals.
CHECKPOINT_CONTRACT = "2026-09-13-single-issue-title-only"
CHECKPOINT_REJUDGED_REASONS = frozenset({"weak_filename_unit_evidence", "filename_confidence_too_low"})
# A folder of loose scan pages (no archive at all -- real example: a peer
# sharing 43 numbered .png pages with no .cbz/.cbr anywhere) is invisible to
# every check above; ARCHIVE_EXT never matches a bare .png/.jpg. Converting
# it to a CBZ here, once, is what makes the existing archive pipeline above
# able to see it on a later run -- nothing about matching or import changes.
PAGE_DIRECTORY_TIMEOUT_SECONDS = int(os.environ.get("INKDROP_SLSKD_SWEEP_PAGE_DIRECTORY_TIMEOUT_SECONDS", 300))
MAX_PAGE_DIRECTORY_CONVERSIONS = int(os.environ.get("INKDROP_SLSKD_SWEEP_MAX_PAGE_DIRECTORY_CONVERSIONS", 5))
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
COMPLETED_IMPORT_SCRIPT = os.environ.get(
    "INKDROP_COMPLETED_IMPORT_SCRIPT",
    str(_Path(__file__).resolve().with_name("inkdrop_completed_import.py")),
)
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
    columns = {row[1] for row in con.execute("pragma table_info(slskd_staging_scan_checkpoint)")}
    if "contract" not in columns:
        con.execute("alter table slskd_staging_scan_checkpoint add column contract text")
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


def incomplete_root_for(root):
    """slskd's in-progress transfer folder for this download root.

    slskd writes a transfer there and moves it out only once it finishes; an
    errored transfer leaves its partial behind. Nothing in it is a finished
    file, so the sweep must never judge it. On the 2026-09-13 snapshot four of
    the fourteen comic_archive_regrab_needed rows were such partials, two with
    slskd records reading "Completed, Errored" at 3.8 of 67.7 MB and 1.3 of
    131 MB. Resolved the way inkdrop_slskd_source_probe resolves it when no
    provider setting is given: the environment first, then root/incomplete.
    """
    configured = (os.environ.get("INKDROP_SLSKD_INCOMPLETE_ROOT") or "").strip()
    return configured or os.path.join(str(root), "incomplete")


def prune_incomplete_root(dirpath, dirnames, incomplete_root):
    """Drop the incomplete folder from an os.walk() listing in place.

    Matched by full path, not by name, so a peer's own folder that happens to
    be called "incomplete" deeper in the tree is still walked. The probe's
    walk likewise prunes only at the download root.
    """
    target = os.path.normcase(os.path.abspath(incomplete_root))
    dirnames[:] = [
        name for name in dirnames
        if os.path.normcase(os.path.abspath(os.path.join(dirpath, name))) != target
    ]


def enumerate_files(root):
    files = []
    incomplete_root = incomplete_root_for(root)
    for dirpath, dirnames, filenames in os.walk(root):
        prune_incomplete_root(dirpath, dirnames, incomplete_root)
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


def raw_image_page_folders_setting_enabled():
    """Whether Settings > Quality > Allow Raw Page-Image Folders is on.

    Same source-of-truth pattern as the other quality.* toggles this sweep
    doesn't otherwise read: an explicit user override wins, anything else
    (unset, non-user source) falls back to the setting's documented default.
    """
    if inkdrop_state is None:
        return True
    try:
        setting = inkdrop_state.app_setting(STATE_DB, "quality.allow_raw_image_page_folders")
    except Exception:
        return True
    if not setting or setting.get("source") != "user":
        return True
    return bool(setting.get("value", True))


def enumerate_page_directories(root):
    """Directories holding only loose, sequential page images -- no archive,
    no subfolders. Mirrors enumerate_files' shape so the checkpoint table
    can track a directory the same way it tracks a real file."""
    directories = []
    incomplete_root = incomplete_root_for(root)
    for dirpath, dirnames, filenames in os.walk(root):
        prune_incomplete_root(dirpath, dirnames, incomplete_root)
        if not filenames:
            continue
        try:
            inspection = inkdrop_archive_conversion.inspect_page_directory(dirpath)
        except inkdrop_archive_conversion.ConversionRefused:
            continue
        if inspection["nested_archives"] or inspection["subdirectories"]:
            continue
        if inspection["image_count"] < inkdrop_archive_conversion.MIN_PAGE_DIRECTORY_IMAGES:
            continue
        try:
            st = os.stat(dirpath)
        except OSError:
            continue
        # image_count stands in for a file's size in the (path, size, mtime)
        # checkpoint key -- it changes if pages are added/removed between
        # runs, which is exactly when a directory should be looked at again.
        directories.append((dirpath, inspection["image_count"], st.st_mtime))
    return directories


def process_one_page_directory(path):
    try:
        result = inkdrop_archive_conversion.convert_page_directory(path, dry_run=DRY_RUN)
    except Exception as exc:
        return {"decision": "error", "reason": f"{type(exc).__name__}: {exc}", "dest": None}
    if result.get("converted") or (DRY_RUN and result.get("ok")):
        return {"decision": "converted", "reason": None, "dest": result.get("dest")}
    return {"decision": "skipped", "reason": result.get("reason") or "not_convertible", "dest": result.get("dest")}


def checkpoint_outlived_its_decision(decision, reason, contract):
    return decision == "skipped" and reason in CHECKPOINT_REJUDGED_REASONS and contract != CHECKPOINT_CONTRACT


def load_checkpoints(con):
    rows = con.execute(
        "select path, size, mtime, checked_at, decision, reason, contract from slskd_staging_scan_checkpoint"
    ).fetchall()
    return {(r[0], r[1], r[2]): r[3] for r in rows if not checkpoint_outlived_its_decision(r[4], r[5], r[6])}


def load_priority_paths():
    """Real file paths SLSKD's own matching already confirmed as a staged,
    ready-to-import candidate for some wanted item -- these are worth trying
    before spending budget on the rest of the (mostly unmatched) backlog.
    Returns (exact_paths, priority_filenames): exact_paths are matched
    directly against enumerate_files() output; priority_filenames are
    matched by basename, for the case below where only a filename (not a
    full local path) was ever recorded."""
    try:
        con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=10)
        con.execute("pragma busy_timeout = 10000")
    except sqlite3.OperationalError:
        return set(), set()
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
        # recover_completed_slskd_candidate_task() already ran the real
        # identity/collision checks (exact unit binding, locator digest,
        # filename-safety gate) before marking a task
        # transfer_succeeded_missing_stage/import_ready -- this is the exact
        # same "confirmed real, just needs the file found" signal as
        # source_attempts.staged_file_ready above, on a different table.
        # Missing it here was the actual reason a real, present, fully
        # downloaded file (confirmed live: Frieren - Beyond Journey's End
        # v08.cbz, on disk complete since the transfer finished) never got
        # picked up -- it fell into the ~1,350-file unprioritized backlog
        # and lost the random shuffle for 7 days straight. Only a full local
        # path is stored for source_attempts above; download_tasks only ever
        # has the bare filename (SLSKD's own "directory" field is the
        # *remote* peer's path, not a local one), so this has to match by
        # basename instead of exact path.
        missing_stage_cutoff = time.time() - MISSING_STAGE_PRIORITY_LOOKBACK_SECONDS
        task_rows = con.execute(
            """
            select local_path, raw_json from download_tasks
            where lower(coalesce(download_client,'')) = 'slskd'
              and status = 'transfer_succeeded_missing_stage'
              and state = 'import_ready'
              and coalesce(completed_at, started_at, 0) > ?
            """,
            (missing_stage_cutoff,),
        ).fetchall()
    except sqlite3.OperationalError:
        return set(), set()
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

    filenames = set()
    for local_path, raw_json in task_rows:
        try:
            raw = json.loads(raw_json or "{}")
        except (ValueError, TypeError):
            raw = {}
        raw = raw if isinstance(raw, dict) else {}
        name = local_path or raw.get("filename") or raw.get("source_path") or raw.get("title")
        if isinstance(name, str) and os.path.basename(name).strip():
            filenames.add(os.path.basename(name))
    return paths, filenames


SLSKD_RANGE_ARCHIVE_PENDING_PACK_SOURCE = "slskd_range_archive_handoff"


def pending_range_archive_claims(basename):
    """True when a pending range-archive pack names this file.

    Such an archive holds several issues and is split by the pack import loop
    against the series' missing map; handing it to the single-file child
    would only produce a refusal (a multi-issue archive is never one issue's
    artifact) and a checkpoint that hides the file from the pack import.
    """
    try:
        from core import inkdrop_pack_import
    except Exception:
        return False
    wanted = re.sub(r"[^a-z0-9]+", " ", str(basename or "").lower()).strip()
    if not wanted:
        return False
    try:
        records = inkdrop_pack_import.pending_pack_records()
    except Exception:
        return False
    for record in records or []:
        if not isinstance(record, dict) or str(record.get("source") or "") != SLSKD_RANGE_ARCHIVE_PENDING_PACK_SOURCE:
            continue
        pack_info = record.get("pack_info") if isinstance(record.get("pack_info"), dict) else {}
        title = str(pack_info.get("local_basename") or record.get("title") or "")
        if re.sub(r"[^a-z0-9]+", " ", title.lower()).strip() == wanted:
            return True
    return False


def process_one_file(path):
    if pending_range_archive_claims(os.path.basename(path)):
        return {"decision": "skipped", "reason": "claimed_by_pending_range_archive_pack", "dest": None}
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
    # --source-issue-hint, not --trusted-issue. This number is parsed off the
    # staged basename above; nothing here has consulted a queue or issue row.
    # Sent as --trusted-issue it reached trusted_issue_mismatch_reason(), which
    # compares it against inkdrop_completed_import's own parse of the same
    # string -- so the only disagreement it could ever report was InkDrop with
    # itself, on a Manual Review card that told the operator the issue was "not
    # recorded on this row". Under the honest name it still names the
    # destination and still earns filename-confidence evidence; it just cannot
    # veto an import.
    issue_number = extract_issue_number(os.path.basename(path))
    if issue_number:
        cmd.extend(["--source-issue-hint", issue_number])
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


# What process_one_file() can hand back without the child having judged the
# file under any acceptance rule. A run counts judgements, not calls to the
# child. A staged comic that matches no monitored series, or is younger than
# the child's minimum age, comes back with no decision; a timeout or a crashed
# child decides nothing; and the child's known-bad content memory and its
# qBittorrent completion waits skip a file before import_files() matches it to
# a target, so no acceptance rule sees it. Counted as judgements, any of these
# let a run that re-judged nothing read as all_candidates_processed while the
# checkpoint window held the rest back (row #961). lock_busy is never counted
# as processed in the first place.
NOT_A_JUDGEMENT_REASONS = frozenset({
    "no_decision_returned",
    "claimed_by_pending_range_archive_pack",
    "known_bad_artifact_content",
    "source_file_incomplete_qbit_download",
    "qbit_completion_unverifiable",
})


def result_is_a_judgement(result):
    if result["decision"] == "imported":
        return True
    return result["decision"] == "skipped" and result["reason"] not in NOT_A_JUDGEMENT_REASONS


def main():
    started = time.time()
    con = sqlite3.connect(LEDGER_DB, timeout=30)
    con.execute("pragma busy_timeout = 30000")
    ensure_checkpoint_table(con)
    checkpoints = load_checkpoints(con)
    error_counts = load_error_counts(con)

    page_directory_summary = {
        "enabled": raw_image_page_folders_setting_enabled(),
        "eligible_this_run": 0,
        "converted_this_run": 0,
        "skipped_this_run": 0,
        "errors_this_run": 0,
        "skipped_via_checkpoint": 0,
    }
    # checked_at of everything held back by the checkpoint window at either
    # site below. Its newest entry is what turns "N files were skipped"
    # into "nothing held back this run can be re-judged before T" -- the one
    # fact a person verifying an acceptance-rule change needs to exclude those
    # files or wait them out, instead of reading a pass over files the run
    # never showed to acceptance code (row #961).
    held_checked_at = []
    if page_directory_summary["enabled"]:
        page_directory_started = time.time()
        eligible_directories = enumerate_page_directories(SLSKD_ROOT)
        page_directory_summary["eligible_this_run"] = len(eligible_directories)
        random.shuffle(eligible_directories)
        conversions_done = 0
        for path, image_count, mtime in eligible_directories:
            if time.time() - page_directory_started > PAGE_DIRECTORY_TIMEOUT_SECONDS:
                break
            if conversions_done >= MAX_PAGE_DIRECTORY_CONVERSIONS:
                break
            key = (path, image_count, mtime)
            checked_at = checkpoints.get(key)
            if checked_at is not None and (time.time() - checked_at) < CHECKPOINT_MAX_AGE_SECONDS:
                page_directory_summary["skipped_via_checkpoint"] += 1
                held_checked_at.append(checked_at)
                continue
            err_count, last_error_at = error_counts.get(path, (0, None))
            if err_count >= MAX_CONSECUTIVE_ERRORS and last_error_at is not None and (time.time() - last_error_at) < ERROR_COOLDOWN_SECONDS:
                continue

            result = process_one_page_directory(path)
            if result["decision"] == "converted":
                page_directory_summary["converted_this_run"] += 1
                conversions_done += 1
                clear_error_count(con, path)
                # The directory itself no longer exists after a real (non-dry-run)
                # conversion -- moved aside as the CBZ's kept original -- so there
                # is nothing further to checkpoint here. enumerate_files() below
                # picks up the new CBZ like any other archive on this same run.
            elif result["decision"] == "error":
                page_directory_summary["errors_this_run"] += 1
                record_error(con, path, result["reason"])
            else:
                page_directory_summary["skipped_this_run"] += 1
                clear_error_count(con, path)
                con.execute(
                    """
                    insert or replace into slskd_staging_scan_checkpoint
                        (path, size, mtime, decision, reason, dest, checked_at, contract)
                    values (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (path, image_count, mtime, result["decision"], result["reason"], result["dest"], time.time(), CHECKPOINT_CONTRACT),
                )
                con.commit()

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
            held_checked_at.append(checked_at)
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
    priority_paths, priority_filenames = load_priority_paths()
    if priority_paths or priority_filenames:
        def _is_priority(item):
            path = item[0]
            return path in priority_paths or os.path.basename(path) in priority_filenames
        priority_items = [item for item in to_process if _is_priority(item)]
        rest_items = [item for item in to_process if not _is_priority(item)]
        to_process = priority_items + rest_items

    summary = {
        "total_files_on_disk": len(all_files),
        "skipped_via_checkpoint": skipped_via_checkpoint,
        "skipped_via_error_cooldown": skipped_via_error_cooldown,
        "priority_candidates_this_run": len(priority_paths) + len(priority_filenames),
        "candidates_to_process_this_run": len(to_process),
        "processed_this_run": 0,
        "judged_this_run": 0,
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
        if result_is_a_judgement(result):
            summary["judged_this_run"] += 1
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
                (path, size, mtime, decision, reason, dest, checked_at, contract)
            values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (path, size, mtime, result["decision"], result["reason"], result["dest"], time.time(), CHECKPOINT_CONTRACT),
        )
        con.commit()

    rejudgeable_at = (max(held_checked_at) + CHECKPOINT_MAX_AGE_SECONDS) if held_checked_at else None
    summary["checkpoint_window"] = {
        "window_seconds": CHECKPOINT_MAX_AGE_SECONDS,
        "inside_window": len(held_checked_at),
        "all_rejudgeable_at": rejudgeable_at,
        "all_rejudgeable_at_utc": (
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(rejudgeable_at)) if rejudgeable_at is not None else None
        ),
    }
    if summary["stopped_reason"] is None:
        # A run that reached no acceptance code while the window held files
        # back is not a clean pass: every acceptance rule runs in the child
        # process_one_file() starts, so nothing checkpointed was judged against
        # the code now deployed. Measured on the live host 2026-08-31: 2,258
        # of 2,327 checkpoints inside the window, and PR #1078's fix reached
        # none of the five files it was written for until the window lifted.
        # This used to print all_candidates_processed with 0 processed and
        # exit 0. The exit code is deliberately unchanged -- a run that judges
        # nothing while the window holds files back is this job's steady state
        # every 15 minutes, and a non-zero exit would put the scheduler into
        # failure backoff; the reason and the counts above are what a verifier
        # reads. The test is judged_this_run, not processed_this_run: a file
        # the child returns no decision for is never checkpointed and comes
        # back after every error cooldown, so counting calls to the child
        # brought the clean reason back every hour without judging anything.
        # A run that did judge a file keeps all_candidates_processed, and
        # checkpoint_window says how many it held and until when.
        if summary["judged_this_run"] == 0 and held_checked_at:
            summary["stopped_reason"] = "nothing_rejudged_inside_checkpoint_window"
        else:
            summary["stopped_reason"] = "all_candidates_processed"

    summary["page_directories"] = page_directory_summary
    summary["elapsed_seconds"] = round(time.time() - started, 1)
    print(json.dumps(summary, indent=2))
    con.close()


if __name__ == "__main__":
    main()
