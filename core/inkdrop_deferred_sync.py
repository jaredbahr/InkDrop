#!/usr/bin/env python3
"""Classify and retire stale deferred queue-sync snapshots in bounded batches."""

from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter
from pathlib import Path


PERMANENT_CLASSES = {"already_applied", "superseded", "target_row_removed", "malformed_stale_record", "duplicate"}
ELIGIBLE_CLASSES = {"still_required_eligible", "lock_deferral"}

# The owning worker only replays a snapshot while it is inside this window
# (inkdrop_series_autopilot imports this constant so the two windows cannot
# drift apart). Past it, the worker's reader filters the row out entirely, so
# nothing will ever replay it again -- see reclaim_expired_replays().
REPLAY_TTL_SECONDS = 48 * 3600

# A pending row past REPLAY_TTL_SECONDS is unreachable by the worker's replay
# path by construction. It is NOT in PERMANENT_CLASSES: the reconciler must
# replay whatever work it still carries before retiring it, never just ack it.
EXPIRED_REPLAY_CLASS = "replay_window_expired"


def _payload(value):
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _records(payload):
    if not isinstance(payload, dict): return []
    return [row for key in ("processed", "skipped") for row in (payload.get(key) or []) if isinstance(row, dict)]


def _queue_keys(payload):
    return sorted({str(row.get("autopilot_queue_key") or row.get("queue_id") or "").strip() for row in _records(payload) if str(row.get("autopilot_queue_key") or row.get("queue_id") or "").strip()})


def _replay_rows(payload):
    """The native source-attempt records this snapshot is still carrying.

    These are the only part of a deferred payload that represents a lost
    *write* rather than a stale projection: each one is a source_attempts row
    the owning worker could not record because the DB was locked.
    """
    if not isinstance(payload, dict):
        return []
    rows = payload.get("native_attempt_replay")
    if not isinstance(rows, list):
        return []
    return [
        row for row in rows
        if isinstance(row, dict) and str(row.get("queue_id") or "").strip() and isinstance(row.get("attempt"), dict)
    ]


def _record_retry_times(payload):
    times = []
    for record in _records(payload):
        for key in ("next_retry_after", "retry_after", "next_attempt_at"):
            value = record.get(key)
            if value in (None, ""):
                continue
            try:
                times.append(float(value))
            except (TypeError, ValueError):
                continue
    return [value for value in times if value > 0]


def classify_deferred_syncs(db_path, *, stale_after=24 * 3600, limit=1000, now=None, replay_ttl=None):
    now = float(now or time.time())
    replay_ttl = float(REPLAY_TTL_SECONDS if replay_ttl is None else replay_ttl)
    con = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True, timeout=3)
    con.row_factory = sqlite3.Row
    con.execute("pragma query_only=1")
    rows = con.execute("select * from deferred_queue_syncs order by created_at desc limit ?", (max(1, min(int(limit), 5000)),)).fetchall()
    queue_ids = {str(row[0]) for row in con.execute("select id from queue_items")}
    fingerprints = set()
    results = []
    for row in rows:
        item, payload = dict(row), _payload(row["payload_json"])
        keys = _queue_keys(payload)
        statuses = sorted({str(record.get("status") or "").lower() for record in _records(payload) if record.get("status")})
        fingerprint = (row["source"], row["reason"], tuple(keys), tuple(statuses))
        age = max(0, now - float(row["created_at"] or now))
        retry_times = _record_retry_times(payload)
        future_retry_times = [value for value in retry_times if value > now]
        overdue_retry_times = [value for value in retry_times if value <= now]
        if row["status"] in {"acked", "applied"}: classification = "already_applied"
        elif payload is None: classification = "malformed_stale_record" if age >= stale_after else "malformed_pending"
        elif fingerprint in fingerprints: classification = "duplicate"
        elif keys and not any(key in queue_ids for key in keys): classification = "target_row_removed"
        elif age >= stale_after and row["reason"] == "series_autopilot_lock_busy": classification = "superseded"
        # Checked ahead of every "still waiting for something" class below: those
        # all assume the owning worker will come back for this row, and past the
        # replay TTL it provably will not. Without this branch a row whose
        # payload statuses merely mention a provider falls into
        # provider_client_wait -- a class in neither PERMANENT_CLASSES nor
        # ELIGIBLE_CLASSES -- and is then invisible to the replay path (aged
        # out) and to the retire path (not permanent) for the rest of time.
        elif age >= replay_ttl: classification = EXPIRED_REPLAY_CLASS
        elif future_retry_times: classification = "retry_scheduled"
        elif any("provider" in status or "transfer" in status for status in statuses): classification = "provider_client_wait"
        elif row["reason"] == "series_autopilot_lock_busy": classification = "lock_deferral"
        else: classification = "still_required_eligible"
        fingerprints.add(fingerprint)
        next_attempt_at = min(future_retry_times) if future_retry_times else None
        replay_rows = _replay_rows(payload)
        item.update({
            "classification": classification,
            "age_seconds": age,
            "queue_keys": keys,
            "statuses": statuses,
            "permanently_stale": classification in PERMANENT_CLASSES,
            "eligible_now": classification in ELIGIBLE_CLASSES,
            "replay_expired": classification == EXPIRED_REPLAY_CLASS,
            "unreplayed_attempts": len(replay_rows),
            "next_attempt_at": next_attempt_at,
            "due_now": classification in ELIGIBLE_CLASSES or bool(overdue_retry_times),
            "overdue": bool(overdue_retry_times) and not future_retry_times,
            "overdue_seconds": int(now - min(overdue_retry_times)) if overdue_retry_times and not future_retry_times else 0,
        })
        item.pop("payload_json", None)
        results.append(item)
    con.close()
    pending = [row for row in results if row["status"] == "pending"]
    by_reason = Counter(row["classification"] for row in pending)
    return {
        "ok": True,
        "dry_run": True,
        "generated_at": now,
        "count": len(pending),
        "count_by_reason": dict(sorted(by_reason.items())),
        "oldest_age_seconds": max((row["age_seconds"] for row in pending), default=0),
        "eligible_now": sum(row["eligible_now"] for row in pending),
        "permanently_stale": sum(row["permanently_stale"] for row in pending),
        "replay_expired": sum(row["replay_expired"] for row in pending),
        "unreplayed_attempts": sum(row["unreplayed_attempts"] for row in pending),
        # Pending rows this run cannot touch: the owning worker still holds
        # them. Reported so a run that repairs nothing cannot read as "all
        # clear". These are *not* stuck -- every pending row now becomes
        # reclaimable once it crosses REPLAY_TTL_SECONDS, which is the whole
        # point of EXPIRED_REPLAY_CLASS.
        "not_actionable_now": sum(
            1 for row in pending
            if not row["permanently_stale"] and not row["eligible_now"] and not row["replay_expired"]
            and not row.get("next_attempt_at")
        ),
        "last_successful_sync": max((row.get("applied_at") or 0 for row in results), default=0) or None,
        "next_attempt": min((row["next_attempt_at"] for row in pending if row.get("next_attempt_at")), default=None),
        "due_now": any(row["due_now"] for row in pending),
        "overdue": any(row["overdue"] for row in pending),
        "overdue_seconds": max((int(row.get("overdue_seconds") or 0) for row in pending), default=0),
        "stale_signal": len(pending) >= 50 or any(row["age_seconds"] >= stale_after for row in pending),
        "rows": pending,
    }


def _load_payloads(db_path, ids):
    if not ids:
        return {}
    con = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True, timeout=3)
    try:
        con.execute("pragma query_only=1")
        placeholders = ",".join("?" for _ in ids)
        rows = con.execute(
            f"select id, payload_json from deferred_queue_syncs where id in ({placeholders})", list(ids)
        ).fetchall()
    finally:
        con.close()
    return {str(row[0]): _payload(row[1]) for row in rows}


def reclaim_expired_replays(db_path, *, rows, now, record_attempt=None):
    """Replay the lost writes an expired snapshot still carries, then retire it.

    The owning worker will never look at these rows again (see
    REPLAY_TTL_SECONDS), so this is the last chance to land the source_attempts
    records they are holding. Retiring one without replaying it would drop a
    real write and report success for it, so a row whose replay fails stays
    pending and is reported as failed instead of being acked.
    """
    reclaimed, failed, replayed, notes = [], [], 0, []
    if not rows:
        return {"reclaimed": [], "failed": [], "replayed_attempts": 0, "notes": []}
    if record_attempt is None:
        try:
            from core import inkdrop_state
        except Exception as exc:  # pragma: no cover - import guard
            return {
                "reclaimed": [], "failed": [row["id"] for row in rows], "replayed_attempts": 0,
                "notes": [f"inkdrop_state_unavailable:{type(exc).__name__}"],
            }
        record_attempt = inkdrop_state.record_queue_source_attempt
    payloads = _load_payloads(db_path, [row["id"] for row in rows])
    for row in rows:
        pending_rows = _replay_rows(payloads.get(row["id"]))
        landed, row_failed = 0, False
        for entry in pending_rows:
            try:
                result = record_attempt(
                    db_path,
                    str(entry["queue_id"]).strip(),
                    entry["attempt"],
                    attempt_id=entry.get("attempt_id"),
                    started_at=entry.get("started_at"),
                    completed_at=entry.get("completed_at"),
                )
            except Exception as exc:
                notes.append(f"{row['id']}:replay_raised:{type(exc).__name__}")
                row_failed = True
                break
            if isinstance(result, dict) and not result.get("ok"):
                reason = str(result.get("reason") or "")
                # The target row is gone or its series was removed by the user:
                # this write can never land, and holding the snapshot open for
                # it forever helps nobody. Everything else is treated as a real
                # failure so a transient DB lock does not discard the write.
                if reason in {"queue_item_not_found", "series_removed_by_user"}:
                    notes.append(f"{row['id']}:dropped:{reason}")
                    continue
                notes.append(f"{row['id']}:replay_failed:{reason or 'unknown'}")
                row_failed = True
                break
            landed += 1
        if row_failed:
            failed.append(row["id"])
            continue
        replayed += landed
        reclaimed.append(row["id"])
    return {"reclaimed": reclaimed, "failed": failed, "replayed_attempts": replayed, "notes": notes}


def reconcile_deferred_syncs(db_path, *, batch_size=25, stale_after=24 * 3600, now=None, record_attempt=None):
    now = float(now or time.time())
    audit = classify_deferred_syncs(db_path, stale_after=stale_after, now=now)
    batch_limit = max(1, min(int(batch_size), 100))
    stale_selected = [row for row in audit["rows"] if row["permanently_stale"]]
    # Eligible snapshots still need the owning worker to replay their payload.
    # Maintenance has no replay callback for those, so marking one "applied"
    # here would manufacture success and let the later applied-row
    # acknowledger retire it.
    #
    # Expired snapshots are the opposite case: the worker is never coming back
    # for them, so this is the one place their lost writes can still land.
    # reclaim_expired_replays() replays each one and only returns the ids it
    # actually landed, so the ack below still never runs ahead of the write.
    expired_selected = [row for row in audit["rows"] if row["replay_expired"]][: max(0, batch_limit - len(stale_selected))]
    reclaim = reclaim_expired_replays(db_path, rows=expired_selected, now=now, record_attempt=record_attempt)
    reclaimed_ids = set(reclaim["reclaimed"])
    selected = (stale_selected + [row for row in expired_selected if row["id"] in reclaimed_ids])[:batch_limit]
    if not selected:
        return {
            **audit, "dry_run": False, "reconciled": 0, "failed": len(reclaim["failed"]),
            "stale": int(audit.get("permanently_stale") or 0),
            "replayed_attempts": 0, "reclaimed": 0, "reclaim_failed_ids": reclaim["failed"],
            "reclaim_notes": reclaim["notes"],
        }
    con = sqlite3.connect(Path(db_path), timeout=5)
    con.row_factory = sqlite3.Row
    try:
        con.execute("pragma foreign_keys=on")
        con.execute("begin immediate")
        changed = 0
        for row in selected:
            cur = con.execute("update deferred_queue_syncs set status='acked',acked_at=? where id=? and status='pending'", (now, row["id"]))
            changed += max(0, int(cur.rowcount or 0))
        event_id = f"deferred-sync-reconcile:{int(now * 1000)}"
        classifications = dict(Counter(row["classification"] for row in selected))
        replayed = int(reclaim["replayed_attempts"] or 0)
        message = f"Reconciled {changed} deferred queue-sync snapshots"
        if replayed:
            message += f" and replayed {replayed} lost source attempt(s)"
        con.execute("insert into history_events(id,entity_type,entity_id,event_type,source,message,created_at,raw_json,outcome,display_phase) values(?,?,?,?,?,?,?,?,?,?)", (event_id, "state_repair", "deferred_queue_syncs", "deferred_queue_syncs_reconciled", "inkdrop_deferred_sync", message, now, json.dumps({"ids": [row["id"] for row in selected], "classifications": classifications, "replayed_attempts": replayed, "reclaimed_ids": sorted(reclaimed_ids), "reclaim_failed_ids": reclaim["failed"], "reclaim_notes": reclaim["notes"]}, separators=(",", ":"), sort_keys=True), "repaired", "cleanup"))
        con.commit()
    except Exception:
        con.rollback(); raise
    finally:
        con.close()
    return {
        **audit, "dry_run": False, "reconciled": changed, "failed": len(reclaim["failed"]),
        "stale": int(audit.get("permanently_stale") or 0),
        "replayed_attempts": int(reclaim["replayed_attempts"] or 0),
        "reclaimed": len(reclaimed_ids), "reclaim_failed_ids": reclaim["failed"],
        "reclaim_notes": reclaim["notes"],
        "audit_event_id": event_id, "selected_ids": [row["id"] for row in selected],
    }
