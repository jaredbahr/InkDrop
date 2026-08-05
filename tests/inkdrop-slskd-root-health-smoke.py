#!/usr/bin/env python3
import concurrent.futures
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

import inkdrop_slskd_root_health as root_health


def main():
    with tempfile.TemporaryDirectory() as temp_dir:
        base = Path(temp_dir)
        download = base / "complete"
        incomplete = base / "incomplete"
        download.mkdir()
        incomplete.mkdir()

        healthy = root_health.slskd_root_reachability(download, incomplete)
        assert healthy["ok"] is True, healthy
        assert healthy["problem_count"] == 0, healthy
        assert all(item["readable"] and item["writable"] for item in healthy["roots"]), healthy
        assert not list(base.rglob(".inkdrop-write-probe*")), "write probe leaked a file"

        missing = root_health.probe_root("missing", "Missing root", base / "absent")
        assert missing["state"] == "critical", missing
        assert "does not exist inside the InkDrop container" in missing["detail"], missing
        assert "mount" in missing["next_action"].lower(), missing

        regular_file = base / "not-a-directory"
        regular_file.write_text("x", encoding="utf-8")
        wrong_type = root_health.probe_root("file", "File root", regular_file)
        assert wrong_type["state"] == "critical", wrong_type
        assert "not a directory" in wrong_type["detail"], wrong_type

        real_open = os.open

        def reject_probe(path, flags, mode=0o777):
            if Path(path).name.startswith(".inkdrop-write-probe"):
                raise PermissionError("read-only mount")
            return real_open(path, flags, mode)

        with mock.patch.object(root_health.os, "open", side_effect=reject_probe):
            read_only = root_health.probe_root("readonly", "Read-only root", download)
        assert read_only["readable"] is True, read_only
        assert read_only["writable"] is False, read_only
        assert "cannot be written inside the InkDrop container" in read_only["detail"], read_only
        assert "permissions" in read_only["next_action"].lower(), read_only
        assert not list(base.rglob(".inkdrop-write-probe*")), "failed probe leaked a file"

        sentinel = download / ".inkdrop-write-probe"
        sentinel.write_bytes(root_health._PROBE_CONTENT)
        path_type = type(download)
        real_unlink = path_type.unlink

        def reject_sentinel_unlink(path, *args, **kwargs):
            if Path(path) == sentinel:
                raise PermissionError("unlink denied")
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(path_type, "unlink", side_effect=reject_sentinel_unlink, autospec=True):
            cleanup_failed = root_health.probe_root("cleanup", "Cleanup root", download)
            assert cleanup_failed["status"] == "cleanup required", cleanup_failed
            assert cleanup_failed["cleanup_failed"] is True, cleanup_failed
            assert sentinel.read_bytes() == root_health._PROBE_CONTENT
            assert root_health._owned_probe_file(sentinel)
            repeated = root_health.probe_root("cleanup", "Cleanup root", download)
            assert repeated["status"] == "cleanup required", repeated
            assert list(download.glob(".inkdrop-write-probe*")) == [sentinel], "probe residue was not bounded"
        sentinel.unlink()

        disabled = root_health.slskd_root_reachability(
            base / "disabled-complete", base / "disabled-incomplete", cache_seconds=0, enabled=False
        )
        assert disabled["ok"] is True and disabled["problem_count"] == 0, disabled
        assert disabled["state"] == "disabled", disabled
        assert all(item["status"] == "not checked" for item in disabled["roots"]), disabled

        unconfigured = root_health.slskd_root_reachability(
            "", "", cache_seconds=0, enabled=True, configured=False
        )
        assert unconfigured["ok"] is True and unconfigured["problem_count"] == 0, unconfigured
        assert unconfigured["state"] == "unconfigured", unconfigured

        active_probes = 0
        peak_probes = 0
        tracking_lock = threading.Lock()
        real_probe = root_health._probe_root_unlocked

        def tracked_probe(*args, **kwargs):
            nonlocal active_probes, peak_probes
            with tracking_lock:
                active_probes += 1
                peak_probes = max(peak_probes, active_probes)
            try:
                time.sleep(0.01)
                return real_probe(*args, **kwargs)
            finally:
                with tracking_lock:
                    active_probes -= 1

        with mock.patch.object(root_health, "_probe_root_unlocked", side_effect=tracked_probe):
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                concurrent_results = list(pool.map(
                    lambda _: root_health.probe_root("concurrent", "Concurrent root", download),
                    range(6),
                ))
        assert peak_probes == 1, peak_probes
        assert all(item["state"] == "healthy" for item in concurrent_results), concurrent_results
        assert not list(download.glob(".inkdrop-write-probe*")), "concurrent probes left residue"

        rendezvous = base / "multiprocess-rendezvous"
        rendezvous.mkdir()
        process_count = 6
        child_code = r'''
import json
import os
import sys
import time
from pathlib import Path
from unittest import mock

import inkdrop_slskd_root_health as root_health

root = Path(sys.argv[1])
rendezvous = Path(sys.argv[2])
identity = sys.argv[3]
process_count = int(sys.argv[4])
real_open = os.open

def synchronized_open(path, flags, mode=0o777):
    if Path(path).name.startswith(".inkdrop-write-probe"):
        (rendezvous / ("ready-" + identity)).write_text("ready", encoding="utf-8")
        deadline = time.monotonic() + 10
        while len(list(rendezvous.glob("ready-*"))) < process_count:
            if time.monotonic() >= deadline:
                raise TimeoutError("probe rendezvous timed out")
            time.sleep(0.01)
    return real_open(path, flags, mode)

with mock.patch.object(root_health.os, "open", side_effect=synchronized_open):
    result = root_health.probe_root("multiprocess", "Multiprocess root", root)
print(json.dumps(result))
'''

        def run_process(index):
            return subprocess.run(
                [sys.executable, "-c", child_code, str(download), str(rendezvous), str(index), str(process_count)],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                timeout=20,
                check=True,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=process_count) as pool:
            process_results = list(pool.map(run_process, range(process_count)))
        decoded_results = [json.loads(item.stdout) for item in process_results]
        assert all(item["state"] == "healthy" for item in decoded_results), decoded_results
        assert not list(download.glob(".inkdrop-write-probe*")), "multiprocess probes left residue"

    web_source = (Path(__file__).resolve().parents[1] / "inkdrop_web.py").read_text(encoding="utf-8")
    for contract in (
        '"path_checks": path_checks',
        '"slskd_root_checks": slskd_health.get("root_checks")',
        'primary.textContent = isSlskdPath ? "Open path settings"',
        'warning.setAttribute("aria-live", "polite")',
        'title.textContent = `${root.label || "SLSKD path"}: ${root.path || "not configured"}`',
    ):
        assert contract in web_source, f"missing web health contract: {contract}"

    import inkdrop_web
    with tempfile.TemporaryDirectory() as temp_dir:
        base = Path(temp_dir)
        download = base / "complete"
        download.mkdir()
        runtime_paths = {
            "slskd_download_root": str(download),
            "slskd_incomplete_root": str(base / "missing-incomplete"),
        }
        root_health.clear_root_health_cache()
        provider_settings = {"enabled": True, "configured": True, **runtime_paths}
        with mock.patch.object(inkdrop_web, "inkdrop_runtime_paths", return_value=runtime_paths), mock.patch.object(
            inkdrop_web, "slskd_provider_runtime_settings", return_value=provider_settings
        ):
            system = inkdrop_web.system_health_summary(
                disk_targets=[], log_dir=base / "logs", explicit_log_paths=[]
            )
        assert system["path_problem_count"] == 1, system
        assert system["problem_count"] == 1, system
        assert system["state"] == "critical", system
        assert system["path_checks"][1]["key"] == "slskd_incomplete_root", system

        root_health.clear_root_health_cache()
        provider_settings["enabled"] = False
        with mock.patch.object(inkdrop_web, "inkdrop_runtime_paths", return_value=runtime_paths), mock.patch.object(
            inkdrop_web, "slskd_provider_runtime_settings", return_value=provider_settings
        ):
            disabled_system = inkdrop_web.system_health_summary(
                disk_targets=[], log_dir=base / "logs", explicit_log_paths=[]
            )
        assert disabled_system["path_problem_count"] == 0, disabled_system
        assert disabled_system["problem_count"] == 0, disabled_system
        assert disabled_system["state"] == "healthy", disabled_system

        no_db = base / "no-state.sqlite"
        with mock.patch.object(inkdrop_web, "INKDROP_STATE_DB", no_db), mock.patch.object(
            inkdrop_web, "inkdrop_runtime_paths", return_value=runtime_paths
        ), mock.patch.object(inkdrop_web, "read_slskd_config_text", return_value=""), mock.patch.dict(
            os.environ,
            {"INKDROP_SLSKD_API_BASE_URL": "http://slskd-env:5030", "INKDROP_SLSKD_API_KEY": "env-fixture-key"},
        ):
            env_settings = inkdrop_web.slskd_provider_runtime_settings()
        assert env_settings["configured"] is True, env_settings
        assert env_settings["base_url"] == "http://slskd-env:5030/api/v0", env_settings
        assert env_settings["api_key"] == "env-fixture-key", env_settings

        with mock.patch.object(inkdrop_web, "INKDROP_STATE_DB", no_db), mock.patch.object(
            inkdrop_web, "inkdrop_runtime_paths", return_value=runtime_paths
        ), mock.patch.object(inkdrop_web, "read_slskd_config_text", return_value="key: file-fixture-key\n"), mock.patch.dict(
            os.environ,
            {"INKDROP_SLSKD_API_BASE_URL": "http://slskd-file:5030", "INKDROP_SLSKD_API_KEY": ""},
        ):
            file_settings = inkdrop_web.slskd_provider_runtime_settings()
        assert file_settings["configured"] is True, file_settings
        assert file_settings["base_url"] == "http://slskd-file:5030/api/v0", file_settings
        assert file_settings["api_key"] == "file-fixture-key", file_settings
    print("PASS: SLSKD roots are probed from InkDrop's filesystem context and surfaced in Settings/System")


if __name__ == "__main__":
    main()
