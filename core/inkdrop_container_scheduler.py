#!/usr/bin/env python3
"""Container-native recurring worker runner for the Docker Compose runtime."""

from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))


import dataclasses
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from core import inkdrop_process_lifecycle
from core import inkdrop_runtime_config


def _script(name):
    # Every job below used to invoke these by bare filename, relying on the
    # container's cwd (/app) also being the scripts' own directory. The
    # core/ package move relocated the scripts to /app/core without a
    # matching bare-filename lookup, so every job here started failing with
    # "can't open file ...: No such file or directory" (rc=2) the moment
    # that landed -- silent because nothing surfaced these as a release
    # blocker, only as accumulating worker-scheduler-status.json failures.
    return str(_ROOT / "core" / name)


@dataclasses.dataclass(frozen=True)
class ScheduledJob:
    name: str
    interval_seconds: int
    command: tuple[str, ...] = ()
    initial_delay_seconds: int = 0
    timeout_seconds: int = 1800
    env: dict[str, str] = dataclasses.field(default_factory=dict)
    url: str = ""
    critical: bool = True
    # Whether this job runs unless an operator says otherwise. False marks a
    # job SHELVED: the code stays and the schedule does not. See
    # job_enabled() for the per-job override and the reasoning.
    enabled_by_default: bool = True


def bool_env(name: str, default: bool = False) -> bool:
    value = str(os.environ.get(name, "")).strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on", "enabled"}


def job_enabled_env_name(job_name: str) -> str:
    """The environment variable that decides one job, derived from its name."""
    normalized = "".join(ch if ch.isalnum() else "_" for ch in str(job_name)).strip("_").upper()
    return f"INKDROP_SCHEDULER_JOB_{normalized}_ENABLED"


def job_enabled(job: "ScheduledJob") -> bool:
    """Is this one job scheduled?

    Until 2026-08-22 the only gate was process-wide
    INKDROP_CONTAINER_SCHEDULER_ENABLED: all 29 jobs or none. That is why the
    2026-08-02 decision to shelve ebooks was recorded in a ledger and enforced
    nowhere -- there was no mechanism to enforce it with, so both ebook jobs
    kept running successfully in production for three weeks after the feature
    was shelved. Tracker #599: a shelved decision must name the mechanism that
    enforces it, or be marked advisory.

    A job's own `enabled_by_default` is the recorded decision. The environment
    variable overrides it in either direction, so shelving is reversible
    without a code change -- which matters because the point of shelving is
    that the code is kept.
    """
    return bool_env(job_enabled_env_name(job.name), bool(job.enabled_by_default))


def int_env(name: str, default: int) -> int:
    value = str(os.environ.get(name, "")).strip()
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def bounded_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    return max(int(minimum), min(int(maximum), int_env(name, default)))


def safe_nonnegative_int(value, default: int = 0) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return max(0, int(default))


def log(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {message}", flush=True)


def worker_status_path() -> Path:
    state_dir = inkdrop_runtime_config.state_dir()
    return Path(os.environ.get("INKDROP_WORKER_STATUS_FILE") or state_dir / "worker-scheduler-status.json")


def read_previous_status(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def write_status(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp_path, path)


def failure_backoff_seconds(job: ScheduledJob, consecutive_failures: int) -> int:
    base = max(30, int(job.interval_seconds))
    maximum = bounded_int_env("INKDROP_SCHEDULER_FAILURE_BACKOFF_MAX_SECONDS", 3600, 60, 86400)
    exponent = max(0, min(6, int(consecutive_failures or 0) - 1))
    return min(maximum, base * (2**exponent))


def completion_schedule(job: ScheduledJob, returncode: int, consecutive_failures: int) -> tuple[int, int, str]:
    returncode = int(returncode)
    if returncode == 0:
        return 0, max(30, int(job.interval_seconds)), "success"
    if returncode == 75:
        delay = bounded_int_env("INKDROP_SCHEDULER_DEFERRED_RETRY_SECONDS", 120, 30, 1800)
        return 0, delay, "deferred"
    if returncode == 78:
        return 0, max(30, int(job.interval_seconds)), "configuration_needed"
    failures = max(0, int(consecutive_failures or 0)) + 1
    return failures, failure_backoff_seconds(job, failures), "failed"


def post_url(url: str, timeout_seconds: int) -> int:
    request = urllib.request.Request(url, method="POST", data=b"{}")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read(4096)
            if not 200 <= int(response.status) < 300:
                return 1
            try:
                payload = json.loads(body.decode("utf-8", errors="replace"))
            except (ValueError, TypeError):
                payload = {}
            result = payload.get("result") if isinstance(payload, dict) else {}
            provider_status = str((result or {}).get("status") or "").strip().lower() if isinstance(result, dict) else ""
            return 78 if provider_status in {"configuration_needed", "disabled"} else 0
    except urllib.error.HTTPError as exc:
        log(f"url job failed url={url!r} error={exc}")
        return 1
    except (OSError, urllib.error.URLError) as exc:
        log(f"url job deferred url={url!r} error={exc}")
        return 75


def run_job(job: ScheduledJob) -> int:
    if job.url:
        return post_url(job.url, max(1, int(job.timeout_seconds)))
    env = os.environ.copy()
    env.update(job.env)
    try:
        completed = inkdrop_process_lifecycle.run_tracked(
            list(job.command),
            env=env,
            timeout=job.timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        log(f"job timeout name={job.name} timeout_seconds={job.timeout_seconds}")
        return 124
    except OSError as exc:
        log(f"job launch failed name={job.name} error={exc}")
        return 127
    return int(completed.returncode)


def build_jobs() -> list[ScheduledJob]:
    """The jobs the scheduler will actually run.

    Filtered by job_enabled(). A shelved job is absent from here, and
    therefore absent from job_states, from the status file, and from the run
    loop -- one filter rather than a check at each of the three. Callers
    wanting the whole table, shelved entries included, use all_jobs().
    """
    return [job for job in all_jobs() if job_enabled(job)]


def shelved_job_names() -> list[str]:
    """Jobs that exist and are deliberately not scheduled.

    Reported in the status file so the running process states this
    positively. Absence alone would be indistinguishable from a job that was
    deleted, renamed, or never existed -- and the whole of #599 is that a
    recorded decision and a running process disagreed with nobody able to see
    it.
    """
    return sorted(job.name for job in all_jobs() if not job_enabled(job))


def all_jobs() -> list[ScheduledJob]:
    py = os.environ.get("PYTHON_BIN") or sys.executable or "python"
    state_dir = str(inkdrop_runtime_config.state_dir())
    log_dir = Path(os.environ.get("INKDROP_LOG_DIR") or f"{state_dir}/logs")
    lock_dir = str(inkdrop_runtime_config.lock_dir())
    log_dir.mkdir(parents=True, exist_ok=True)
    # Per-candidate cost is dominated by archive validation and folder-presence
    # stat calls against the media roots, so it scales with disk latency rather
    # than row count. Measured against the production library: limit 1 -> 1.2s,
    # limit 5 -> 6.3s, limit 25 -> 158s. The old default of 25 could not finish
    # inside this job's timeout, which is deliberately capped at 60s so the
    # projection never monopolizes a worker slot -- a fresh install started with
    # a job that timed out on every run and never advanced its cursor.
    verified_import_projection_limit = bounded_int_env(
        "INKDROP_SCHEDULER_VERIFIED_IMPORT_PROJECTION_LIMIT",
        5,
        1,
        100,
    )
    verified_import_projection_timeout = bounded_int_env(
        "INKDROP_SCHEDULER_VERIFIED_IMPORT_PROJECTION_TIMEOUT_SECONDS",
        45,
        15,
        60,
    )
    # The projection child waits for the state-database write lock itself, and
    # series-autopilot holds that lock for most of every 900s cycle. Left at the
    # maintenance defaults the child can wait ~47s (4 attempts x 10s connect plus
    # 1+2+4s backoff), so this job's own timeout fires first and SIGKILLs it
    # mid-pass. The cursor is only written after both projection stages finish,
    # so a killed run makes no progress and the next run repeats it -- observed
    # in production as 50 consecutive rc=124 failures and a permanently dead job.
    # Keep the child's worst case (attempts x db timeout + backoff) inside the
    # kill deadline so a contended run exits cleanly and retries on the next tick.
    projection_db_attempts = 2
    projection_db_timeout = max(2.0, round(verified_import_projection_timeout * 0.35, 1))
    projection_lock_delay = 0.5
    # State-retention budget. history_events and source_attempts are append-only
    # and were measured on production growing ~80k rows/day combined. At 24 runs
    # per day this budget removes up to 120k rows/day, so it stays ahead of that
    # inflow and still drains a backlog in a little over a week without ever
    # becoming the heaviest writer on the database.
    state_retention_max_deletes = bounded_int_env(
        "INKDROP_SCHEDULER_STATE_RETENTION_MAX_DELETES",
        5000,
        100,
        100000,
    )
    # Each batch is its own short transaction; 500 id-keyed deletes is a few
    # milliseconds of write-lock hold, and the budget above is spent over ~10 of
    # them so acquisition workers get the lock back between batches.
    state_retention_batch_size = bounded_int_env(
        "INKDROP_SCHEDULER_STATE_RETENTION_BATCH_SIZE",
        500,
        25,
        5000,
    )
    state_retention_timeout = bounded_int_env(
        "INKDROP_SCHEDULER_STATE_RETENTION_TIMEOUT_SECONDS",
        300,
        60,
        900,
    )
    # Deliberately NOT the projection job's timeout*0.35 lock budget. Retention
    # checkpoints after every batch, so a deferral costs no progress -- unlike
    # the projection job, whose cursor only advances at the very end and which
    # therefore has to be willing to wait. Retention is the lowest-priority
    # writer in the system, so it waits briefly and returns 75 (deferred, retried
    # in INKDROP_SCHEDULER_DEFERRED_RETRY_SECONDS) rather than sitting on a
    # connection while series-autopilot holds the write lock. Worst case is
    # 2 x 15s + 0.5s backoff = 30.5s of waiting, which leaves the bulk of the
    # 300s kill deadline for the deletes themselves even if a lock-retry replays
    # the whole pass with a fresh budget.
    state_retention_db_attempts = 2
    state_retention_db_timeout = 15.0
    state_retention_lock_delay = 0.5
    # Page-reclaim budget. Each step hands back up to batch_pages free pages in
    # one short write transaction -- measured ~28ms of write-lock hold at 1000
    # pages (~4MB at the production page_size of 4096), against ~400ms at 16000
    # for no gain in total reclaim throughput. The per-run page cap keeps one
    # pass to roughly 1GB so a large backlog is drained over consecutive hours
    # instead of one long sitting, and the time cap makes the job exit on its
    # own terms rather than being SIGKILLed partway through.
    db_maintenance_batch_pages = bounded_int_env("INKDROP_DB_MAINTENANCE_BATCH_PAGES", 1000, 50, 65536)
    db_maintenance_max_pages = bounded_int_env("INKDROP_DB_MAINTENANCE_MAX_PAGES", 262144, 1000, 4194304)
    db_maintenance_max_seconds = bounded_int_env("INKDROP_DB_MAINTENANCE_MAX_SECONDS", 120, 10, 1800)
    return [
        ScheduledJob(
            "verified-import-projection",
            int_env("INKDROP_SCHEDULER_VERIFIED_IMPORT_PROJECTION_INTERVAL_SECONDS", 30),
            (
                py,
                "-B",
                _script("inkdrop_state_maintenance.py"),
                "--mode",
                "projection",
                "--timeout-seconds",
                str(projection_db_timeout),
                "--busy-timeout-ms",
                str(int(projection_db_timeout * 1000)),
                "--lock-attempts",
                str(projection_db_attempts),
                "--lock-initial-delay",
                str(projection_lock_delay),
                "--projection-limit",
                str(verified_import_projection_limit),
            ),
            initial_delay_seconds=5,
            timeout_seconds=verified_import_projection_timeout,
            critical=False,
        ),
        ScheduledJob(
            "queue-maintenance",
            int_env("INKDROP_SCHEDULER_QUEUE_MAINTENANCE_INTERVAL_SECONDS", 180),
            (py, "-B", _script("inkdrop_state_maintenance.py"), "--mode", "maintenance"),
            initial_delay_seconds=10,
            timeout_seconds=bounded_int_env(
                "INKDROP_SCHEDULER_QUEUE_MAINTENANCE_TIMEOUT_SECONDS",
                180,
                60,
                1800,
            ),
        ),
        ScheduledJob(
            "manual-search-worker",
            int_env("INKDROP_SCHEDULER_MANUAL_SEARCH_INTERVAL_SECONDS", 30),
            (py, "-B", _script("inkdrop_manual_search_worker.py"), "--state-db", f"{state_dir}/inkdrop-state.sqlite3", "--limit", "3"),
            initial_delay_seconds=15,
            timeout_seconds=180,
            critical=False,
        ),
        ScheduledJob(
            "auth-state-cleanup",
            int_env("INKDROP_SCHEDULER_AUTH_CLEANUP_INTERVAL_SECONDS", 21600),
            (py, "-B", _script("inkdrop_auth_cli.py"), "--state-db", f"{state_dir}/inkdrop-state.sqlite3", "cleanup", "--batch-limit", "250"),
            initial_delay_seconds=75,
            timeout_seconds=60,
            critical=False,
        ),
        ScheduledJob(
            "log-retention",
            int_env("INKDROP_SCHEDULER_LOG_RETENTION_INTERVAL_SECONDS", 300),
            (py, "-B", _script("inkdrop_log_retention.py"), "--json"),
            initial_delay_seconds=90,
            timeout_seconds=bounded_int_env(
                "INKDROP_SCHEDULER_LOG_RETENTION_TIMEOUT_SECONDS",
                120,
                30,
                600,
            ),
            critical=False,
        ),
        ScheduledJob(
            "series-status-refresh",
            int_env("INKDROP_SCHEDULER_STATUS_REFRESH_INTERVAL_SECONDS", 120),
            (py, "-B", _script("inkdrop_series_autopilot.py"), "--status-only"),
            initial_delay_seconds=20,
            timeout_seconds=180,
        ),
        ScheduledJob(
            "manual-review-noop-resolve",
            int_env("INKDROP_SCHEDULER_REVIEW_NOOP_INTERVAL_SECONDS", 900),
            (py, "-B", _script("inkdrop_internal_jobs.py"), "manual-review-noop-resolve"),
            initial_delay_seconds=45,
            timeout_seconds=60,
            critical=False,
        ),
        ScheduledJob(
            "notification-dispatch",
            int_env("INKDROP_SCHEDULER_NOTIFICATION_DISPATCH_INTERVAL_SECONDS", 180),
            (py, "-B", _script("inkdrop_internal_jobs.py"), "notification-dispatch"),
            initial_delay_seconds=30,
            # 90s default: the pass is a handful of bounded SQLite queries plus
            # up to six health checks with their own short per-provider timeouts
            # (2.5-8s each), so a normal run finishes in low single-digit
            # seconds. The floor still lets an operator dial it down; the
            # ceiling stops one wedged health check from hanging the whole job.
            timeout_seconds=bounded_int_env(
                "INKDROP_SCHEDULER_NOTIFICATION_DISPATCH_TIMEOUT_SECONDS",
                90,
                30,
                600,
            ),
            critical=False,
        ),
        ScheduledJob(
            "completed-import-comics",
            int_env("INKDROP_SCHEDULER_COMPLETED_IMPORT_COMICS_INTERVAL_SECONDS", 900),
            (
                "/usr/bin/flock",
                "-n",
                "-E",
                "75",
                f"{lock_dir}/inkdrop-comics-import.lock",
                py,
                "-B",
                _script("inkdrop_completed_import.py"),
                "--kind",
                "comics",
                "--pending-only",
                "--all-series",
            ),
            initial_delay_seconds=60,
            timeout_seconds=2700,
        ),
        ScheduledJob(
            "import-ready-worker",
            int_env("INKDROP_SCHEDULER_IMPORT_READY_INTERVAL_SECONDS", 900),
            ("/app/inkdrop-import-ready-worker.sh",),
            initial_delay_seconds=90,
            timeout_seconds=3000,
        ),
        ScheduledJob(
            "slskd-completed-import",
            int_env("INKDROP_SCHEDULER_SLSKD_COMPLETED_IMPORT_INTERVAL_SECONDS", 900),
            (
                "/usr/bin/flock",
                "-n",
                "-E",
                "75",
                # A private, sweep-instance-only lock -- confirmed live tonight that
                # wrapping the whole sweep run (up to ~40 minutes) in the SHARED
                # inkdrop-comics-import.lock blocked every other consumer of that
                # lock for the entire run regardless of which file the sweep
                # happened to be on: completed-import-comics, the web UI's manual
                # source-import action, and the queue runner (inkdrop_web.py
                # with_import_lock / queue_runner_import_cmd) all share that lock.
                # The sweep now takes inkdrop-comics-import.lock itself, internally,
                # scoped to just the one subprocess call per file (see
                # inkdrop_slskd_staging_sweep.py's acquire_comics_import_lock). This
                # outer lock only needs to stop two sweep instances overlapping.
                f"{lock_dir}/inkdrop-slskd-sweep-instance.lock",
                py,
                "-B",
                _script("inkdrop_slskd_staging_sweep.py"),
            ),
            # This runs the checkpointed sweep wrapper, not inkdrop_completed_import.py
            # directly. A plain --all-series --slskd-staging scan has to walk and
            # evaluate the full SLSKD backlog before it can decide what's new, which
            # took ~42 minutes on the real backlog and left nothing to show for a run
            # killed mid-scan by a redeploy. The wrapper checkpoints each file's
            # decision (in imported-files.sqlite3, on the host, so it survives a
            # container recreate) immediately after deciding it, so an interrupted
            # run only loses whatever single file was in flight -- confirmed live:
            # a run killed after checkpointing 9/11 files resumed and only
            # reprocessed the remaining 2, and a third run against a fully
            # checkpointed set completed in 0.1s instead of ~40s. Its own budget/
            # per-file/max-import knobs are read directly from env
            # (INKDROP_SLSKD_SWEEP_*) inside the wrapper; this job's own timeout is
            # sized as a backstop above that budget, not the primary bound.
            initial_delay_seconds=180,
            timeout_seconds=bounded_int_env(
                "INKDROP_SCHEDULER_SLSKD_COMPLETED_IMPORT_TIMEOUT_SECONDS", 2700, 300, 3600
            ),
            critical=False,
        ),
        ScheduledJob(
            "series-autopilot",
            int_env("INKDROP_SCHEDULER_SERIES_AUTOPILOT_INTERVAL_SECONDS", 900),
            ("/app/inkdrop-series-autopilot-cron.sh",),
            initial_delay_seconds=120,
            timeout_seconds=1000,
        ),
        ScheduledJob(
            "manual-comics-inbox",
            int_env("INKDROP_SCHEDULER_MANUAL_COMICS_INTERVAL_SECONDS", 600),
            (py, "-B", _script("inkdrop_completed_import.py"), "--kind", "comics", "--manual-inbox", "--all-series", "--ignore-cutoff", "--min-age-seconds", "30"),
            initial_delay_seconds=150,
            timeout_seconds=2700,
            critical=False,
        ),
        ScheduledJob(
            "manual-ebooks-inbox",
            int_env("INKDROP_SCHEDULER_MANUAL_EBOOKS_INTERVAL_SECONDS", 600),
            (py, "-B", _script("inkdrop_completed_import.py"), "--kind", "ebooks", "--manual-inbox", "--ignore-cutoff", "--min-age-seconds", "30"),
            initial_delay_seconds=210,
            timeout_seconds=1200,
            critical=False,
            # SHELVED 2026-08-02; enforced here 2026-08-22. Ebooks are on the
            # radar, not in development -- the code is kept deliberately, the
            # schedule is not. It ran every 600s with rc=0 for three weeks
            # after the decision, spending worker slots in the same pipeline
            # the acquisition backlog competes for.
            enabled_by_default=False,
        ),
        ScheduledJob(
            "manual-source-autoresolve",
            int_env("INKDROP_SCHEDULER_MANUAL_SOURCE_INTERVAL_SECONDS", 600),
            (
                "/usr/bin/flock",
                "-n",
                "-E",
                "75",
                f"{lock_dir}/inkdrop-manual-source-autoresolve.lock",
                "timeout",
                "30m",
                py,
                "-B",
                _script("inkdrop_manual_source_autoresolve.py"),
                "--live",
                "--include-ready",
                "--max-imports",
                "2",
                "--min-age-seconds",
                "120",
            ),
            initial_delay_seconds=300,
            timeout_seconds=1900,
        ),
        ScheduledJob(
            # The GENERAL source-worker pass. It was called
            # "source-worker-mangadex" while its wrapper ran twelve providers
            # -- prowlarr, rss, mangadex, suwayomi, comicscodes and seven
            # Prowlarr indexers -- and read the general interval knob below,
            # which is what gave it away. An operator reading that name to
            # find out why acquisition stalled was sent to one provider out
            # of twelve. source-worker-suwayomi below is the genuinely
            # manga-scoped job and keeps its provider name.
            "source-worker",
            # 300s, not 1800s. The interval is measured from COMPLETION
            # (completion_schedule), so the real period is runtime + interval:
            # at 1800s with a ~600s pass the lane fired about 27 times a day
            # and funded a median of 9 rows each, which is ~243 rows/day
            # against a ~2,000-row schedulable backlog -- 8 days a sweep
            # before selection is even considered. This is the throughput half
            # of that; the convergence half is the steady lane's sweep key.
            int_env("INKDROP_SCHEDULER_SOURCE_WORKER_INTERVAL_SECONDS", 300),
            ("/app/inkdrop-source-worker-cron.sh",),
            initial_delay_seconds=360,
            timeout_seconds=900,
            critical=False,
        ),
        ScheduledJob(
            "source-worker-suwayomi",
            int_env("INKDROP_SCHEDULER_SUWAYOMI_WORKER_INTERVAL_SECONDS", 1800),
            ("/app/inkdrop-source-worker-suwayomi-cron.sh",),
            initial_delay_seconds=1260,
            timeout_seconds=900,
            critical=False,
        ),
        ScheduledJob(
            "manga-metadata-guard",
            int_env("INKDROP_SCHEDULER_MANGA_METADATA_GUARD_INTERVAL_SECONDS", 1800),
            # --repair is deliberately NOT passed. It gates every destructive
            # path in this guard, and two of them are unsafe as written:
            #
            #  * inspect_and_repair_archives opens the canonical CBZ in ZIP
            #    append mode and writes ComicInfo.xml straight into it -- no
            #    temp build, no fsync/validate, no atomic rename, no preserved
            #    original. A fault injected between the write and
            #    ZipFile.close() leaves the archive unreadable (BadZipFile)
            #    with its hash changed, while the file still exists on disk, so
            #    state keeps treating a dead file as a valid sole copy.
            #  * prune_internal_kavita_rows deletes every childless Chapter and
            #    Volume library-wide rather than only the rows it identified,
            #    on a connection with foreign keys disabled.
            #
            # Without the flag the job still scans and still reports what it
            # would change, which is the useful half. Restore it only alongside
            # a temp-build/fsync/validate/atomic-rename/rollback sequence with
            # fault-injection coverage, and a prune scoped to its own targets.
            #
            # Do NOT reach for INKDROP_MANGA_METADATA_GUARD_MAX_ARCHIVES=0 as a
            # kill switch: 0 means NO LIMIT (see its --max-archives help), so it
            # widens the blast radius from 250 archives to the whole library.
            (py, "-B", _script("inkdrop_manga_metadata_guard.py"), "--status-json", f"{state_dir}/manga-metadata-guard-status.json"),
            initial_delay_seconds=420,
            timeout_seconds=1800,
            env={
                "INKDROP_MANGA_METADATA_GUARD_MAX_ARCHIVES": os.environ.get("INKDROP_MANGA_METADATA_GUARD_MAX_ARCHIVES", "250"),
                # #340 fixed this job's own command but not this env-var
                # injection: it still handed the child a pre-move path,
                # overriding that child's own correct self-relative default.
                "INKDROP_COMPLETED_IMPORT_SCRIPT": os.environ.get("INKDROP_COMPLETED_IMPORT_SCRIPT", _script("inkdrop_completed_import.py")),
            },
            critical=False,
        ),
        ScheduledJob(
            "sab-failed-cleanup",
            int_env("INKDROP_SCHEDULER_SAB_CLEANUP_INTERVAL_SECONDS", 3600),
            (py, "-B", _script("inkdrop_sab_failed_cleanup.py"), "--min-age-hours", "2", "--history-limit", "800", "--max-delete", "100", "--json"),
            initial_delay_seconds=480,
            timeout_seconds=1200,
            critical=False,
        ),
        ScheduledJob(
            "slskd-search-cleanup",
            int_env("INKDROP_SCHEDULER_SLSKD_SEARCH_CLEANUP_INTERVAL_SECONDS", 3600),
            (py, "-B", _script("inkdrop_slskd_search_cleanup.py"), "--json"),
            initial_delay_seconds=540,
            timeout_seconds=300,
            critical=False,
        ),
        ScheduledJob(
            "scheduled-backup",
            # This is a check-in cadence, not the backup interval itself --
            # run_scheduled_backup() only actually creates an archive once the
            # configured interval has elapsed since the last
            # "scheduled"-labeled one (real archive timestamps on disk, no
            # separate state file to lose track of across a container
            # restart). Checking every few hours costs nothing when nothing
            # is due and means a missed window from downtime is caught soon
            # after the container comes back, not a full interval late.
            int_env("INKDROP_SCHEDULER_BACKUP_CHECK_INTERVAL_SECONDS", 14400),
            (
                py,
                "-B",
                _script("inkdrop_backup_restore.py"),
                "scheduled-backup",
                # None of the three policy numbers is passed here on purpose:
                # not the interval, and not either retention limit. All three
                # are operator policy editable from Settings > General > Full
                # backups, and run_scheduled_backup() resolves each of them on
                # every pass (Settings, then INKDROP_BACKUP_INTERVAL_DAYS /
                # INKDROP_BACKUP_RETENTION_DAYS / INKDROP_BACKUP_RETENTION_COUNT,
                # then the shipped defaults of 7 days / 28 days / newest 6).
                # Baking a value into argv here instead would freeze it at
                # container start, so a change made in the UI would not take
                # effect until a restart -- and would override the saved
                # setting when it finally did.
                #
                # --interval-days used to be baked in here, which is exactly
                # that trap: the cadence had no settings row and no field, so
                # nothing surfaced the contradiction. It is resolved per run
                # now, the same way the retention pair below always was.
                #
                # Age alone does not bound disk usage: an archive of the
                # production database is ~2.25GB, so a shorter interval under
                # the same window silently multiplies the footprint. The
                # default count cap of 6 holds that at roughly 14GB regardless
                # of how the interval is configured, while still covering more
                # than one full retention window at the default weekly cadence.
            ),
            initial_delay_seconds=900,
            # The state database is a full-library SQLite file (tens of GB on
            # a real production install) and this backs the whole thing up
            # with sqlite3's own WAL-safe backup API, not a fast raw copy.
            # Generously sized rather than measured yet -- revisit once a
            # real production run has a wall-clock number to size this
            # against, the same discipline the comicvine-scan job's own
            # timeout comment documents.
            timeout_seconds=bounded_int_env("INKDROP_SCHEDULER_BACKUP_TIMEOUT_SECONDS", 3600, 300, 21600),
            critical=False,
        ),
        ScheduledJob(
            "completed-import-ebooks",
            int_env("INKDROP_SCHEDULER_COMPLETED_IMPORT_EBOOKS_INTERVAL_SECONDS", 3600),
            (py, "-B", _script("inkdrop_completed_import.py"), "--kind", "ebooks"),
            initial_delay_seconds=1020,
            timeout_seconds=1800,
            critical=False,
            # SHELVED 2026-08-02; enforced here 2026-08-22. Sibling of
            # manual-ebooks-inbox above; same reasoning.
            enabled_by_default=False,
        ),
        ScheduledJob(
            "state-retention",
            int_env("INKDROP_SCHEDULER_STATE_RETENTION_INTERVAL_SECONDS", 3600),
            (
                py,
                "-B",
                _script("inkdrop_state_maintenance.py"),
                "--mode",
                "retention",
                "--timeout-seconds",
                str(state_retention_db_timeout),
                "--busy-timeout-ms",
                str(int(state_retention_db_timeout * 1000)),
                "--lock-attempts",
                str(state_retention_db_attempts),
                "--lock-initial-delay",
                str(state_retention_lock_delay),
                "--retention-batch-size",
                str(state_retention_batch_size),
                "--retention-max-deletes",
                str(state_retention_max_deletes),
            ),
            # 660s lands in an otherwise empty stagger slot between comicvine-scan
            # (600) and completed-import-ebooks (1020), so retention never joins
            # the startup burst competing for the three concurrency slots.
            initial_delay_seconds=660,
            timeout_seconds=state_retention_timeout,
            critical=False,
        ),
        ScheduledJob(
            "db-maintenance",
            int_env("INKDROP_SCHEDULER_DB_MAINTENANCE_INTERVAL_SECONDS", 3600),
            (
                py,
                "-B",
                _script("inkdrop_db_maintenance.py"),
                "run",
                "--batch-pages",
                str(db_maintenance_batch_pages),
                "--max-pages",
                str(db_maintenance_max_pages),
                "--max-seconds",
                str(db_maintenance_max_seconds),
            ),
            # 780s sits in the gap between state-retention (660) and
            # completed-import-ebooks (1020), so page reclaim never joins the
            # startup burst competing for the three concurrency slots.
            initial_delay_seconds=780,
            # The child stops itself at --max-seconds; this is a backstop above
            # that, not the primary bound. Each reclaim step holds the write
            # lock ~28ms at the default batch, so a kill here can only ever
            # interrupt between steps -- there is no cursor to lose, and the
            # next pass simply continues from the current freelist.
            timeout_seconds=bounded_int_env(
                "INKDROP_SCHEDULER_DB_MAINTENANCE_TIMEOUT_SECONDS",
                db_maintenance_max_seconds + 60,
                60,
                3600,
            ),
            critical=False,
        ),
        ScheduledJob(
            "comicvine-scan",
            int_env("INKDROP_SCHEDULER_COMICVINE_SCAN_INTERVAL_SECONDS", 21600),
            (py, "-B", _script("inkdrop_internal_jobs.py"), "comicvine-scan"),
            initial_delay_seconds=600,
            # Raised 120s -> 300s once already, and it failed 128 more times at
            # 300.01s. Raising it a third time would have been wrong: the scan's
            # own work measures 71s (5s of ComicVine calls for a 12-watch batch,
            # 61s of missing-issue refresh, 4s to enqueue). What overran the
            # timeout was a full sync_state() pass on the end of the summary,
            # 430s of whole-database backfills that no scan asked for -- and it
            # ended in an IntegrityError, so no timeout would ever have let this
            # job report success. That pass is now its own job, below.
            # 300s stands: roughly 3x the ~100s a scan needs including its
            # watch ingest, which is the headroom that absorbs a slow ComicVine
            # rather than a second job's workload.
            timeout_seconds=bounded_int_env(
                "INKDROP_SCHEDULER_COMICVINE_SCAN_TIMEOUT_SECONDS",
                300,
                120,
                1800,
            ),
            critical=False,
        ),
        ScheduledJob(
            "full-state-sync",
            # Hourly, which is what this pass already costs the machine: riding
            # on comicvine-scan it was replayed every 3600s by that job's
            # failure backoff, and its backfills commit as they go, so the work
            # landed even though the job never reported success. Moving it to
            # the scan's own 21600s interval would have cut the history-event
            # backfill's drain from ~120K rows a day to ~20K against an 861K
            # backlog that grows by ~10.8K a day -- a regression bought with a
            # tidier-looking schedule.
            int_env("INKDROP_SCHEDULER_FULL_STATE_SYNC_INTERVAL_SECONDS", 3600),
            (py, "-B", _script("inkdrop_internal_jobs.py"), "full-state-sync"),
            # 1500s is deliberately clear of the 430s a full pass measured on a
            # 45.7GB production database, because that number is the one that
            # grows with the library. It starts at 2400s so it never shares a
            # startup window with comicvine-scan (600s), which reads the same
            # tables -- overlapping them is how the original lock contention
            # that got blamed for the first timeout was manufactured.
            initial_delay_seconds=2400,
            timeout_seconds=bounded_int_env(
                "INKDROP_SCHEDULER_FULL_STATE_SYNC_TIMEOUT_SECONDS",
                1500,
                300,
                3600,
            ),
            critical=False,
        ),
        ScheduledJob(
            "incremental-state-reconciliation",
            int_env("INKDROP_SCHEDULER_FULL_RECONCILIATION_INTERVAL_SECONDS", 21600),
            (py, "-B", _script("inkdrop_state_maintenance.py"), "--mode", "queue"),
            initial_delay_seconds=1800,
            timeout_seconds=bounded_int_env(
                "INKDROP_SCHEDULER_INCREMENTAL_RECONCILIATION_TIMEOUT_SECONDS",
                240,
                60,
                300,
            ),
            critical=False,
        ),
        ScheduledJob(
            "deep-state-integrity-audit",
            int_env("INKDROP_SCHEDULER_DEEP_INTEGRITY_INTERVAL_SECONDS", 86400),
            (py, "-B", _script("inkdrop_state_maintenance.py"), "--mode", "integrity", "--integrity-limit", "100"),
            initial_delay_seconds=3600,
            timeout_seconds=bounded_int_env(
                "INKDROP_SCHEDULER_DEEP_INTEGRITY_TIMEOUT_SECONDS",
                300,
                120,
                300,
            ),
            critical=False,
        ),
        ScheduledJob(
            "library-reconciliation-report",
            int_env("INKDROP_SCHEDULER_LIBRARY_RECONCILIATION_INTERVAL_SECONDS", 86400),
            (
                py,
                "-B",
                _script("inkdrop_library_reconcile.py"),
                "--mode",
                "report",
                # No --json. run_job() does not redirect the child's streams
                # (inkdrop_process_lifecycle.run_tracked is called with no
                # stdout/stderr/capture_output), so the child inherits this
                # process's stdout and anything it prints lands in ordinary
                # container log retention -- five 10MB files, a different
                # audience and a different lifetime from the authenticated
                # cache file. The full report carries series titles, filenames,
                # source and expected library paths and the absolute state-DB
                # path, so it belongs in --cache-file only. The CLI enforces
                # this too: supplying --cache-file makes stdout counts-only, so
                # re-adding --json here cannot re-open it.
                "--cache-file",
                f"{state_dir}/library-reconciliation-last.json",
            ),
            initial_delay_seconds=1200,
            # Read-only: one os.walk + stat pass over both library roots plus a
            # handful of grouped SQL queries, no hashing. Measured well under a
            # minute against ~3,400 files across 295GB; the wide ceiling below is
            # headroom for library growth, not an expectation of taking that long.
            timeout_seconds=bounded_int_env(
                "INKDROP_SCHEDULER_LIBRARY_RECONCILIATION_TIMEOUT_SECONDS",
                600,
                60,
                1800,
            ),
            critical=False,
        ),
    ]


def _job_runner(job: ScheduledJob, completed: queue.Queue) -> None:
    started_at = time.time()
    started = time.monotonic()
    try:
        rc = run_job(job)
    except BaseException as exc:
        log(f"job crashed name={job.name} error_type={type(exc).__name__} error={exc}")
        rc = 125
    completed.put(
        {
            "name": job.name,
            "rc": int(rc),
            "started_at": started_at,
            "completed_at": time.time(),
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
    )


def _restored_job_state(job: ScheduledJob, previous: dict, started_at: float) -> dict:
    previous_jobs = previous.get("jobs") if isinstance(previous.get("jobs"), list) else []
    previous_row = next((row for row in previous_jobs if isinstance(row, dict) and row.get("name") == job.name), {})
    try:
        next_run_at = float(previous_row.get("next_run_at") or 0)
    except (TypeError, ValueError):
        next_run_at = 0
    if next_run_at <= 0:
        next_run_at = started_at + max(0, int(job.initial_delay_seconds))
    return {
        "name": job.name,
        "critical": bool(job.critical),
        "interval_seconds": int(job.interval_seconds),
        "timeout_seconds": int(job.timeout_seconds),
        "next_run_at": next_run_at,
        "last_started_at": previous_row.get("last_started_at"),
        "last_completed_at": previous_row.get("last_completed_at"),
        "last_elapsed_seconds": previous_row.get("last_elapsed_seconds"),
        "last_rc": previous_row.get("last_rc"),
        "last_outcome": previous_row.get("last_outcome"),
        "consecutive_failures": safe_nonnegative_int(previous_row.get("consecutive_failures")),
    }


def scheduler_status_payload(*, started_at, heartbeat_at, job_states, active, max_concurrency, stopping=False, shelved=None):
    rows = []
    now = float(heartbeat_at)
    for name in sorted(job_states):
        row = dict(job_states[name])
        row["active"] = name in active
        row["late_by_seconds"] = 0 if row["active"] else round(max(0.0, now - float(row.get("next_run_at") or now)), 3)
        rows.append(row)
    failures = [row for row in rows if int(row.get("consecutive_failures") or 0) > 0]
    late = [row for row in rows if float(row.get("late_by_seconds") or 0) > 0]
    state = "stopping" if stopping else "degraded" if failures or late else "healthy"
    return {
        "worker_status_schema_version": 1,
        "ok": not stopping,
        "state": state,
        "pid": os.getpid(),
        "started_at": float(started_at),
        "heartbeat_at": float(heartbeat_at),
        "max_concurrency": int(max_concurrency),
        "active_jobs": [dict(active[name]) for name in sorted(active)],
        "jobs": rows,
        # Named, not merely missing. A reader comparing a shelving decision
        # against the running process needs the process to say what it is
        # deliberately not running; an absent row cannot distinguish "shelved"
        # from "deleted", "renamed", or "never existed".
        "shelved_jobs": sorted(shelved or []),
        "failure_count": len(failures),
        "late_job_count": len(late),
    }


def main() -> int:
    inkdrop_runtime_config.normalize_runtime_environment()
    if not bool_env("INKDROP_CONTAINER_SCHEDULER_ENABLED", True):
        log("container scheduler disabled by INKDROP_CONTAINER_SCHEDULER_ENABLED")
        return 0
    stop = False

    def request_stop(_signum, _frame):
        nonlocal stop
        stop = True
        log("shutdown requested")

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    jobs = build_jobs()
    shelved = shelved_job_names()
    jobs_by_name = {job.name: job for job in jobs}
    status_path = worker_status_path()
    previous = read_previous_status(status_path)
    started_at = time.time()
    max_concurrency = bounded_int_env("INKDROP_SCHEDULER_MAX_CONCURRENCY", 3, 1, 8)
    heartbeat_seconds = bounded_int_env("INKDROP_SCHEDULER_HEARTBEAT_SECONDS", 10, 2, 60)
    job_states = {job.name: _restored_job_state(job, previous, started_at) for job in jobs}
    active = {}
    completed = queue.Queue()
    last_heartbeat = 0.0
    log(
        "container scheduler started "
        f"max_concurrency={max_concurrency} status_file={status_path} jobs=" + ",".join(job.name for job in jobs)
        + (" shelved=" + ",".join(shelved) if shelved else "")
    )
    while not stop:
        # The scheduler is PID 1 in the worker container. Wrapper descendants
        # (for example timeout/flock children) can be adopted here after their
        # direct parent exits, so reap only completed, untracked children.
        inkdrop_process_lifecycle.reap_untracked_children()
        now = time.time()
        while True:
            try:
                result = completed.get_nowait()
            except queue.Empty:
                break
            name = result["name"]
            active.pop(name, None)
            row = job_states[name]
            row["last_started_at"] = result["started_at"]
            row["last_completed_at"] = result["completed_at"]
            row["last_elapsed_seconds"] = result["elapsed_seconds"]
            row["last_rc"] = result["rc"]
            failures, delay, outcome = completion_schedule(
                jobs_by_name[name], result["rc"], row.get("consecutive_failures") or 0
            )
            row["consecutive_failures"] = failures
            row["last_outcome"] = outcome
            row["next_run_at"] = time.time() + delay
            log(
                f"job done name={name} rc={result['rc']} elapsed_seconds={result['elapsed_seconds']:.1f} "
                f"outcome={outcome} consecutive_failures={row['consecutive_failures']} next_run_seconds={delay}"
            )

        available = max(0, max_concurrency - len(active))
        due = sorted(
            (
                job
                for job in jobs
                if job.name not in active and float(job_states[job.name].get("next_run_at") or now) <= now
            ),
            key=lambda job: (not job.critical, float(job_states[job.name].get("next_run_at") or now), job.name),
        )
        for job in due[:available]:
            log(f"job start name={job.name}")
            active[job.name] = {
                "name": job.name,
                "critical": bool(job.critical),
                "started_at": time.time(),
                "timeout_seconds": int(job.timeout_seconds),
            }
            thread = threading.Thread(target=_job_runner, args=(job, completed), name=f"inkdrop-{job.name}", daemon=True)
            thread.start()

        now = time.time()
        if now - last_heartbeat >= heartbeat_seconds:
            write_status(
                status_path,
                scheduler_status_payload(
                    shelved=shelved,
                    started_at=started_at,
                    heartbeat_at=now,
                    job_states=job_states,
                    active=active,
                    max_concurrency=max_concurrency,
                ),
            )
            last_heartbeat = now
        time.sleep(1.0)

    inkdrop_process_lifecycle.reap_untracked_children()
    write_status(
        status_path,
        scheduler_status_payload(
            shelved=shelved,
            started_at=started_at,
            heartbeat_at=time.time(),
            job_states=job_states,
            active=active,
            max_concurrency=max_concurrency,
            stopping=True,
        ),
    )
    log("container scheduler stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
