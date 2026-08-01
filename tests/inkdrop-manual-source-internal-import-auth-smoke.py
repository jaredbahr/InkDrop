#!/usr/bin/env python3
"""Auth-boundary regression for scheduled manual-source preview/live imports."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import types
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest import mock

import inkdrop_manual_source_autoresolve as resolver


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def smoke_live_manual_import_defers_broad_sync():
    import inkdrop_web

    source_path = Path("/staging/slskd/pending.cbz")
    item = {"review_id": "review:pending", "series": "Pending Series", "issue": "1"}
    detected = {"path": str(source_path), "filename": source_path.name}
    child_result = {
        "count": 1,
        "imported": [{"dest": "/library/comics/Pending Series/Pending Series 001.cbz"}],
        "verification": {
            "checked_count": 1,
            "failure_count": 0,
            "pending_scan_count": 1,
            "failures": [
                {
                    "dest": "/library/comics/Pending Series/Pending Series 001.cbz",
                    "verification_status": "waiting_for_library_scan",
                }
            ],
        },
    }
    child_calls = []
    proof_calls = []

    def fake_run_command(args, timeout=75, env=None):
        child_calls.append({"args": list(args), "timeout": timeout, "env": dict(env or {})})
        return json.dumps(child_result)

    def fake_proof_sync(item_value, detected_value, result_value):
        proof_calls.append((item_value, detected_value, result_value))
        return {"ok": True, "required": True, "recorded": 1, "already_recorded": 0, "deferred": 0}

    with mock.patch.object(
        inkdrop_web,
        "detected_manual_source_file",
        return_value=(item, detected, source_path),
    ), mock.patch.object(
        inkdrop_web,
        "manual_source_archive_uses_pack_import",
        return_value=False,
    ), mock.patch.object(
        inkdrop_web,
        "with_import_lock",
        side_effect=lambda args, wait_seconds=60: list(args),
    ), mock.patch.object(
        inkdrop_web,
        "run_command",
        side_effect=fake_run_command,
    ), mock.patch.object(
        inkdrop_web,
        "sync_manual_source_import_proof",
        side_effect=fake_proof_sync,
    ), mock.patch.object(
        inkdrop_web,
        "mark_manual_source_import_resolved",
        side_effect=AssertionError("pending library visibility must not clear the resolver row"),
    ):
        result = inkdrop_web.import_detected_manual_source(
            {"review_id": item["review_id"], "path": str(source_path), "dryRun": False}
        )
        preview = inkdrop_web.import_detected_manual_source(
            {"review_id": item["review_id"], "path": str(source_path), "dryRun": True}
        )

    require(len(child_calls) == 2, f"expected live and preview completed-import child calls: {child_calls}")
    require(not proof_calls, f"pending or preview imports must not record completion proof: {proof_calls}")
    child = child_calls[0]
    require(
        "--no-wait-for-library-scan" in child["args"],
        f"live manual import did not use asynchronous library visibility: {child['args']}",
    )
    require(
        child["env"].get("INKDROP_COMPLETED_IMPORT_STATUS_SYNC_MODE") == "defer",
        f"live manual import did not defer broad state sync: {child['env']}",
    )
    resolution = result.get("manual_source_resolution_status") or {}
    require(resolution.get("state") == "verification_pending", f"pending visibility state changed: {resolution}")
    require(result.get("manual_source_resolved") is False, f"pending visibility cleared resolver row: {result}")
    preview_child = child_calls[1]
    require("--dry-run" in preview_child["args"], f"manual preview lost dry-run isolation: {preview_child['args']}")
    require(
        "--no-wait-for-library-scan" in preview_child["args"],
        f"manual preview did not retain the bounded child contract: {preview_child['args']}",
    )
    require(
        preview_child["env"].get("INKDROP_COMPLETED_IMPORT_STATUS_SYNC_MODE") == "defer",
        f"manual preview child did not retain the deferred-sync contract: {preview_child['env']}",
    )
    require(preview.get("dry_run") is True, f"manual preview changed to a live import: {preview}")
    require(
        (preview.get("manual_source_resolution_status") or {}).get("state") == "preview_importable",
        f"manual preview importability semantics changed: {preview}",
    )
    require(preview.get("manual_source_resolved") is False, f"manual preview cleared resolver row: {preview}")

    with mock.patch.dict(
        os.environ,
        {
            "INKDROP_PARENT_ENV_SENTINEL": "preserved",
            "INKDROP_COMPLETED_IMPORT_STATUS_SYNC_MODE": "full",
        },
        clear=False,
    ):
        merged_env = inkdrop_web.command_env(inkdrop_web.manual_source_import_child_env())
    require(merged_env.get("INKDROP_PARENT_ENV_SENTINEL") == "preserved", "manual child env dropped parent values")
    require(
        merged_env.get("INKDROP_COMPLETED_IMPORT_STATUS_SYNC_MODE") == "defer",
        f"manual child defer override was not merged into parent env: {merged_env.get('INKDROP_COMPLETED_IMPORT_STATUS_SYNC_MODE')}",
    )


def smoke_live_manual_import_requires_durable_completion_proof():
    import inkdrop_web

    source_path = Path("/staging/slskd/verified.cbz")
    item = {
        "review_id": "review:verified",
        "autopilot_queue_key": "queue:verified",
        "inkdrop_series_id": "comicvine:100",
        "inkdrop_issue_id": "comicvine-issue:101",
        "series": "Verified Series",
        "issue": "1",
    }
    detected = {"path": str(source_path), "filename": source_path.name}
    child_result = {
        "count": 1,
        "imported": [
            {
                "dest": "/library/comics/Verified Series/Verified Series 001.cbz",
                "matched_series": "Verified Series",
                "issue_number": "1",
            }
        ],
        "verification": {
            "checked_count": 1,
            "failure_count": 0,
            "pending_scan_count": 0,
            "folder_verified_count": 1,
        },
    }

    def run_with_proof(proof_result):
        order = []

        def proof_sync(*_args):
            order.append("proof")
            return dict(proof_result)

        def mark_resolved(*_args):
            order.append("resolved")
            return {"autopilot_queue_key": item["autopilot_queue_key"], "destinations": [child_result["imported"][0]["dest"]]}

        with mock.patch.object(
            inkdrop_web,
            "detected_manual_source_file",
            return_value=(item, detected, source_path),
        ), mock.patch.object(
            inkdrop_web,
            "manual_source_archive_uses_pack_import",
            return_value=False,
        ), mock.patch.object(
            inkdrop_web,
            "with_import_lock",
            side_effect=lambda args, wait_seconds=60: list(args),
        ), mock.patch.object(
            inkdrop_web,
            "run_command",
            return_value=json.dumps(child_result),
        ), mock.patch.object(
            inkdrop_web,
            "manual_source_import_verified",
            return_value=(True, "verified import"),
        ), mock.patch.object(
            inkdrop_web,
            "manual_source_already_present",
            return_value=(False, ""),
        ), mock.patch.object(
            inkdrop_web,
            "sync_manual_source_import_proof",
            side_effect=proof_sync,
        ), mock.patch.object(
            inkdrop_web,
            "mark_manual_source_import_resolved",
            side_effect=mark_resolved,
        ):
            result = inkdrop_web.import_detected_manual_source(
                {"review_id": item["review_id"], "path": str(source_path), "dryRun": False}
            )
        return result, order

    success, success_order = run_with_proof(
        {"ok": True, "required": True, "recorded": 1, "already_recorded": 0, "deferred": 0}
    )
    require(success_order == ["proof", "resolved"], f"manual source resolved before durable proof: {success_order}")
    require(success.get("manual_source_resolved") is True, f"proved import did not resolve: {success}")

    failed, failed_order = run_with_proof(
        {"ok": False, "required": True, "recorded": 0, "already_recorded": 0, "deferred": 1}
    )
    require(failed_order == ["proof"], f"unproved import was marked resolved: {failed_order}")
    require(failed.get("manual_source_resolved") is False, f"unproved import cleared the row: {failed}")
    require(
        (failed.get("manual_source_resolution_status") or {}).get("state") == "verification_pending",
        f"unproved import did not remain pending: {failed}",
    )


def smoke_live_manual_import_requires_exact_artifact_inspection():
    import inkdrop_web

    source_path = Path("/staging/slskd/uninspected.cbz")
    item = {
        "review_id": "review:uninspected",
        "autopilot_queue_key": "queue:uninspected",
        "series": "Uninspected Series",
        "issue": "1",
    }
    detected = {"path": str(source_path), "filename": source_path.name}
    child_result = {
        "count": 1,
        "imported": [{"dest": "/library/comics/Uninspected Series/Uninspected Series 001.cbz"}],
        "verification": {"checked_count": 1, "failure_count": 0, "pending_scan_count": 0},
    }
    with mock.patch.object(
        inkdrop_web,
        "detected_manual_source_file",
        return_value=(item, detected, source_path),
    ), mock.patch.object(
        inkdrop_web,
        "manual_source_archive_uses_pack_import",
        return_value=False,
    ), mock.patch.object(
        inkdrop_web,
        "with_import_lock",
        side_effect=lambda args, wait_seconds=60: list(args),
    ), mock.patch.object(
        inkdrop_web,
        "run_command",
        return_value=json.dumps(child_result),
    ), mock.patch.object(
        inkdrop_web,
        "manual_source_import_verified",
        return_value=(True, "verified import"),
    ), mock.patch.object(
        inkdrop_web.inkdrop_completed_import,
        "auto_inspect_completion_allowed",
        return_value=False,
    ), mock.patch.object(
        inkdrop_web,
        "sync_manual_source_import_proof",
        side_effect=AssertionError("uninspected artifact must not create completion proof"),
    ), mock.patch.object(
        inkdrop_web,
        "mark_manual_source_import_resolved",
        side_effect=AssertionError("uninspected artifact must not resolve"),
    ):
        result = inkdrop_web.import_detected_manual_source(
            {"review_id": item["review_id"], "path": str(source_path), "dryRun": False}
        )
    require(result.get("manual_source_resolved") is False, f"uninspected import cleared the row: {result}")
    require(
        (result.get("manual_source_resolution_status") or {}).get("state") == "verification_pending",
        f"uninspected import did not remain pending: {result}",
    )


def smoke_manual_source_proof_settles_queue_and_wanted():
    import inkdrop_state
    import inkdrop_web

    with tempfile.TemporaryDirectory(prefix="inkdrop-manual-proof-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        library_root = root / "library"
        destination = library_root / "Verified Series" / "Verified Series 001.cbz"
        destination.parent.mkdir(parents=True)
        with zipfile.ZipFile(destination, "w") as archive:
            archive.writestr("001.jpg", b"image")
            archive.writestr("ComicInfo.xml", "<ComicInfo><Series>Verified Series</Series><Number>1</Number></ComicInfo>")
        state_db = root / "inkdrop-state.sqlite3"
        with inkdrop_state.connect(state_db) as con:
            inkdrop_state.init_schema(con)
            con.execute(
                "insert into app_settings(key,scope,value_json,source,updated_at) values(?,?,?,?,?)",
                ("media_management.comic_root", "media_management", json.dumps(str(library_root)), "test", 1.0),
            )
            con.execute(
                "insert into series(id,title,sort_title,media_type,metadata_provider,metadata_id,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?)",
                ("comicvine:100", "Verified Series", "verified series", "comic", "comicvine", "100", 1.0, 1.0, "{}"),
            )
            con.execute(
                "insert into issues(id,series_id,issue_number,normalized_number,title,metadata_provider,metadata_id,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?,?)",
                ("comicvine-issue:101", "comicvine:100", "1", "1", "Issue 1", "comicvine", "101", 1.0, 1.0, "{}"),
            )
            con.execute(
                "insert into wanted_items(id,series_id,issue_id,reason,status,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?)",
                ("wanted:1", "comicvine:100", "comicvine-issue:101", "test", "in_progress", 1.0, 1.0, "{}"),
            )
            con.execute(
                "insert into queue_items(id,wanted_id,series_id,issue_id,state,current_source,query,last_event,active,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?,?,?,?)",
                ("queue:verified", "wanted:1", "comicvine:100", "comicvine-issue:101", "importing", "slskd", "Verified Series 1", "waiting for proof", 1, 1.0, 1.0, json.dumps({"series": "Verified Series", "issue": "1"})),
            )
            con.commit()

        item = {
            "review_id": "review:verified",
            "autopilot_queue_key": "queue:verified",
            "wanted_id": "wanted:1",
            "inkdrop_series_id": "comicvine:100",
            "inkdrop_issue_id": "comicvine-issue:101",
            "series": "Verified Series",
            "issue": "1",
        }
        result = {
            "imported": [
                {
                    "source": str(root / "staging" / "Verified Series 001.cbz"),
                    "dest": str(destination),
                    "queue_id": "queue:substituted",
                    "series_id": "comicvine:substituted",
                    "issue_id": "comicvine-issue:substituted",
                    "matched_series": "Substituted Series",
                    "issue_number": "999",
                }
            ],
            "verification": {
                "checked": [
                    {
                        "dest": str(destination),
                        "verification_status": "folder_verified",
                        "folder_path_exists": True,
                        "library_visibility_required": False,
                    }
                ],
                "checked_count": 1,
                "failure_count": 0,
                "folder_verified_count": 1,
            },
        }
        with mock.patch.object(inkdrop_web, "INKDROP_STATE_DB", state_db):
            proof = inkdrop_web.sync_manual_source_import_proof(
                item,
                {"path": str(root / "staging" / "Verified Series 001.cbz")},
                result,
            )
        require(proof.get("ok") is True, f"matching verified import proof was not recorded: {proof}")
        with sqlite3.connect(state_db) as con:
            queue_state = con.execute("select state,active from queue_items where id='queue:verified'").fetchone()
            wanted_status = con.execute("select status from wanted_items where id='wanted:1'").fetchone()[0]
            proof_count = con.execute(
                "select count(*) from import_results where queue_id='queue:verified' and verified=1"
            ).fetchone()[0]
        require(queue_state == ("verified", 0), f"verified import did not settle queue: {queue_state}")
        require(wanted_status == "satisfied", f"verified import did not settle Wanted: {wanted_status}")
        require(proof_count == 1, f"verified import proof was not idempotently recorded: {proof_count}")

        stale_item = {**item, "autopilot_queue_key": "queue:missing"}
        with mock.patch.object(inkdrop_web, "INKDROP_STATE_DB", state_db):
            stale_proof = inkdrop_web.sync_manual_source_import_proof(
                stale_item,
                {"path": str(root / "staging" / "Verified Series 001.cbz")},
                result,
            )
        require(stale_proof.get("ok") is False, f"stale exact queue key fell back to another queue: {stale_proof}")
        missing_item = dict(item)
        missing_item.pop("autopilot_queue_key")
        substituted_result = json.loads(json.dumps(result))
        substituted_result["imported"][0]["queue_id"] = "queue:verified"
        with mock.patch.object(inkdrop_web, "INKDROP_STATE_DB", state_db):
            missing_proof = inkdrop_web.sync_manual_source_import_proof(
                missing_item,
                {"path": str(root / "staging" / "Verified Series 001.cbz")},
                substituted_result,
            )
        require(missing_proof.get("ok") is False, f"child queue ID replaced missing trusted identity: {missing_proof}")
        require(
            missing_proof.get("reason") == "missing_exact_queue_id",
            f"missing trusted queue ID did not fail closed: {missing_proof}",
        )
        with sqlite3.connect(state_db) as con:
            proof_count_after_stale = con.execute(
                "select count(*) from import_results where verified=1"
            ).fetchone()[0]
        require(proof_count_after_stale == 1, f"stale queue key created another completion proof: {proof_count_after_stale}")


def smoke_manual_source_threads_exact_issue_identity():
    import inkdrop_web

    source_path = Path("/staging/slskd/Fullmetal Alchemist #007 - After The Rain.cbz")
    item = {
        "review_id": "review:fma-7",
        "source": "series_autopilot_queue",
        "autopilot_queue": True,
        "series": "Fullmetal Alchemist",
        "issue": "7",
        "issue_title": "After The Rain",
        "issue_id": "zzz-current",
        "inkdrop_series_id": "comicvine:100",
    }
    detected = {"path": str(source_path), "filename": source_path.name}
    calls = []

    def fake_run_command(args, timeout=75, env=None):
        calls.append(list(args))
        return json.dumps({"count": 0, "imported": []})

    with mock.patch.object(
        inkdrop_web,
        "detected_manual_source_file",
        return_value=(item, detected, source_path),
    ), mock.patch.object(
        inkdrop_web,
        "manual_source_archive_uses_pack_import",
        return_value=False,
    ), mock.patch.object(
        inkdrop_web,
        "run_command",
        side_effect=fake_run_command,
    ):
        inkdrop_web.import_detected_manual_source(
            {"review_id": item["review_id"], "path": str(source_path), "dryRun": True}
        )

    require(len(calls) == 1, f"manual source did not invoke completed import once: {calls}")
    command = calls[0]
    for flag, value in (
        ("--trusted-series-id", "comicvine:100"),
        ("--trusted-issue", "7"),
        ("--trusted-issue-id", "zzz-current"),
        ("--trusted-issue-title", "After The Rain"),
    ):
        require(flag in command, f"manual source command omitted {flag}: {command}")
        require(command[command.index(flag) + 1] == value, f"manual source command changed {flag}: {command}")

    unbound = inkdrop_web.manual_source_trusted_import_context(
        {**item, "issue_id": None, "issue_title": "Brotherhood"},
        detected,
    )
    require("issue_title" not in unbound, f"unbound issue title crossed trust boundary: {unbound}")


def smoke_deferred_status_allows_concurrent_queue_writer():
    import inkdrop_completed_import as completed_import

    with tempfile.TemporaryDirectory(prefix="inkdrop-manual-import-defer-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        queue_db = root / "queue.sqlite3"
        with sqlite3.connect(queue_db) as con:
            con.execute("create table queue_items(id text primary key, state text not null)")
            con.execute("insert into queue_items(id, state) values('queue:1', 'queued')")

        results = {}
        errors = []
        start = threading.Barrier(2)

        def write_status():
            try:
                start.wait(timeout=2)
                results["status"] = completed_import.write_import_status(
                    {"kind": "comics", "imported_count": 1}
                )
            except Exception as exc:
                errors.append(exc)

        def write_queue():
            try:
                start.wait(timeout=2)
                with sqlite3.connect(queue_db, timeout=0.25) as con:
                    con.execute("update queue_items set state='searching' where id='queue:1'")
                results["queue"] = True
            except Exception as exc:
                errors.append(exc)

        with mock.patch.object(completed_import, "STATE_DIR", root), mock.patch.object(
            completed_import, "IMPORT_STATUS_PATH", root / "import-status.json"
        ), mock.patch.object(completed_import, "LOG_PATH", root / "completed-import.log"), mock.patch.object(
            completed_import,
            "sync_inkdrop_import_results",
            side_effect=AssertionError("deferred manual child must not enter broad state sync"),
        ), mock.patch.dict(
            os.environ,
            {"INKDROP_COMPLETED_IMPORT_STATUS_SYNC_MODE": "defer"},
            clear=False,
        ):
            threads = [threading.Thread(target=write_status), threading.Thread(target=write_queue)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=3)

        require(not any(thread.is_alive() for thread in threads), "deferred status concurrency test did not finish")
        require(not errors, f"deferred status blocked a concurrent queue writer: {errors}")
        require(results.get("queue") is True, f"concurrent queue writer did not complete: {results}")
        require(
            (results.get("status") or {}).get("reason") == "import_status_sync_deferred",
            f"manual child status write did not skip broad sync: {results}",
        )
        with sqlite3.connect(queue_db) as con:
            state = con.execute("select state from queue_items where id='queue:1'").fetchone()[0]
        require(state == "searching", f"concurrent queue write was not durable: {state}")


def main():
    internal_calls = []
    fake_web = types.SimpleNamespace(
        import_detected_manual_source=lambda payload: internal_calls.append(dict(payload)) or {
            "dry_run": bool(payload.get("dryRun")),
            "manual_source_resolution_status": {
                "state": "preview_importable" if payload.get("dryRun") else "verified_clearable"
            },
        }
    )
    original_web = sys.modules.get("inkdrop_web")
    sys.modules["inkdrop_web"] = fake_web
    try:
        with mock.patch.dict(
            os.environ,
            {
                "INKDROP_AUTH_MODE": "required",
                "INKDROP_MANUAL_SOURCE_IMPORT_API_URL": "",
                "INKDROP_WEB_BASE_URL": "",
                "INKDROP_CONTAINER_WEB_BASE_URL": "http://inkdrop:8796",
                "INKDROP_WORKER_API_KEY": "",
            },
            clear=False,
        ), mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("default loopback crossed HTTP auth boundary")):
            preview_path = Path("/staging/slskd/preview.cbz")
            live_path = Path("/staging/slskd/live.cbz")
            preview = resolver.post_import_detected(
                resolver.DEFAULT_MANUAL_SOURCE_IMPORT_API_URL,
                "review:preview",
                preview_path,
                dry_run=True,
            )
            live = resolver.post_import_detected(
                resolver.DEFAULT_MANUAL_SOURCE_IMPORT_API_URL,
                "review:live",
                live_path,
                dry_run=False,
            )
        require(resolver.importable_preview(preview), "auth-enabled internal preview did not remain importable")
        require((live.get("result") or {}).get("manual_source_resolution_status", {}).get("state") == "verified_clearable", "auth-enabled internal live import did not run")
        require(
            internal_calls == [
                {"review_id": "review:preview", "path": str(preview_path), "dryRun": True},
                {"review_id": "review:live", "path": str(live_path), "dryRun": False},
            ],
            "trusted internal preview/live payload changed",
        )
        require(not any("token" in key.lower() for payload in internal_calls for key in payload), "internal path added a secret token workaround")

        def assert_external_http_boundary(label, external_url, env):
            missing_env = {**env, "INKDROP_WORKER_API_KEY": ""}
            with mock.patch.dict(os.environ, missing_env, clear=False), mock.patch.object(
                urllib.request, "urlopen", side_effect=AssertionError("missing worker key must fail before network")
            ):
                try:
                    resolver.post_import_detected(
                        external_url,
                        f"review:{label}:missing",
                        f"/staging/{label}-missing.cbz",
                        dry_run=True,
                    )
                except RuntimeError as exc:
                    require("INKDROP_WORKER_API_KEY" in str(exc), f"{label} missing-key failure was not actionable")
                else:
                    raise AssertionError(f"{label} allowed unauthenticated worker HTTP")
            unauthorized = urllib.error.HTTPError(
                external_url,
                401,
                "Unauthorized",
                hdrs=None,
                fp=io.BytesIO(json.dumps({"ok": False, "error": "authentication_required"}).encode("utf-8")),
            )
            worker_key = "ik_worker_test_secret"
            with mock.patch.dict(os.environ, {**env, "INKDROP_WORKER_API_KEY": worker_key}, clear=False), mock.patch.object(
                urllib.request, "urlopen", side_effect=unauthorized
            ) as external_call:
                try:
                    resolver.post_import_detected(
                        external_url,
                        f"review:{label}",
                        f"/staging/{label}.cbz",
                        dry_run=True,
                    )
                except RuntimeError as exc:
                    require(
                        "HTTP 401" in str(exc) and "authentication_required" in str(exc),
                        f"{label} auth failure lost its typed context",
                    )
                else:
                    raise AssertionError(f"{label} bypassed HTTP authentication")
            request = external_call.call_args.args[0]
            require(request.full_url == external_url, f"{label} URL was not preserved")
            require(
                request.get_header("X-inkdrop-api-key") == worker_key,
                f"{label} did not use the supported InkDrop API-key header",
            )
            require(not request.has_header("Authorization") and not request.has_header("Cookie"), f"{label} invented a bearer/cookie workaround")
            require(external_call.call_count == 1, f"{label} did not make exactly one HTTP request")
            require(len(internal_calls) == 2, f"{label} was routed through trusted in-process path")

        manual_url = "https://manual-callback.example/api/manual-source/import-detected"
        assert_external_http_boundary(
            "manual_env",
            manual_url,
            {"INKDROP_MANUAL_SOURCE_IMPORT_API_URL": manual_url, "INKDROP_WEB_BASE_URL": ""},
        )
        web_base_url = "https://web-base.example/api/manual-source/import-detected"
        assert_external_http_boundary(
            "web_base_env",
            web_base_url,
            {"INKDROP_MANUAL_SOURCE_IMPORT_API_URL": "", "INKDROP_WEB_BASE_URL": "https://web-base.example"},
        )
        cli_url = "https://cli-override.example/api/manual-source/import-detected"
        assert_external_http_boundary(
            "cli_api_url",
            cli_url,
            {"INKDROP_MANUAL_SOURCE_IMPORT_API_URL": "", "INKDROP_WEB_BASE_URL": ""},
        )
    finally:
        if original_web is None:
            sys.modules.pop("inkdrop_web", None)
        else:
            sys.modules["inkdrop_web"] = original_web

    smoke_live_manual_import_defers_broad_sync()
    smoke_live_manual_import_requires_durable_completion_proof()
    smoke_live_manual_import_requires_exact_artifact_inspection()
    smoke_manual_source_proof_settles_queue_and_wanted()
    smoke_manual_source_threads_exact_issue_identity()
    smoke_deferred_status_allows_concurrent_queue_writer()

    print("INKDROP_MANUAL_SOURCE_INTERNAL_IMPORT_AUTH_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
