#!/usr/bin/env python3
"""Static audit for InkDrop-owned state schema before state module splits."""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = ROOT / "inkdrop_state.py"

REQUIRED_TABLE_COLUMNS = {
    "series": {"id", "title", "media_type", "metadata_provider", "metadata_id", "library_path", "monitored", "monitor_new", "auto_grab", "raw_json"},
    "issues": {"id", "series_id", "issue_number", "normalized_number", "metadata_provider", "metadata_id", "raw_json"},
    "wanted_items": {"id", "series_id", "issue_id", "reason", "status", "priority", "raw_json"},
    "queue_items": {"id", "wanted_id", "series_id", "issue_id", "state", "current_source", "source_order_json", "retry_after", "outcome", "display_phase", "provider_status_state", "raw_json"},
    "source_attempts": {"id", "queue_id", "wanted_id", "series_id", "issue_id", "source", "provider_id", "protocol", "download_client", "download_url_hash", "candidate_identity", "lifecycle_phase", "outcome", "display_phase", "failure_reason", "retry_eligible", "raw_json"},
    "download_tasks": {"id", "queue_id", "wanted_id", "series_id", "issue_id", "source_attempt_id", "source", "provider_id", "protocol", "download_client", "external_id", "candidate_identity", "lifecycle_phase", "outcome", "display_phase", "failure_reason", "retry_eligible", "local_path", "progress", "raw_json"},
    "import_results": {"id", "queue_id", "source_attempt_id", "series_id", "issue_id", "source_path", "dest_path", "status", "outcome", "display_phase", "completion_truth", "folder_imported", "library_visibility_status", "verified", "raw_json"},
    "media_files": {"id", "path", "normalized_path", "media_type", "series_id", "issue_id", "queue_id", "import_result_id", "status", "active", "completion_truth", "folder_imported", "raw_json"},
    "bad_source_candidates": {"id", "source", "provider", "protocol", "scope_key", "normalized_title", "download_url_hash", "source_path", "reason", "failure_count", "raw_json"},
    "deferred_queue_syncs": {"id", "source", "reason", "status", "created_at", "applied_at", "acked_at", "payload_json"},
    "worker_activity": {"id", "worker_id", "lane", "source", "state", "status_label", "next_action", "heartbeat_at", "expires_at", "raw_json"},
    "queue_claims": {"queue_id", "owner_id", "operation", "claimed_at", "heartbeat_at", "expires_at", "raw_json"},
    "review_exceptions": {"id", "review_id", "origin", "source", "series_id", "issue_id", "state", "reason", "next_action", "actionable", "parked", "active", "raw_json"},
    "history_events": {"id", "entity_type", "entity_id", "series_id", "issue_id", "event_type", "source", "message", "outcome", "display_phase", "created_at", "raw_json"},
    "provider_configs": {"id", "provider_type", "display_name", "enabled", "base_url", "secret_ref", "ownership", "automation_role", "capabilities_json", "settings_json"},
    "app_settings": {"key", "scope", "label", "value_json", "description", "source", "updated_at"},
    "schema_migrations": {"version", "name", "applied_at"},
    "auth_users": {"id", "username", "password_hash", "role", "enabled", "created_at", "updated_at", "last_login_at", "password_changed_at", "raw_json"},
    "auth_sessions": {"id", "user_id", "token_hash", "csrf_hash", "created_at", "last_seen_at", "expires_at", "revoked_at", "revoked_reason", "user_agent", "remote_addr", "raw_json"},
    "api_keys": {"id", "name", "description", "key_hash", "fingerprint", "prefix", "scopes_json", "role", "enabled", "created_at", "updated_at", "last_used_at", "revoked_at", "raw_json"},
    "auth_login_attempts": {"bucket_key", "username_hint", "remote_addr", "failure_count", "window_started_at", "last_failed_at", "locked_until", "updated_at"},
    "auth_recovery_tokens": {"id", "user_id", "token_hash", "created_at", "expires_at", "used_at", "created_by", "raw_json"},
    "auth_audit_events": {"id", "event_type", "outcome", "actor_type", "actor_id", "username_hint", "remote_addr", "user_agent", "created_at", "detail_json"},
    "manga_companion_links": {"id", "comicvine_series_id", "mangadex_series_id", "normalized_title", "status", "created_at", "updated_at", "last_refresh_at", "last_refresh_status", "raw_json"},
    "manga_companion_reconcile_attempts": {"series_id", "status", "attempt_count", "last_attempt_at", "next_retry_at", "raw_json"},
    "manga_companion_jobs": {"id", "seed_provider", "seed_series_id", "counterpart_provider", "counterpart_metadata_id", "comicvine_series_id", "mangadex_series_id", "normalized_title", "candidate_json", "auto_grab", "status", "attempt_count", "next_retry_at", "lease_expires_at", "lease_token", "last_error", "created_at", "updated_at", "raw_json"},
}

REQUIRED_INDEX_NAMES = {
    "idx_queue_active_state_updated",
    "idx_queue_active_provider_status",
    "idx_source_attempts_candidate_identity",
    "idx_source_attempts_provider_id_key",
    "idx_source_attempts_provider_id_recent",
    "idx_source_attempts_lifecycle_phase",
    "idx_source_attempts_outcome_recent",
    "idx_source_attempts_display_phase_recent",
    "idx_download_tasks_candidate_identity",
    "idx_download_tasks_provider_id",
    "idx_import_results_folder_truth_created",
    "idx_media_files_series_issue",
    "idx_bad_source_candidates_scope",
    "idx_deferred_queue_syncs_status_created",
    "idx_worker_activity_worker_updated",
    "idx_queue_claims_expires",
    "idx_history_entity_created",
    "idx_provider_type",
    "idx_auth_users_username",
    "idx_auth_sessions_token_hash",
    "idx_auth_sessions_user_active",
    "idx_api_keys_fingerprint",
    "idx_api_keys_enabled",
    "idx_auth_login_attempts_locked",
    "idx_auth_recovery_token_hash",
    "idx_auth_audit_created",
    "idx_auth_audit_type_created",
    "idx_manga_companion_active_comicvine",
    "idx_manga_companion_active_mangadex",
    "idx_manga_companion_jobs_due",
}

REQUIRED_PUBLIC_STATE_FUNCTIONS = {
    "state_summary",
    "state_sections_from_summary",
    "state_view_summary",
    "state_view",
    "state_dashboard",
    "state_sections_shell",
}

REQUIRED_STATE_SUMMARY_FIELDS = {
    "ok",
    "schema_version",
    "last_sync_at",
    "series",
    "issues",
    "wanted_items",
    "wanted_by_status",
    "queue_items",
    "queue_by_state",
    "active_queue_items",
    "queue_by_active_state",
    "queue_by_display_active_state",
    "queue_work_items",
    "queue_display_work_items",
    "queue_source_wait_items",
    "queue_exception_items",
    "queue_backlog_items",
    "queue_retry_due_items",
    "queue_retry_later_items",
    "queue_retry_unscheduled_items",
    "queue_backlog_by_reason",
    "source_attempts",
    "download_tasks",
    "import_results",
    "history_events",
    "provider_configs",
    "provider_outcomes",
    "provider_health_problem_items",
    "provider_health_by_state",
    "provider_health_attention",
    "standalone_foundation",
}

REQUIRED_STATE_SECTIONS_FIELDS = {
    "dashboard_shell",
    "preview_mode",
    "queue_preview_deferred",
    "history_preview_deferred",
    "sections",
    "section_count",
    "section_links",
    "summary",
}


def _schema_script(source: str) -> str:
    match = re.search(r"def init_schema\(con\):.*?con\.executescript\(\s*\"\"\"(?P<script>.*?)\"\"\"\s*\)", source, flags=re.DOTALL)
    return match.group("script") if match else ""


def _extract_tables(script: str):
    tables = {}
    pattern = re.compile(r"create table if not exists\s+([a-zA-Z0-9_]+)\s*\((.*?)\);", flags=re.IGNORECASE | re.DOTALL)
    for match in pattern.finditer(script):
        table = match.group(1)
        body = match.group(2)
        columns = set()
        for raw_line in body.splitlines():
            line = raw_line.strip().rstrip(",")
            if not line or line.lower().startswith(("foreign key", "primary key", "unique", "constraint")):
                continue
            name_match = re.match(r"([a-zA-Z_][a-zA-Z0-9_]*)\b", line)
            if name_match:
                columns.add(name_match.group(1))
        tables[table] = columns
    return tables


def _extract_index_names(source: str):
    return set(re.findall(r"create\s+(?:unique\s+)?index\s+if\s+not\s+exists\s+([a-zA-Z0-9_]+)", source, flags=re.IGNORECASE))


def _function_text(source: str, function_name: str) -> str:
    marker = f"\ndef {function_name}("
    start = source.find(marker)
    if start < 0:
        if source.startswith(f"def {function_name}("):
            start = 0
        else:
            return ""
    else:
        start += 1
    next_function = source.find("\ndef ", start + len(marker))
    next_class = source.find("\nclass ", start + len(marker))
    candidates = [value for value in (next_function, next_class) if value >= 0]
    end = min(candidates) if candidates else len(source)
    return source[start:end]


def _public_state_contract_findings(source: str):
    findings = []
    functions = {name: _function_text(source, name) for name in REQUIRED_PUBLIC_STATE_FUNCTIONS}
    for name, text in functions.items():
        if not text:
            findings.append({"kind": "missing_public_state_function", "function": name, "message": f"public state helper {name} is missing"})
    summary_text = functions.get("state_summary", "")
    for field in sorted(REQUIRED_STATE_SUMMARY_FIELDS):
        if f'"{field}"' not in summary_text:
            findings.append({"kind": "missing_state_summary_field", "field": field, "message": f"state_summary should expose {field}"})
    sections_text = functions.get("state_sections_shell", "")
    for field in sorted(REQUIRED_STATE_SECTIONS_FIELDS):
        if f'"{field}"' not in sections_text:
            findings.append({"kind": "missing_state_sections_field", "field": field, "message": f"state_sections_shell should expose {field}"})
    view_text = functions.get("state_view", "")
    for needle in ("loaded_count", "total_count", "rows", "focus", "focused"):
        if f'"{needle}"' not in view_text:
            findings.append({"kind": "missing_state_view_field", "field": needle, "message": f"state_view should expose {needle}"})
    combined = "\n".join(functions.values())
    for needle in ("state_sections_from_summary(", "compact_state_view_summary(", "queue_preview_rows(", "recent_history(", "provider_health_rollup(con)", "**provider_health"):
        if needle not in combined:
            findings.append({"kind": "missing_public_state_contract", "needle": needle, "message": f"public state helpers should include {needle}"})
    return findings


def _runtime_schema():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import inkdrop_state

    with tempfile.TemporaryDirectory(prefix="inkdrop-state-schema-audit-") as tmp:
        db_path = Path(tmp) / inkdrop_state.STATE_DB_NAME
        inkdrop_state.ensure_schema(db_path)
        con = sqlite3.connect(db_path)
        try:
            tables = {}
            for row in con.execute("select name from sqlite_master where type='table'"):
                table = row[0]
                tables[table] = {info[1] for info in con.execute(f"pragma table_info({table})")}
            indexes = {row[0] for row in con.execute("select name from sqlite_master where type='index'")}
            schema_version_row = con.execute("select value from schema_meta where key='schema_version'").fetchone()
            schema_version = schema_version_row[0] if schema_version_row else None
            integrity = con.execute("pragma integrity_check").fetchone()[0]
            foreign_key_violations = [tuple(row) for row in con.execute("pragma foreign_key_check")]
        finally:
            con.close()
    return tables, indexes, schema_version, integrity, foreign_key_violations


def _create_minimal_legacy_db(db_path: Path):
    con = sqlite3.connect(db_path)
    try:
        con.executescript(
            """
            create table series (
                id text primary key,
                title text not null,
                sort_title text,
                media_type text not null default 'comic',
                year integer,
                publisher text,
                metadata_provider text,
                metadata_id text,
                kapowarr_id integer,
                source text,
                monitored integer not null default 1,
                monitor_new integer not null default 1,
                auto_grab integer not null default 1,
                created_at real,
                updated_at real,
                raw_json text
            );
            create table queue_items (
                id text primary key,
                wanted_id text,
                series_id text not null,
                issue_id text,
                state text not null,
                current_source text,
                query text,
                last_event text,
                active integer not null default 0,
                created_at real,
                updated_at real,
                source_order_json text,
                recovery_steps_json text,
                raw_json text
            );
            create table source_attempts (
                id text primary key,
                queue_id text,
                wanted_id text,
                series_id text,
                issue_id text,
                source text,
                provider_id text,
                source_type text,
                provider text,
                download_client text,
                lifecycle_phase text,
                status text,
                title text,
                username text,
                score real,
                started_at real,
                completed_at real,
                raw_json text
            );
            create table download_tasks (
                id text primary key,
                queue_id text,
                wanted_id text,
                series_id text,
                issue_id text,
                source_attempt_id text,
                source text,
                provider text,
                download_client text,
                external_id text,
                title text,
                status text,
                state text not null,
                category text,
                save_path text,
                local_path text,
                size_bytes integer,
                progress real,
                started_at real,
                updated_at real,
                completed_at real,
                raw_json text
            );
            create table import_results (
                id text primary key,
                queue_id text,
                series_id text,
                issue_id text,
                source_path text,
                dest_path text,
                status text,
                verified integer not null default 0,
                imported_count integer not null default 0,
                skipped_count integer not null default 0,
                created_at real,
                raw_json text
            );
            create table bad_source_candidates (
                id text primary key,
                source text,
                provider text,
                protocol text,
                series text,
                title text,
                normalized_title text not null,
                download_url_hash text,
                source_path text,
                reason text not null,
                failure_count integer not null default 1,
                first_seen_at real not null,
                last_seen_at real not null,
                raw_json text
            );
            create table history_events (
                id text primary key,
                entity_type text,
                entity_id text,
                series_id text,
                issue_id text,
                event_type text not null,
                source text,
                message text,
                created_at real,
                raw_json text
            );
            create table provider_configs (
                id text primary key,
                provider_type text not null,
                display_name text not null,
                enabled integer not null default 1,
                base_url text,
                secret_ref text,
                settings_json text,
                source text,
                created_at real,
                updated_at real
            );
            create table app_settings (
                key text primary key,
                scope text not null default 'general',
                label text,
                value_json text
            );
            """
        )
        con.execute(
            "insert into series(id, title, media_type, monitored, monitor_new, auto_grab, raw_json) values(?,?,?,?,?,?,?)",
            ("series:legacy", "Legacy Series", "comic", 1, 1, 1, '{"legacy":true}'),
        )
        con.execute(
            "insert into queue_items(id, series_id, state, active, raw_json) values(?,?,?,?,?)",
            ("queue:legacy", "series:legacy", "queued", 1, '{"retry_after":12345,"retry_after_iso":"1970-01-01T03:25:45Z"}'),
        )
        con.execute(
            "insert into source_attempts(id, queue_id, series_id, source, provider_id, source_type, provider, download_client, lifecycle_phase, status, raw_json) values(?,?,?,?,?,?,?,?,?,?,?)",
            ("attempt:legacy", "queue:legacy", "series:legacy", "slskd", "slskd", "source", "slskd", "slskd", "observed", "started", '{"legacy":true}'),
        )
        con.execute(
            "insert into download_tasks(id, queue_id, series_id, source_attempt_id, source, provider, download_client, title, status, state, raw_json) values(?,?,?,?,?,?,?,?,?,?,?)",
            ("task:legacy", "queue:legacy", "series:legacy", "attempt:legacy", "slskd", "slskd", "slskd", "Legacy File", "started", "downloading", '{"legacy":true}'),
        )
        con.execute(
            "insert into import_results(id, queue_id, series_id, source_path, dest_path, status, verified, imported_count, skipped_count, raw_json) values(?,?,?,?,?,?,?,?,?,?)",
            ("import:legacy", "queue:legacy", "series:legacy", "/tmp/source.cbz", "/library/Legacy.cbz", "imported", 1, 1, 0, '{"legacy":true}'),
        )
        con.execute(
            "insert into bad_source_candidates(id, normalized_title, reason, failure_count, first_seen_at, last_seen_at, raw_json) values(?,?,?,?,?,?,?)",
            ("bad:legacy", "legacy pack", "bad_archive", 1, 1.0, 2.0, '{"legacy":true}'),
        )
        con.execute(
            "insert into history_events(id, event_type, source, message, created_at, raw_json) values(?,?,?,?,?,?)",
            ("history:legacy", "legacy_event", "audit", "Legacy event", 1.0, '{"legacy":true}'),
        )
        con.execute(
            "insert into provider_configs(id, provider_type, display_name, enabled, settings_json) values(?,?,?,?,?)",
            ("provider:legacy", "slskd", "SLSKD", 1, "{}"),
        )
        con.execute(
            "insert into app_settings(key, scope, label, value_json) values(?,?,?,?)",
            ("setting:legacy", "general", "Legacy Setting", '{"enabled":true}'),
        )
        con.commit()
    finally:
        con.close()


def _query_plan_details(con: sqlite3.Connection, sql: str, params=()):
    return [str(row[3]) for row in con.execute(f"explain query plan {sql}", params)]


def _query_plan_uses_index(con: sqlite3.Connection, sql: str, index_name: str, params=()):
    details = _query_plan_details(con, sql, params)
    return {
        "index": index_name,
        "ok": any(index_name in detail for detail in details),
        "details": details,
    }


def _migration_fixture():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import inkdrop_state

    with tempfile.TemporaryDirectory(prefix="inkdrop-state-migration-audit-") as tmp:
        db_path = Path(tmp) / inkdrop_state.STATE_DB_NAME
        _create_minimal_legacy_db(db_path)
        inkdrop_state.ensure_schema(db_path)
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        try:
            inkdrop_state.upsert_provider_config(
                con,
                {
                    "id": "comicvine",
                    "provider_type": "metadata",
                    "display_name": "ComicVine",
                    "enabled": False,
                    "base_url": "",
                    "secret_ref": "env:INKDROP_COMICVINE_API_KEY",
                    "settings_group": "metadata",
                    "ownership": "native",
                    "automation_role": "Primary metadata source",
                    "description": "Series and issue lookup for native InkDrop records.",
                    "next_action": "Configure an API key before enabling ComicVine metadata lookup.",
                    "capabilities": ["metadata", "series_search", "issue_lookup"],
                    "applied_by": ["Add Series", "InkDrop native metadata sync"],
                    "settings": {"api_key_env": "INKDROP_COMICVINE_API_KEY"},
                    "source": "runtime",
                },
                1.0,
            )
            inkdrop_state.upsert_app_setting(
                con,
                {
                    "key": "media_management.rename_imported_files",
                    "scope": "media_management",
                    "label": "Rename imported files",
                    "value": True,
                    "description": "Use InkDrop naming rules for completed imports.",
                    "source": "runtime",
                },
                1.0,
            )
            con.commit()
            con.execute(
                """
                insert into source_attempts(
                    id, queue_id, series_id, source, provider_id, source_type, provider,
                    protocol, download_client, download_url_hash, candidate_identity,
                    lifecycle_phase, outcome, display_phase, failure_reason, retry_eligible,
                    status, raw_json
                ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "attempt:candidate",
                    "queue:legacy",
                    "series:legacy",
                    "slskd",
                    "slskd",
                    "source",
                    "slskd",
                    "soulseek",
                    "slskd",
                    "hash:candidate",
                    "slskd:user:file.cbz",
                    "searched",
                    "candidate_available",
                    "source_candidate",
                    None,
                    1,
                    "candidate_available",
                    '{"candidate_identity":"slskd:user:file.cbz"}',
                ),
            )
            con.execute(
                """
                insert into download_tasks(
                    id, queue_id, series_id, source_attempt_id, source, provider_id, provider,
                    protocol, download_client, external_id, candidate_identity, lifecycle_phase,
                    outcome, display_phase, failure_reason, retry_eligible, title, status, state, raw_json
                ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "task:candidate",
                    "queue:legacy",
                    "series:legacy",
                    "attempt:candidate",
                    "slskd",
                    "slskd",
                    "slskd",
                    "soulseek",
                    "slskd",
                    "external:candidate",
                    "slskd:user:file.cbz",
                    "downloading",
                    "in_progress",
                    "downloading",
                    None,
                    1,
                    "Candidate File",
                    "downloading",
                    "downloading",
                    '{"candidate_identity":"slskd:user:file.cbz"}',
                ),
            )
            con.commit()
            tables = {
                row[0]: {info[1] for info in con.execute(f"pragma table_info({row[0]})")}
                for row in con.execute("select name from sqlite_master where type='table'")
            }
            indexes = {row[0] for row in con.execute("select name from sqlite_master where type='index'")}
            preserved = {
                "series": con.execute("select title from series where id='series:legacy'").fetchone()[0],
                "queue": con.execute("select state, retry_after, retry_after_iso from queue_items where id='queue:legacy'").fetchone(),
                "source_attempt": con.execute("select source, provider from source_attempts where id='attempt:legacy'").fetchone(),
                "download_task": con.execute("select state, download_client from download_tasks where id='task:legacy'").fetchone(),
                "import_result": con.execute("select status, verified from import_results where id='import:legacy'").fetchone(),
                "bad_candidate": con.execute("select reason from bad_source_candidates where id='bad:legacy'").fetchone()[0],
                "history": con.execute("select event_type from history_events where id='history:legacy'").fetchone()[0],
                "provider": con.execute("select provider_type from provider_configs where id='provider:legacy'").fetchone()[0],
                "legacy_provider_columns": con.execute(
                    "select settings_group, ownership, automation_role, capabilities_json from provider_configs where id='provider:legacy'"
                ).fetchone(),
                "legacy_app_setting": con.execute(
                    "select scope, label, value_json, description, source from app_settings where key='setting:legacy'"
                ).fetchone(),
                "public_provider": con.execute(
                    """
                    select base_url, secret_ref, settings_group, ownership, automation_role, capabilities_json, settings_json, source
                    from provider_configs
                    where id='comicvine'
                    """
                ).fetchone(),
                "public_app_setting": con.execute(
                    """
                    select scope, label, value_json, description, source
                    from app_settings
                    where key='media_management.rename_imported_files'
                    """
                ).fetchone(),
                "candidate_attempt": con.execute(
                    """
                    select id from source_attempts
                    where candidate_identity='slskd:user:file.cbz'
                      and lower(coalesce(provider_id, ''))='slskd'
                      and lower(coalesce(outcome, ''))='candidate_available'
                    """
                ).fetchone()[0],
                "candidate_task": con.execute(
                    """
                    select id from download_tasks
                    where candidate_identity='slskd:user:file.cbz'
                      and provider_id='slskd'
                      and lower(coalesce(display_phase, ''))='downloading'
                    """
                ).fetchone()[0],
            }
            query_plan_checks = {
                "source_attempt_candidate_identity": _query_plan_uses_index(
                    con,
                    "select id from source_attempts where candidate_identity=?",
                    "idx_source_attempts_candidate_identity",
                    ("slskd:user:file.cbz",),
                ),
                "source_attempt_provider_recent": _query_plan_uses_index(
                    con,
                    """
                    select id from source_attempts
                    where lower(coalesce(provider_id, ''))=?
                    order by coalesce(completed_at, started_at, 0) desc, id desc
                    limit 1
                    """,
                    "idx_source_attempts_provider_id_recent",
                    ("slskd",),
                ),
                "source_attempt_outcome_recent": _query_plan_uses_index(
                    con,
                    """
                    select id from source_attempts
                    where lower(coalesce(outcome, ''))=?
                    order by coalesce(completed_at, started_at, 0) desc, id desc
                    limit 1
                    """,
                    "idx_source_attempts_outcome_recent",
                    ("candidate_available",),
                ),
                "source_attempt_display_phase_recent": _query_plan_uses_index(
                    con,
                    """
                    select id from source_attempts
                    where lower(coalesce(display_phase, ''))=?
                    order by coalesce(completed_at, started_at, 0) desc, id desc
                    limit 1
                    """,
                    "idx_source_attempts_display_phase_recent",
                    ("source_candidate",),
                ),
                "download_task_candidate_identity": _query_plan_uses_index(
                    con,
                    "select id from download_tasks where candidate_identity=?",
                    "idx_download_tasks_candidate_identity",
                    ("slskd:user:file.cbz",),
                ),
                "download_task_provider_recent": _query_plan_uses_index(
                    con,
                    """
                    select id from download_tasks
                    where provider_id=?
                    order by updated_at desc
                    limit 1
                    """,
                    "idx_download_tasks_provider_id",
                    ("slskd",),
                ),
            }
        finally:
            con.close()
    return tables, indexes, preserved, query_plan_checks


def _schema_17_upgrade_fixture():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import inkdrop_state

    with tempfile.TemporaryDirectory(prefix="inkdrop-state-schema-17-upgrade-") as tmp:
        db_path = Path(tmp) / inkdrop_state.STATE_DB_NAME
        inkdrop_state.ensure_schema(db_path)
        con = sqlite3.connect(db_path)
        try:
            con.execute("pragma foreign_keys=on")
            con.execute("drop table manga_companion_reconcile_attempts")
            con.execute("drop table manga_companion_links")
            con.execute(
                "insert into schema_meta(key,value) values('schema_version','17') "
                "on conflict(key) do update set value='17'"
            )
            con.execute(
                "insert into series(id,title,media_type,metadata_provider,metadata_id,raw_json) values(?,?,?,?,?,?)",
                ("comicvine:schema17", "Schema 17 Manga", "manga", "comicvine", "schema17", '{"preserve":true}'),
            )
            con.execute(
                "insert into issues(id,series_id,issue_number,normalized_number,metadata_provider,metadata_id,raw_json) values(?,?,?,?,?,?,?)",
                ("comicvine:schema17:issue:1", "comicvine:schema17", "1", "1", "comicvine", "schema17-issue", '{"preserve":true}'),
            )
            con.commit()
            cache_key = inkdrop_state.schema_cache_key(con)
        finally:
            con.close()
        if cache_key:
            inkdrop_state.INIT_SCHEMA_READY_KEYS.discard(cache_key)
        inkdrop_state.ensure_schema(db_path)
        con = sqlite3.connect(db_path)
        try:
            con.execute("pragma foreign_keys=on")
            schema_version = con.execute(
                "select value from schema_meta where key='schema_version'"
            ).fetchone()[0]
            preserved = {
                "series": con.execute(
                    "select title, raw_json from series where id='comicvine:schema17'"
                ).fetchone(),
                "issue": con.execute(
                    "select series_id, issue_number, raw_json from issues where id='comicvine:schema17:issue:1'"
                ).fetchone(),
            }
            tables = {row[0] for row in con.execute("select name from sqlite_master where type='table'")}
            indexes = {row[0] for row in con.execute("select name from sqlite_master where type='index'")}
            integrity = con.execute("pragma integrity_check").fetchone()[0]
            foreign_key_violations = [tuple(row) for row in con.execute("pragma foreign_key_check")]
        finally:
            con.close()
    return {
        "schema_version": schema_version,
        "preserved": preserved,
        "tables": tables,
        "indexes": indexes,
        "integrity": integrity,
        "foreign_key_violations": foreign_key_violations,
    }


def audit():
    findings = []
    if not STATE_FILE.exists():
        return {"ok": False, "findings": [{"kind": "missing_file", "file": str(STATE_FILE), "message": "state module is missing"}]}
    source = STATE_FILE.read_text(encoding="utf-8", errors="replace")
    script = _schema_script(source)
    tables = _extract_tables(script)
    indexes = _extract_index_names(source)
    runtime_tables = {}
    runtime_indexes = set()
    runtime_schema_version = None
    runtime_integrity = None
    runtime_foreign_key_violations = []
    migration_tables = {}
    migration_indexes = set()
    migration_preserved = {}
    migration_query_plan_checks = {}
    schema_17_upgrade = {}
    try:
        runtime_tables, runtime_indexes, runtime_schema_version, runtime_integrity, runtime_foreign_key_violations = _runtime_schema()
    except Exception as exc:
        findings.append({"kind": "runtime_schema_error", "message": f"ensure_schema failed against a temp DB: {exc}"})
    try:
        migration_tables, migration_indexes, migration_preserved, migration_query_plan_checks = _migration_fixture()
    except Exception as exc:
        findings.append({"kind": "migration_fixture_error", "message": f"ensure_schema failed against the legacy fixture DB: {exc}"})
    try:
        schema_17_upgrade = _schema_17_upgrade_fixture()
    except Exception as exc:
        findings.append({"kind": "schema_17_upgrade_error", "message": f"ensure_schema failed against the schema-17 fixture DB: {exc}"})

    if 'STATE_DB_NAME = "inkdrop-state.sqlite3"' not in source:
        findings.append({"kind": "missing_state_db_name", "message": "STATE_DB_NAME should remain inkdrop-state.sqlite3"})
    if "SCHEMA_VERSION = 18" not in source:
        findings.append({"kind": "schema_version_changed", "message": "schema audit expected SCHEMA_VERSION = 18; update audit deliberately if schema changes"})
    if "ensure_columns(" not in source:
        findings.append({"kind": "missing_migration_helper", "message": "state schema should keep ensure_columns migration helper"})
    findings.extend(_public_state_contract_findings(source))

    for table, required_columns in sorted(REQUIRED_TABLE_COLUMNS.items()):
        if table not in tables:
            findings.append({"kind": "missing_table", "table": table, "message": f"{table} table is missing"})
        else:
            missing = sorted(required_columns - tables[table])
            if missing:
                findings.append({"kind": "missing_columns", "table": table, "columns": missing, "message": f"{table} missing columns: {', '.join(missing)}"})
        if runtime_tables:
            if table not in runtime_tables:
                findings.append({"kind": "runtime_missing_table", "table": table, "message": f"{table} table was not created by ensure_schema"})
            else:
                runtime_missing = sorted(required_columns - runtime_tables[table])
                if runtime_missing:
                    findings.append({"kind": "runtime_missing_columns", "table": table, "columns": runtime_missing, "message": f"{table} runtime schema missing columns: {', '.join(runtime_missing)}"})
        if migration_tables and table in migration_tables:
            migration_missing = sorted(required_columns - migration_tables[table])
            if migration_missing:
                findings.append({"kind": "migration_missing_columns", "table": table, "columns": migration_missing, "message": f"{table} migrated schema missing columns: {', '.join(migration_missing)}"})

    missing_indexes = sorted(REQUIRED_INDEX_NAMES - indexes)
    for index_name in missing_indexes:
        findings.append({"kind": "missing_index", "index": index_name, "message": f"required index {index_name} is missing"})
    if runtime_indexes:
        runtime_missing_indexes = sorted(REQUIRED_INDEX_NAMES - runtime_indexes)
        for index_name in runtime_missing_indexes:
            findings.append({"kind": "runtime_missing_index", "index": index_name, "message": f"required index {index_name} was not created by ensure_schema"})
    if migration_indexes:
        migration_missing_indexes = sorted(REQUIRED_INDEX_NAMES - migration_indexes)
        for index_name in migration_missing_indexes:
            findings.append({"kind": "migration_missing_index", "index": index_name, "message": f"required index {index_name} was not created during legacy fixture migration"})
    if runtime_schema_version is not None and str(runtime_schema_version) != "18":
        findings.append({"kind": "runtime_schema_version_mismatch", "message": f"temp DB schema_version is {runtime_schema_version!r}, expected '18'"})
    if runtime_integrity is not None and runtime_integrity != "ok":
        findings.append({"kind": "runtime_integrity_failed", "message": f"temp DB integrity_check returned {runtime_integrity!r}"})
    if runtime_foreign_key_violations:
        findings.append({"kind": "runtime_foreign_key_violation", "rows": runtime_foreign_key_violations, "message": "temp DB has foreign-key violations"})
    if schema_17_upgrade:
        if str(schema_17_upgrade.get("schema_version")) != "18":
            findings.append({"kind": "schema_17_upgrade_version_mismatch", "message": f"schema-17 fixture upgraded to {schema_17_upgrade.get('schema_version')!r}, expected '18'"})
        if tuple(schema_17_upgrade.get("preserved", {}).get("series") or ()) != ("Schema 17 Manga", '{"preserve":true}'):
            findings.append({"kind": "schema_17_upgrade_data_loss", "key": "series", "message": "schema-17 series row was not preserved"})
        if tuple(schema_17_upgrade.get("preserved", {}).get("issue") or ()) != ("comicvine:schema17", "1", '{"preserve":true}'):
            findings.append({"kind": "schema_17_upgrade_data_loss", "key": "issue", "message": "schema-17 issue row was not preserved"})
        for table in ("manga_companion_links", "manga_companion_reconcile_attempts"):
            if table not in schema_17_upgrade.get("tables", set()):
                findings.append({"kind": "schema_17_upgrade_missing_table", "table": table, "message": f"schema-17 upgrade did not create {table}"})
        for index_name in ("idx_manga_companion_active_comicvine", "idx_manga_companion_active_mangadex"):
            if index_name not in schema_17_upgrade.get("indexes", set()):
                findings.append({"kind": "schema_17_upgrade_missing_index", "index": index_name, "message": f"schema-17 upgrade did not create {index_name}"})
        if schema_17_upgrade.get("integrity") != "ok":
            findings.append({"kind": "schema_17_upgrade_integrity_failed", "message": f"schema-17 upgraded DB integrity_check returned {schema_17_upgrade.get('integrity')!r}"})
        if schema_17_upgrade.get("foreign_key_violations"):
            findings.append({"kind": "schema_17_upgrade_foreign_key_violation", "rows": schema_17_upgrade["foreign_key_violations"], "message": "schema-17 upgraded DB has foreign-key violations"})
    expected_preserved = {
        "series": "Legacy Series",
        "bad_candidate": "bad_archive",
        "history": "legacy_event",
        "provider": "slskd",
        "candidate_attempt": "attempt:candidate",
        "candidate_task": "task:candidate",
    }
    for key, expected in expected_preserved.items():
        if migration_preserved and migration_preserved.get(key) != expected:
            findings.append({"kind": "migration_data_loss", "key": key, "message": f"legacy fixture value {key} was {migration_preserved.get(key)!r}, expected {expected!r}"})
    if migration_preserved:
        queue_state, retry_after, retry_after_iso = migration_preserved.get("queue") or (None, None, None)
        if queue_state != "queued" or int(retry_after or 0) != 12345 or retry_after_iso != "1970-01-01T03:25:45Z":
            findings.append({"kind": "migration_queue_backfill_failed", "message": f"legacy queue row was not preserved/backfilled correctly: {migration_preserved.get('queue')!r}"})
        if tuple(migration_preserved.get("source_attempt") or ()) != ("slskd", "slskd"):
            findings.append({"kind": "migration_data_loss", "key": "source_attempt", "message": f"legacy source attempt row changed: {migration_preserved.get('source_attempt')!r}"})
        if tuple(migration_preserved.get("download_task") or ()) != ("downloading", "slskd"):
            findings.append({"kind": "migration_data_loss", "key": "download_task", "message": f"legacy download task row changed: {migration_preserved.get('download_task')!r}"})
        if tuple(migration_preserved.get("import_result") or ()) != ("imported", 1):
            findings.append({"kind": "migration_data_loss", "key": "import_result", "message": f"legacy import result row changed: {migration_preserved.get('import_result')!r}"})
        legacy_app_setting = tuple(migration_preserved.get("legacy_app_setting") or ())
        if legacy_app_setting != ("general", "Legacy Setting", '{"enabled":true}', None, None):
            findings.append({"kind": "migration_app_setting_backfill_failed", "message": f"legacy app setting was not preserved/backfilled safely: {legacy_app_setting!r}"})
        legacy_provider_columns = tuple(migration_preserved.get("legacy_provider_columns") or ())
        if legacy_provider_columns != (None, None, None, None):
            findings.append({"kind": "migration_provider_defaults_changed", "message": f"legacy provider config gained unexpected defaults: {legacy_provider_columns!r}"})
        public_provider = tuple(migration_preserved.get("public_provider") or ())
        if len(public_provider) != 8:
            findings.append({"kind": "public_provider_fixture_missing", "message": f"public provider fixture was not inserted: {public_provider!r}"})
        else:
            base_url, secret_ref, settings_group, ownership, automation_role, capabilities_json, settings_json, source_name = public_provider
            if base_url not in ("", None):
                findings.append({"kind": "public_provider_private_default", "message": f"public provider base_url should default blank, got {base_url!r}"})
            if secret_ref != "env:INKDROP_COMICVINE_API_KEY":
                findings.append({"kind": "public_provider_secret_ref_invalid", "message": f"public provider should store a secret ref, got {secret_ref!r}"})
            if settings_group != "metadata" or ownership != "native" or automation_role != "Primary metadata source" or source_name != "runtime":
                findings.append({"kind": "public_provider_contract_mismatch", "message": f"public provider ownership/role/source mismatch: {public_provider!r}"})
            capabilities = json.loads(capabilities_json or "[]")
            settings = json.loads(settings_json or "{}")
            if "metadata" not in capabilities or settings.get("api_key_env") != "INKDROP_COMICVINE_API_KEY":
                findings.append({"kind": "public_provider_settings_contract_mismatch", "message": f"public provider capabilities/settings mismatch: {public_provider!r}"})
            if "b1d281" in (settings_json or "") or "b1d281" in (secret_ref or ""):
                findings.append({"kind": "public_provider_secret_leak", "message": "public provider fixture stored a literal-looking ComicVine secret"})
        public_app_setting = tuple(migration_preserved.get("public_app_setting") or ())
        if public_app_setting != ("media_management", "Rename imported files", "true", "Use InkDrop naming rules for completed imports.", "runtime"):
            findings.append({"kind": "public_app_setting_contract_mismatch", "message": f"public app setting fixture mismatch: {public_app_setting!r}"})
    for name, plan in sorted(migration_query_plan_checks.items()):
        if not plan.get("ok"):
            findings.append(
                {
                    "kind": "migration_query_plan_missing_index",
                    "query": name,
                    "index": plan.get("index"),
                    "details": plan.get("details") or [],
                    "message": f"migrated diagnostic query {name} did not use {plan.get('index')}",
                }
            )

    return {
        "ok": not findings,
        "schema_version": 18 if "SCHEMA_VERSION = 18" in source else None,
        "runtime_schema_version": runtime_schema_version,
        "runtime_integrity": runtime_integrity,
        "runtime_foreign_key_violations": runtime_foreign_key_violations,
        "schema_17_upgrade": {
            "schema_version": schema_17_upgrade.get("schema_version"),
            "preserved_keys": sorted((schema_17_upgrade.get("preserved") or {}).keys()),
            "integrity": schema_17_upgrade.get("integrity"),
            "foreign_key_violations": schema_17_upgrade.get("foreign_key_violations") or [],
        } if schema_17_upgrade else {},
        "table_count": len(tables),
        "runtime_table_count": len(runtime_tables),
        "migration_table_count": len(migration_tables),
        "index_count": len(indexes),
        "runtime_index_count": len(runtime_indexes),
        "migration_index_count": len(migration_indexes),
        "migration_fixture_preserved_keys": sorted(migration_preserved) if migration_preserved else [],
        "migration_public_provider_contract": {
            "provider": "comicvine",
            "secret_storage": "secret_ref",
            "base_url_default": "blank",
            "app_setting": "media_management.rename_imported_files",
        } if migration_preserved else {},
        "migration_query_plan_checks": migration_query_plan_checks,
        "required_table_count": len(REQUIRED_TABLE_COLUMNS),
        "required_index_count": len(REQUIRED_INDEX_NAMES),
        "required_public_state_function_count": len(REQUIRED_PUBLIC_STATE_FUNCTIONS),
        "required_state_summary_field_count": len(REQUIRED_STATE_SUMMARY_FIELDS),
        "required_state_sections_field_count": len(REQUIRED_STATE_SECTIONS_FIELDS),
        "findings": findings,
    }


def main() -> int:
    payload = audit()
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
