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


def classify_deferred_syncs(db_path, *, stale_after=24 * 3600, limit=1000, now=None):
    now = float(now or time.time())
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
        elif future_retry_times: classification = "retry_scheduled"
        elif any("provider" in status or "transfer" in status for status in statuses): classification = "provider_client_wait"
        elif row["reason"] == "series_autopilot_lock_busy": classification = "lock_deferral"
        else: classification = "still_required_eligible"
        fingerprints.add(fingerprint)
        next_attempt_at = min(future_retry_times) if future_retry_times else None
        item.update({
            "classification": classification,
            "age_seconds": age,
            "queue_keys": keys,
            "statuses": statuses,
            "permanently_stale": classification in PERMANENT_CLASSES,
            "eligible_now": classification in ELIGIBLE_CLASSES,
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
        "last_successful_sync": max((row.get("applied_at") or 0 for row in results), default=0) or None,
        "next_attempt": min((row["next_attempt_at"] for row in pending if row.get("next_attempt_at")), default=None),
        "due_now": any(row["due_now"] for row in pending),
        "overdue": any(row["overdue"] for row in pending),
        "overdue_seconds": max((int(row.get("overdue_seconds") or 0) for row in pending), default=0),
        "stale_signal": len(pending) >= 50 or any(row["age_seconds"] >= stale_after for row in pending),
        "rows": pending,
    }


def reconcile_deferred_syncs(db_path, *, batch_size=25, stale_after=24 * 3600, now=None):
    now = float(now or time.time())
    audit = classify_deferred_syncs(db_path, stale_after=stale_after, now=now)
    batch_limit = max(1, min(int(batch_size), 100))
    stale_selected = [row for row in audit["rows"] if row["permanently_stale"]]
    # Eligible snapshots still need the owning worker to replay their payload.
    # Maintenance has no replay callback, so marking one "applied" here would
    # manufacture success and let the later applied-row acknowledger retire it.
    selected = stale_selected[:batch_limit]
    if not selected:
        return {**audit, "dry_run": False, "reconciled": 0, "failed": 0, "stale": int(audit.get("permanently_stale") or 0)}
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
        con.execute("insert into history_events(id,entity_type,entity_id,event_type,source,message,created_at,raw_json,outcome,display_phase) values(?,?,?,?,?,?,?,?,?,?)", (event_id, "state_repair", "deferred_queue_syncs", "deferred_queue_syncs_reconciled", "inkdrop_deferred_sync", f"Reconciled {changed} deferred queue-sync snapshots", now, json.dumps({"ids": [row["id"] for row in selected], "classifications": classifications}, separators=(",", ":"), sort_keys=True), "repaired", "cleanup"))
        con.commit()
    except Exception:
        con.rollback(); raise
    finally:
        con.close()
    return {**audit, "dry_run": False, "reconciled": changed, "failed": 0, "stale": int(audit.get("permanently_stale") or 0), "audit_event_id": event_id, "selected_ids": [row["id"] for row in selected]}
