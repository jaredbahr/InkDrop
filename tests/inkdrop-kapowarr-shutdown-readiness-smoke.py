#!/usr/bin/env python3
"""Smoke-check the Kapowarr shutdown readiness gate."""

import json
import sqlite3
import tempfile
import time
from pathlib import Path

import inkdrop_state


def fail(message):
    raise AssertionError(message)


def insert_series(con, series_id, title, provider, metadata_id, now, *, source=None, monitored=1, raw=None):
    con.execute(
        """
        insert into series(
            id, title, media_type, metadata_provider, metadata_id, source,
            monitored, monitor_new, auto_grab, created_at, updated_at, raw_json
        )
        values(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            series_id,
            title,
            "comic",
            provider,
            metadata_id,
            source or provider,
            monitored,
            monitored,
            monitored,
            now,
            now,
            json.dumps(raw or {}),
        ),
    )


def seed(db_path):
    now = time.time()
    with inkdrop_state.connect(db_path) as con:
        inkdrop_state.init_schema(con)
        insert_series(con, "comicvine:clean", "Clean Native", "comicvine", "clean", now)
        insert_series(
            con,
            "kapowarr:retired",
            "Retired Adapter Shadow",
            "comicvine",
            "clean",
            now,
            source="comicvine",
            monitored=0,
            raw={
                "automation_parked_reason": "adapter_shadow_retired",
                "retired_target_series_id": "comicvine:clean",
            },
        )
        con.commit()


def populate_clean_repo_root(root):
    root = Path(root)
    for check in inkdrop_state.KAPOWARR_SHUTDOWN_CODE_CHECKS:
        path = root / str(check["file"])
        path.write_text("# no kapowarr runtime dependency here\n", encoding="utf-8")
    return root


def write_compose_copy(root, rel_path, *, profiled=True, restart='"no"'):
    path = Path(root) / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    profile_line = '    profiles: ["kapowarr-adapter"]\n' if profiled else ""
    path.write_text(
        "name: arr-core\n"
        "services:\n"
        "  flaresolverr:\n"
        "    image: ghcr.io/flaresolverr/flaresolverr:latest\n"
        "  kapowarr:\n"
        f"{profile_line}"
        "    image: mrcas/kapowarr:latest\n"
        "    container_name: kapowarr\n"
        "    depends_on: [flaresolverr]\n"
        f"    restart: {restart}\n",
        encoding="utf-8",
    )


def no_kapowarr_container_probe():
    return {
        "docker_available": True,
        "container_present": False,
        "state": "absent",
        "reason": "container_absent",
    }


def running_kapowarr_container_probe():
    return {
        "docker_available": True,
        "container_present": True,
        "running": True,
        "state": "running",
        "restart_policy": "no",
        "image": "mrcas/kapowarr:latest",
        "name": "kapowarr",
    }


def stopped_kapowarr_container_probe():
    return {
        "docker_available": True,
        "container_present": True,
        "running": False,
        "state": "exited",
        "restart_policy": "no",
        "image": "mrcas/kapowarr:latest",
        "name": "kapowarr",
    }


def main():
    with tempfile.TemporaryDirectory(prefix="inkdrop-kapowarr-shutdown-", ignore_cleanup_errors=True) as tmp:
        db_path = Path(tmp) / "inkdrop-state.sqlite3"
        seed(db_path)

        original_container_probe = inkdrop_state.kapowarr_runtime_container_probe
        inkdrop_state.kapowarr_runtime_container_probe = no_kapowarr_container_probe
        try:
            live_code_view = inkdrop_state.state_view(
                db_path,
                "kapowarr_shutdown_readiness",
                limit=50,
                summary_mode="compact",
                row_mode="compact",
            )
        finally:
            inkdrop_state.kapowarr_runtime_container_probe = original_container_probe
        if live_code_view.get("view") != "kapowarr_shutdown_readiness" or not live_code_view.get("ok"):
            fail(f"unexpected shutdown view shape: {live_code_view}")
        summary = live_code_view.get("summary") or {}
        if int(summary.get("unparked_kapowarr_series") or 0) != 0:
            fail(f"parked adapter row was counted as active: {summary}")
        if int(summary.get("parked_kapowarr_series") or 0) != 1:
            fail(f"parked adapter history was not counted: {summary}")
        if not summary.get("safe_to_stop_kapowarr"):
            fail(f"guarded-off Kapowarr compatibility paths and parked-only state should be shutdown-ready: {summary}")
        if int(summary.get("kapowarr_compose_blocker_count") or 0) != 0:
            fail(f"profile-gated compose files should not block shutdown readiness: {summary}")
        if int(summary.get("kapowarr_runtime_container_present_count") or 0) != 0:
            fail(f"absent Kapowarr container should be reported cleanly: {summary}")
        if int(summary.get("hard_code_dependency_count") or 0) != 0:
            fail(f"guarded compatibility code should not count as a hard code blocker: {summary}")
        runtime_row = next((row for row in live_code_view.get("rows") or [] if row.get("id") == "kapowarr_runtime_container"), None)
        if not runtime_row or runtime_row.get("container_present") or runtime_row.get("blocking") or runtime_row.get("state") != "ready":
            fail(f"absent Kapowarr container row should be ready and non-blocking: {runtime_row}")
        compose_rows = [row for row in live_code_view.get("rows") or [] if row.get("category") == "deployment_compose"]
        if not compose_rows:
            fail(f"shutdown readiness view did not expose deployment compose rows: {live_code_view.get('rows')}")
        for row in compose_rows:
            if row.get("file_exists") and row.get("service_present"):
                if row.get("blocking") or not row.get("profile_present") or not row.get("restart_safe"):
                    fail(f"Kapowarr compose service should be opt-in and restart-safe: {row}")
        completed_row = next((row for row in live_code_view.get("rows") or [] if row.get("id") == "completed_import_kapowarr_adapter"), None)
        if not completed_row or not completed_row.get("guard_token_present") or completed_row.get("blocking"):
            fail(f"guarded completed-import fallback should not block when completed-import fallback is off: {completed_row}")
        web_row = next((row for row in live_code_view.get("rows") or [] if row.get("id") == "web_kapowarr_sync"), None)
        if not web_row or not web_row.get("guard_token_present") or web_row.get("blocking"):
            fail(f"guarded legacy web sync should not block when allow_legacy_sync is off: {web_row}")
        missing_row = next((row for row in live_code_view.get("rows") or [] if row.get("id") == "missing_acquire_kapowarr_fallback"), None)
        if not missing_row or not missing_row.get("guard_token_present") or missing_row.get("blocking"):
            fail(f"guarded missing-acquire fallback should not block when kapowarr fallback is off: {missing_row}")
        autopilot_row = next((row for row in live_code_view.get("rows") or [] if row.get("id") == "series_autopilot_kapowarr_paths"), None)
        if not autopilot_row or not autopilot_row.get("guard_token_present") or autopilot_row.get("blocking"):
            fail(f"guarded Series Autopilot path fallback should not block when path fallback is off: {autopilot_row}")
        path_fallback_row = next((row for row in live_code_view.get("rows") or [] if row.get("id") == "kapowarr_path_fallback"), None)
        if not path_fallback_row or path_fallback_row.get("blocking") or path_fallback_row.get("enabled") or not path_fallback_row.get("retired"):
            fail(f"path fallback setting should default to shutdown-ready/off: {path_fallback_row}")
        missing_fallback_row = next((row for row in live_code_view.get("rows") or [] if row.get("id") == "kapowarr_missing_fallback"), None)
        if not missing_fallback_row or missing_fallback_row.get("blocking") or missing_fallback_row.get("enabled") or not missing_fallback_row.get("retired"):
            fail(f"missing fallback setting should be retired and shutdown-ready/off: {missing_fallback_row}")
        completed_fallback_row = next((row for row in live_code_view.get("rows") or [] if row.get("id") == "kapowarr_completed_import_fallback"), None)
        if not completed_fallback_row or completed_fallback_row.get("blocking") or completed_fallback_row.get("enabled") or not completed_fallback_row.get("retired"):
            fail(f"completed-import fallback setting should default to shutdown-ready/off: {completed_fallback_row}")
        for row in live_code_view.get("rows") or []:
            if "raw_json" in row or "raw" in row:
                fail(f"shutdown readiness row leaked raw JSON: {row}")

        with tempfile.TemporaryDirectory(prefix="inkdrop-kapowarr-shutdown-clean-root-") as clean_root:
            clean_view = inkdrop_state.kapowarr_shutdown_readiness_view(
                db_path,
                limit=50,
                summary_mode="compact",
                repo_root=populate_clean_repo_root(clean_root),
                container_probe=no_kapowarr_container_probe,
            )
        clean_summary = clean_view.get("summary") or {}
        if not clean_summary.get("safe_to_stop_kapowarr"):
            fail(f"clean repo root and parked-only state should be shutdown-ready: {clean_summary}")
        if clean_summary.get("kapowarr_container_recommendation") != "safe_to_stop":
            fail(f"clean repo did not recommend safe_to_stop: {clean_summary}")

        with tempfile.TemporaryDirectory(prefix="inkdrop-kapowarr-shutdown-running-root-") as running_root:
            running_view = inkdrop_state.kapowarr_shutdown_readiness_view(
                db_path,
                limit=50,
                summary_mode="compact",
                repo_root=populate_clean_repo_root(running_root),
                container_probe=running_kapowarr_container_probe,
            )
        running_summary = running_view.get("summary") or {}
        if not running_summary.get("safe_to_stop_kapowarr"):
            fail(f"running runtime container should not make the dependency safety gate unsafe: {running_summary}")
        if running_summary.get("kapowarr_container_recommendation") != "stop_container":
            fail(f"running runtime container should recommend stop_container: {running_summary}")
        running_row = next((row for row in running_view.get("rows") or [] if row.get("id") == "kapowarr_runtime_container"), None)
        if not running_row or running_row.get("blocking") or not running_row.get("container_running") or running_row.get("state") != "gap":
            fail(f"running runtime container row should be surfaced as a non-blocking gap: {running_row}")

        with tempfile.TemporaryDirectory(prefix="inkdrop-kapowarr-shutdown-stopped-root-") as stopped_root:
            stopped_view = inkdrop_state.kapowarr_shutdown_readiness_view(
                db_path,
                limit=50,
                summary_mode="compact",
                repo_root=populate_clean_repo_root(stopped_root),
                container_probe=stopped_kapowarr_container_probe,
            )
        stopped_summary = stopped_view.get("summary") or {}
        if stopped_summary.get("kapowarr_container_recommendation") != "remove_stopped_container":
            fail(f"stopped runtime container should recommend cleanup: {stopped_summary}")
        stopped_row = next((row for row in stopped_view.get("rows") or [] if row.get("id") == "kapowarr_runtime_container"), None)
        if not stopped_row or stopped_row.get("blocking") or stopped_row.get("state") != "watch":
            fail(f"stopped runtime container row should be a non-blocking watch item: {stopped_row}")

        with tempfile.TemporaryDirectory(prefix="inkdrop-kapowarr-shutdown-profiled-compose-") as profiled_root:
            populate_clean_repo_root(profiled_root)
            write_compose_copy(profiled_root, "arr-core-compose.bazarr.yaml", profiled=True, restart='"no"')
            profiled_view = inkdrop_state.kapowarr_shutdown_readiness_view(
                db_path,
                limit=50,
                summary_mode="compact",
                repo_root=profiled_root,
                container_probe=no_kapowarr_container_probe,
            )
        profiled_summary = profiled_view.get("summary") or {}
        if not profiled_summary.get("safe_to_stop_kapowarr"):
            fail(f"profile-gated Kapowarr compose service should be shutdown-ready: {profiled_summary}")
        profiled_row = next((row for row in profiled_view.get("rows") or [] if row.get("id") == "arr_core_compose_kapowarr_profile"), None)
        if not profiled_row or profiled_row.get("blocking") or not profiled_row.get("profile_present"):
            fail(f"profiled compose row was not ready: {profiled_row}")

        with tempfile.TemporaryDirectory(prefix="inkdrop-kapowarr-shutdown-ungated-compose-") as ungated_root:
            populate_clean_repo_root(ungated_root)
            write_compose_copy(ungated_root, "arr-core-compose.bazarr.yaml", profiled=False, restart="unless-stopped")
            ungated_view = inkdrop_state.kapowarr_shutdown_readiness_view(
                db_path,
                limit=50,
                summary_mode="compact",
                repo_root=ungated_root,
                container_probe=no_kapowarr_container_probe,
            )
        ungated_summary = ungated_view.get("summary") or {}
        if ungated_summary.get("safe_to_stop_kapowarr"):
            fail(f"ungated Kapowarr compose service should block shutdown-readiness: {ungated_summary}")
        ungated_row = next((row for row in ungated_view.get("rows") or [] if row.get("id") == "arr_core_compose_kapowarr_profile"), None)
        if not ungated_row or not ungated_row.get("blocking") or ungated_row.get("profile_present"):
            fail(f"ungated compose row did not block: {ungated_row}")

        with sqlite3.connect(db_path) as con:
            con.row_factory = sqlite3.Row
            now = time.time()
            con.execute(
                """
                insert into series(
                    id, title, media_type, metadata_provider, metadata_id, source,
                    monitored, monitor_new, auto_grab, created_at, updated_at, raw_json
                )
                values(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                ("kapowarr:active", "Active Adapter", "comic", "kapowarr", "", "kapowarr", 1, 1, 1, now, now, "{}"),
            )
            con.commit()

        with tempfile.TemporaryDirectory(prefix="inkdrop-kapowarr-shutdown-clean-root-") as clean_root:
            active_view = inkdrop_state.kapowarr_shutdown_readiness_view(
                db_path,
                limit=50,
                summary_mode="compact",
                repo_root=populate_clean_repo_root(clean_root),
                container_probe=no_kapowarr_container_probe,
            )
        active_summary = active_view.get("summary") or {}
        if active_summary.get("safe_to_stop_kapowarr"):
            fail(f"active Kapowarr-owned series should block shutdown: {active_summary}")
        if int(active_summary.get("unparked_kapowarr_series") or 0) != 1:
            fail(f"active Kapowarr series count missing: {active_summary}")
        if not any(row.get("id") == "kapowarr_truth_series" and row.get("blocking") for row in active_view.get("rows") or []):
            fail(f"active Kapowarr truth row did not block shutdown: {active_view.get('rows')}")

    print("KAPOWARR_SHUTDOWN_READINESS_OK: shutdown gate separates parked adapter history from live/runtime dependencies")


if __name__ == "__main__":
    main()
