"""Read-only resource gates for bounded missing-library recovery.

The policy never changes queue or transfer state.  It only decides whether a
new recovery handoff may begin; already-owned work remains untouched.
"""

from __future__ import annotations

import datetime as _datetime
import json
import math
import os
import shutil
from pathlib import Path

import inkdrop_state


TRUTHY = {"1", "true", "on", "yes", "enabled"}
FAILURE_STATUSES = {
    "client_unavailable",
    "enqueue_response_ambiguous",
    "failed_download",
    "provider_unavailable",
    "provider_wait",
}


def _enabled(env, name, default="0"):
    return str(env.get(name, default) or default).strip().lower() in TRUTHY


def _nonnegative_int(env, name, default=0):
    try:
        return max(0, int(env.get(name, default) or 0))
    except (TypeError, ValueError):
        return max(0, int(default or 0))


def _control_path(env):
    explicit = str(env.get("INKDROP_MISSING_RECOVERY_CONTROL_FILE") or "").strip()
    if explicit:
        return Path(explicit)
    state_dir = str(env.get("INKDROP_STATE_DIR") or "/state").strip()
    return Path(state_dir) / "missing-recovery-control.json"


def _operator_paused(env):
    path = _control_path(env)
    if not path.exists():
        return False
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024:
            return True
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return True
    return not isinstance(payload, dict) or payload.get("paused") is True


def _in_quiet_hours(value, now):
    text = str(value or "").strip()
    if not text:
        return False
    try:
        start_text, end_text = (part.strip() for part in text.split("-", 1))
        start_hour, start_minute = (int(part) for part in start_text.split(":", 1))
        end_hour, end_minute = (int(part) for part in end_text.split(":", 1))
        start = start_hour * 60 + start_minute
        end = end_hour * 60 + end_minute
        if not (0 <= start < 1440 and 0 <= end < 1440):
            return True
    except (TypeError, ValueError):
        return True
    local = _datetime.datetime.fromtimestamp(float(now)).astimezone()
    minute = local.hour * 60 + local.minute
    if start == end:
        return True
    return start <= minute < end if start < end else minute >= start or minute < end


def _worker_healthy(env, now):
    path_text = str(
        env.get("INKDROP_WORKER_STATUS_FILE")
        or Path(str(env.get("INKDROP_STATE_DIR") or "/state")) / "worker-scheduler-status.json"
    ).strip()
    path = Path(path_text)
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
            return False
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return False
        if type(payload.get("worker_status_schema_version")) is not int or payload.get("worker_status_schema_version") != 1:
            return False
        raw_heartbeat = payload.get("heartbeat_at")
        if isinstance(raw_heartbeat, bool) or not isinstance(raw_heartbeat, (int, float)):
            return False
        heartbeat_at = float(raw_heartbeat)
        if not math.isfinite(heartbeat_at):
            return False
        heartbeat_age = float(now) - heartbeat_at
        max_age = _nonnegative_int(env, "INKDROP_WORKER_HEALTH_MAX_AGE_SECONDS", 90)
        raw_failure_count = payload.get("failure_count")
        raw_late_job_count = payload.get("late_job_count")
        if type(raw_failure_count) is not int or type(raw_late_job_count) is not int:
            return False
        failure_count = raw_failure_count
        late_job_count = raw_late_job_count
    except (OSError, ValueError, TypeError, OverflowError):
        return False
    raw_state = payload.get("state")
    if not isinstance(raw_state, str):
        return False
    state = raw_state.strip().lower()
    if failure_count < 0 or late_job_count < 0:
        return False
    jobs = payload.get("jobs")
    if not isinstance(jobs, list) or any(not isinstance(row, dict) for row in jobs):
        return False
    failed_jobs = []
    late_jobs = []
    job_names = set()
    try:
        for row in jobs:
            if not isinstance(row.get("name"), str) or not row["name"].strip():
                return False
            job_name = row["name"].strip()
            if job_name in job_names:
                return False
            job_names.add(job_name)
            if type(row.get("critical")) is not bool or type(row.get("active")) is not bool:
                return False
            raw_consecutive_failures = row.get("consecutive_failures")
            raw_late_by_seconds = row.get("late_by_seconds")
            if type(raw_consecutive_failures) is not int:
                return False
            if isinstance(raw_late_by_seconds, bool) or not isinstance(raw_late_by_seconds, (int, float)):
                return False
            consecutive_failures = raw_consecutive_failures
            late_by_seconds = float(raw_late_by_seconds)
            if consecutive_failures < 0 or late_by_seconds < 0 or not math.isfinite(late_by_seconds):
                return False
            if consecutive_failures > 0:
                failed_jobs.append(row)
            if late_by_seconds > 0:
                late_jobs.append(row)
    except (TypeError, ValueError, OverflowError):
        return False
    problem_jobs = failed_jobs + [row for row in late_jobs if row not in failed_jobs]
    problem_counts_match = len(failed_jobs) == failure_count and len(late_jobs) == late_job_count
    only_noncritical_problems = bool(problem_jobs) and problem_counts_match and all(
        row.get("critical") is False for row in problem_jobs
    )
    return bool(
        payload.get("ok") is True
        and state in {"healthy", "degraded"}
        and problem_counts_match
        and (state == "healthy" or only_noncritical_problems)
        and (state != "healthy" or (failure_count == 0 and late_job_count == 0 and not problem_jobs))
        and max_age > 0
        and 0 <= heartbeat_age <= max_age
    )


def _recent_handoff_usage(db_path, now):
    hour_after = float(now) - 3600
    day_after = float(now) - 86400
    with inkdrop_state.connect_read(db_path) as con:
        rows = con.execute(
            """
            select sa.id, sa.status, sa.started_at,
                   coalesce(max(case when dt.size_bytes > 0 then dt.size_bytes end), 0) as size_bytes
            from source_attempts sa
            left join download_tasks dt on dt.source_attempt_id=sa.id
            where coalesce(sa.started_at, 0) >= ?
              and lower(coalesce(sa.status, ''))='download_started'
            group by sa.id, sa.status, sa.started_at
            """,
            (day_after,),
        ).fetchall()
        recent = con.execute(
            """
            select lower(coalesce(status, '')) as status
            from source_attempts
            where coalesce(started_at, 0) >= ?
              and lower(coalesce(source_type, ''))='download_client'
            order by coalesce(started_at, 0) desc, id desc
            limit 50
            """,
            (day_after,),
        ).fetchall()
    hour_rows = [row for row in rows if float(row["started_at"] or 0) >= hour_after]
    consecutive_failures = 0
    for row in recent:
        if str(row["status"] or "") not in FAILURE_STATUSES:
            break
        consecutive_failures += 1
    return {
        "handoffs_hour": len(hour_rows),
        "bytes_hour": sum(int(row["size_bytes"] or 0) for row in hour_rows),
        "bytes_day": sum(int(row["size_bytes"] or 0) for row in rows),
        "consecutive_failures": consecutive_failures,
    }


def evaluate_new_handoff(db_path, *, proposed_bytes=0, staging_root=None, now=None, env=None):
    """Return a public, locator-free admission decision for one new handoff."""

    env = os.environ if env is None else env
    now = _datetime.datetime.now().timestamp() if now is None else float(now)
    if not (_enabled(env, "INKDROP_QUEUE_RUNNER_AUTOPILOT_ENABLED") and _enabled(env, "INKDROP_MISSING_RECOVERY_ENABLED")):
        return {"allowed": True, "recovery_active": False, "reason": "recovery_not_active"}

    if _operator_paused(env):
        return {"allowed": False, "recovery_active": True, "reason": "operator_paused"}
    if _in_quiet_hours(env.get("INKDROP_MISSING_RECOVERY_QUIET_HOURS"), now):
        return {"allowed": False, "recovery_active": True, "reason": "quiet_hours"}
    if not _worker_healthy(env, now):
        return {"allowed": False, "recovery_active": True, "reason": "worker_health_degraded"}

    minimum_free = _nonnegative_int(env, "INKDROP_MISSING_RECOVERY_MIN_STAGING_FREE_BYTES")
    root = str(
        staging_root
        or env.get("INKDROP_SOURCE_WORKER_STAGING_ROOT")
        or env.get("INKDROP_DOWNLOAD_STAGING_ROOT")
        or env.get("INKDROP_STAGING_DIR")
        or env.get("INKDROP_STAGING_ROOT")
        or ""
    ).strip()
    if minimum_free:
        if not root:
            return {"allowed": False, "recovery_active": True, "reason": "staging_space_unknown"}
        try:
            free = int(shutil.disk_usage(root).free)
        except OSError:
            return {"allowed": False, "recovery_active": True, "reason": "staging_space_unknown"}
        if free < minimum_free + max(0, int(proposed_bytes or 0)):
            return {"allowed": False, "recovery_active": True, "reason": "staging_space_low", "free_bytes": free}

    try:
        usage = _recent_handoff_usage(db_path, now)
    except Exception:
        return {"allowed": False, "recovery_active": True, "reason": "database_health_degraded"}

    failure_limit = _nonnegative_int(env, "INKDROP_MISSING_RECOVERY_PAUSE_AFTER_FAILURES")
    if failure_limit and usage["consecutive_failures"] >= failure_limit:
        return {"allowed": False, "recovery_active": True, "reason": "repeated_handoff_failures", **usage}

    proposed = max(0, int(proposed_bytes or 0))
    checks = (
        ("INKDROP_MISSING_RECOVERY_MAX_HANDOFFS_PER_HOUR", usage["handoffs_hour"] + 1, "hourly_handoff_limit"),
        ("INKDROP_MISSING_RECOVERY_MAX_BYTES_PER_HOUR", usage["bytes_hour"] + proposed, "hourly_data_cap"),
        ("INKDROP_MISSING_RECOVERY_MAX_BYTES_PER_DAY", usage["bytes_day"] + proposed, "daily_data_cap"),
    )
    for name, projected, reason in checks:
        limit = _nonnegative_int(env, name)
        if limit and projected > limit:
            return {"allowed": False, "recovery_active": True, "reason": reason, **usage}
    return {"allowed": True, "recovery_active": True, "reason": "within_limits", **usage}
