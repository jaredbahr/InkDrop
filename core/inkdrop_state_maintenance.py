#!/usr/bin/env python3
"""Run bounded InkDrop queue maintenance or reconciliation."""

from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))


import argparse
import json
import sqlite3
import time

from core import inkdrop_runtime_config
from core import inkdrop_state
from core import inkdrop_deferred_sync
from core import inkdrop_credential_evidence_cleanup


PENDING_FOLDER_IMPORT_PROJECTION_CURSOR = "pending_folder_import_projection_cursor"


def project_pending_folder_imports(
    db_path,
    *,
    limit=1,
    timeout_seconds=None,
    busy_timeout_ms=None,
    lock_attempts=2,
    lock_initial_delay=0.25,
):
    """Atomically settle folder-presence imports that are pending verification.

    The broad verifier can take minutes over a production backlog.  This
    projection deliberately bounds how much work it does per scheduled pass
    and uses its own cursor -- keyed on (created_at, id) rather than the
    content-hash id alone, since id sorts randomly with respect to time and a
    plain id cursor can strand older rows behind newer ones indefinitely -- so
    a slow or invalid row cannot starve the rest.  The fresh proof is created
    inside a savepoint by the existing managed-file verifier, which re-runs
    archive, linkage, and unit gates.  If the queue transition refuses that
    proof, every tentative write is rolled back and the original import
    remains pending; if the verifier itself raises, the row is recorded as
    errored and the pass moves on rather than aborting every row behind it.
    """
    bounded_limit = max(1, min(int(limit or 1), 25))

    def _project():
        now = time.time()
        with inkdrop_state.connect(
            db_path,
            timeout_seconds=timeout_seconds,
            busy_timeout_ms=busy_timeout_ms,
        ) as con:
            inkdrop_state.init_schema(con)
            cursor_row = con.execute(
                "select value from schema_meta where key=? limit 1",
                (PENDING_FOLDER_IMPORT_PROJECTION_CURSOR,),
            ).fetchone()
            cursor_raw = str(cursor_row["value"] or "") if cursor_row else ""
            cursor = inkdrop_state.json_loads(cursor_raw, {}) if cursor_raw else {}
            cursor = cursor if isinstance(cursor, dict) else {}
            cursor_created_at = cursor.get("created_at")
            cursor_id = str(cursor.get("id") or "")
            has_cursor = cursor_created_at is not None and cursor_id
            select_sql = """
                select ir.id, ir.queue_id, ir.source_attempt_id, ir.created_at,
                       ir.series_id, ir.issue_id, ir.source_path, ir.dest_path,
                       ir.status, ir.outcome, ir.display_phase,
                       ir.completion_truth, ir.imported_count, ir.folder_imported,
                       ir.library_visibility_required,
                       ir.library_visibility_status,
                       ir.library_visibility_provider, ir.raw_json,
                       q.wanted_id, q.state as queue_state,
                       sa.source as source_attempt_source
                  from import_results ir
                  join queue_items q on q.id=ir.queue_id
                  join wanted_items w on w.id=q.wanted_id
                  left join source_attempts sa on sa.id=ir.source_attempt_id
                 where ir.verified=0
                   and lower(coalesce(ir.status,''))='verification_pending'
                   and coalesce(ir.folder_imported,0)=1
                   and coalesce(ir.library_visibility_required,0)=0
                   and nullif(trim(coalesce(ir.dest_path,'')),'') is not null
                   and q.active=1
                   and lower(coalesce(q.state,'')) in (
                       'queued','searching','source_wait','downloading','importing','failed','blocked'
                   )
                   and lower(coalesce(w.status,'')) in ('wanted','in_progress')
            """
            params = []
            if has_cursor:
                select_sql += " and (ir.created_at, ir.id) > (?, ?)"
                params.extend([cursor_created_at, cursor_id])
            select_sql += " order by ir.created_at, ir.id limit ?"
            params.append(bounded_limit)
            rows = con.execute(select_sql, params).fetchall()
            wrapped = False
            if not rows and has_cursor:
                rows = con.execute(
                    select_sql.replace(" and (ir.created_at, ir.id) > (?, ?)", ""),
                    (bounded_limit,),
                ).fetchall()
                wrapped = True

            result = {
                "ok": True,
                "limit": bounded_limit,
                "examined": 0,
                "settled": 0,
                "missing": 0,
                "strict_rejected": 0,
                "ineligible": 0,
                "errored": 0,
                "errors": [],
                "wrapped": wrapped,
                "cursor": cursor,
                "cursor_advanced": False,
            }
            for row in rows:
                result["examined"] += 1
                result["cursor"] = {"created_at": row["created_at"], "id": str(row["id"] or "")}
                if not inkdrop_state.direct_import_row_is_verification_candidate(row):
                    result["ineligible"] += 1
                    continue
                raw = inkdrop_state.json_loads(row["raw_json"] or "{}", {}) or {}
                raw = raw if isinstance(raw, dict) else {}
                raw_source = str(raw.get("source") or "").strip().lower()
                raw_kind = str(raw.get("kind") or "").strip().lower()
                if not (
                    raw_source == "folder_presence"
                    or raw_kind in {"folder_presence_backfill", "folder_presence_wanted_backfill"}
                ):
                    result["ineligible"] += 1
                    continue
                if (
                    inkdrop_state.import_result_wrong_unit_rejected(row, raw)
                    or inkdrop_state.boolish(raw.get("unsafe_completion_proof_rejected"), False)
                    or inkdrop_state.import_result_has_unsafe_completion_evidence(row, raw)
                ):
                    result["ineligible"] += 1
                    continue
                if not inkdrop_state.import_result_managed_dest_path_exists(con, row["dest_path"]):
                    result["missing"] += 1
                    continue

                con.execute("savepoint pending_folder_import_projection")
                try:
                    proof_id = inkdrop_state.reverify_managed_file_import_proof(
                        con,
                        {
                            "id": row["queue_id"],
                            "series_id": row["series_id"],
                            "issue_id": row["issue_id"],
                        },
                        now,
                    )
                    settled = bool(proof_id) and inkdrop_state.mark_queue_verified_for_import(
                        con, row["queue_id"], row["wanted_id"], now,
                        message="Managed folder import proof re-verified",
                        current_source="folder_presence",
                    )
                    if not settled:
                        con.execute("rollback to pending_folder_import_projection")
                        result["strict_rejected"] += 1
                    else:
                        result["settled"] += 1
                except Exception as exc:
                    con.execute("rollback to pending_folder_import_projection")
                    result["errored"] += 1
                    result["errors"].append({
                        "import_result_id": str(row["id"] or ""),
                        "error": f"{type(exc).__name__}: {exc}",
                    })
                finally:
                    con.execute("release pending_folder_import_projection")

            next_cursor = result["cursor"] if rows else {}
            con.execute(
                """
                insert into schema_meta(key,value) values(?,?)
                on conflict(key) do update set value=excluded.value
                """,
                (PENDING_FOLDER_IMPORT_PROJECTION_CURSOR, inkdrop_state.json_dumps(next_cursor)),
            )
            result["cursor_advanced"] = next_cursor != cursor
            result["cursor"] = next_cursor
            con.commit()
            return result

    return inkdrop_state.with_db_lock_retry(
        _project,
        attempts=max(1, int(lock_attempts or 1)),
        initial_delay=max(0.01, float(lock_initial_delay or 0.01)),
    )


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
            if result.get("ok"):
                result["pending_folder_import_projection"] = project_pending_folder_imports(
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
            result["credential_evidence_cleanup"] = inkdrop_credential_evidence_cleanup.cleanup_persisted_credentials(
                inkdrop_runtime_config.state_db_path(),
                batch_size=100,
            )
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
