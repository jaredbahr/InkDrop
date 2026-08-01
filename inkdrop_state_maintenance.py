#!/usr/bin/env python3
"""Run bounded InkDrop queue maintenance or reconciliation."""

from __future__ import annotations

import argparse
import json
import sqlite3

import inkdrop_runtime_config
import inkdrop_state
import inkdrop_deferred_sync


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("projection", "integrity", "retention", "maintenance", "queue", "full"), default="maintenance")
    parser.add_argument("--include-summary", action="store_true")
    parser.add_argument("--export-json", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    parser.add_argument("--busy-timeout-ms", type=int, default=10000)
    parser.add_argument("--lock-attempts", type=int, default=4)
    parser.add_argument("--lock-initial-delay", type=float, default=1.0)
    parser.add_argument("--projection-limit", type=int, default=100)
    parser.add_argument("--integrity-limit", type=int, default=500)
    parser.add_argument("--retention-batch-size", type=int, default=None)
    parser.add_argument("--retention-max-deletes", type=int, default=None)
    args = parser.parse_args()

    try:
        if args.mode == "projection":
            result = inkdrop_state.sync_verified_import_projection(
                inkdrop_runtime_config.state_db_path(),
                limit=max(1, int(args.projection_limit)),
                timeout_seconds=max(0.1, float(args.timeout_seconds)),
                busy_timeout_ms=max(100, int(args.busy_timeout_ms)),
                lock_attempts=max(1, int(args.lock_attempts)),
                lock_initial_delay=max(0.1, float(args.lock_initial_delay)),
            )
        elif args.mode == "integrity":
            result = inkdrop_state.sync_completion_integrity(
                inkdrop_runtime_config.state_db_path(),
                limit=max(1, int(args.integrity_limit)),
                timeout_seconds=max(0.1, float(args.timeout_seconds)),
                busy_timeout_ms=max(100, int(args.busy_timeout_ms)),
                lock_attempts=max(1, int(args.lock_attempts)),
                lock_initial_delay=max(0.1, float(args.lock_initial_delay)),
            )
        elif args.mode == "retention":
            # Retention windows themselves come from INKDROP_RETENTION_* so a
            # hand-run pass and the scheduled one agree; only the bounded work
            # budget is exposed on the command line.
            result = inkdrop_state.prune_event_history(
                inkdrop_runtime_config.state_db_path(),
                batch_size=args.retention_batch_size,
                max_deletes=args.retention_max_deletes,
                timeout_seconds=max(0.1, float(args.timeout_seconds)),
                busy_timeout_ms=max(100, int(args.busy_timeout_ms)),
                lock_attempts=max(1, int(args.lock_attempts)),
                lock_initial_delay=max(0.1, float(args.lock_initial_delay)),
            )
        else:
            result = inkdrop_state.sync_queue_state(
                inkdrop_runtime_config.state_dir(),
                inkdrop_runtime_config.state_db_path(),
                mode=args.mode,
                include_summary=args.include_summary,
                export_json=args.export_json,
                export_reason=f"scheduled_{args.mode}",
                timeout_seconds=max(0.1, float(args.timeout_seconds)),
                busy_timeout_ms=max(100, int(args.busy_timeout_ms)),
                lock_attempts=max(1, int(args.lock_attempts)),
                lock_initial_delay=max(0.1, float(args.lock_initial_delay)),
            )
        if result.get("ok") and args.mode in {"maintenance", "full"}:
            # Retire only classifier-proven stale snapshots. This is a separate,
            # bounded transaction after queue synchronization has released its
            # own write lock.
            deferred = inkdrop_deferred_sync.reconcile_deferred_syncs(
                inkdrop_runtime_config.state_db_path(),
                batch_size=25,
            )
            result["deferred_queue_sync_reconciliation"] = {
                key: value for key, value in deferred.items() if key != "rows"
            }
    except sqlite3.OperationalError as exc:
        if not inkdrop_state.is_database_locked_error(exc):
            raise
        print(json.dumps({
            "ok": True,
            "deferred": True,
            "reason": "database_locked",
            "mode": args.mode,
            "detail": "InkDrop state is busy; scheduled maintenance will retry shortly.",
        }, sort_keys=True))
        return 75
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
