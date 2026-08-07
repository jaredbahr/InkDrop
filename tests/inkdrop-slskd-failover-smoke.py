#!/usr/bin/env python3
"""Deterministic SLSKD failover regression coverage."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from PIL import Image

from core import inkdrop_manual_source_autoresolve as resolver
from core import inkdrop_completed_import as completed_import
from core import inkdrop_series_autopilot as autopilot
from core import inkdrop_slskd_source_probe as probe
from core import inkdrop_state


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def threshold_smoke():
    original_now = resolver.now
    start = 10_000.0
    later = start + 48 * 60 * 60 + 60
    try:
        resolver.now = lambda: start
        require(resolver.SLSKD_WAITING_QUEUED_STALE_SECONDS == 48 * 60 * 60, "default queued-wait stall must be 48 hours")
        record = {"candidate_source": "slskd_probe", "ts": start - 301, "review_id": "review", "filename": "Series 001.cbz"}
        remote = {
            "status": "transfer_in_progress",
            "state": "Queued, Remotely",
            "requestedAt": "1970-01-01T02:41:39",
            "bytesTransferred": 0,
            "percentComplete": 0,
            "averageSpeed": 0,
            "same_user_active_transfer_count": 1,
            "same_candidate_active_transfer_count": 0,
        }
        require(
            resolver.queued_transfer_stale_seconds(remote) == resolver.SLSKD_WAITING_QUEUED_STALE_SECONDS,
            "an unrelated same-user active transfer must not shorten the queued-wait threshold",
        )
        require(not resolver.stale_waiting_failure_reason(record, remote), "a five-minute-old remote queue wait must survive the 48h default")
        resolver.now = lambda: later
        require("queued remotely" in resolver.stale_waiting_failure_reason(record, remote), "remote queue wait must fail once it exceeds 48 hours")
        resolver.now = lambda: start
        local = dict(remote, state="Queued, Locally")
        record["ts"] = start - 241
        require(not resolver.stale_waiting_failure_reason(record, local), "a four-minute-old local queue wait must survive the 48h default")
        resolver.now = lambda: later
        require("queued locally" in resolver.stale_waiting_failure_reason(record, local), "local queue wait must fail once it exceeds 48 hours")
        resolver.now = lambda: start
        remote["same_candidate_active_transfer_count"] = 1
        require(
            resolver.queued_transfer_stale_seconds(remote) == resolver.SLSKD_WAITING_QUEUED_STALE_SECONDS,
            "same-candidate progress elsewhere must not shorten the queued-wait delay below the 48h default",
        )

        active = {
            "status": "transfer_in_progress",
            "state": "InProgress",
            "requestedAt": "1970-01-01T02:36:41",
            "bytesTransferred": 0,
            "percentComplete": 0,
            "averageSpeed": 0,
        }
        configured = {"enabled": True, "seconds": 10 * 60}
        record["ts"] = 10_000 - 599
        require(not resolver.stale_waiting_failure_reason(record, active, configured), "active transfer failed below configured threshold")
        record["ts"] = 10_000 - 600
        require("stalled with no progress" in resolver.stale_waiting_failure_reason(record, active, configured), "active transfer did not fail at configured threshold")
        require(resolver.transfer_zero_progress(active) is True, "explicit live numeric zero was not recognized")
        unknown_progress_cases = {
            "missing": {},
            "null": {"bytesTransferred": None, "percentComplete": None, "averageSpeed": None},
            "nonnumeric": {"bytesTransferred": "?", "percentComplete": "?", "averageSpeed": "?"},
            "negative": {"bytesTransferred": -1, "percentComplete": -1, "averageSpeed": -1},
        }
        for label, progress_fields in unknown_progress_cases.items():
            unknown_active = {
                "status": "transfer_in_progress",
                "state": "InProgress",
                "requestedAt": active["requestedAt"],
                **progress_fields,
            }
            require(
                resolver.transfer_zero_progress(unknown_active) is False,
                f"{label} live progress evidence was classified as affirmative zero",
            )
            require(
                not resolver.stale_waiting_failure_reason(record, unknown_active, configured),
                f"{label} live progress evidence triggered stall cancellation/retry",
            )
        progressing_active = dict(active, bytesTransferred=1)
        require(resolver.transfer_zero_progress(progressing_active) is False, "positive live progress was classified as zero")
        require(
            not resolver.stale_waiting_failure_reason(record, progressing_active, configured),
            "positive live progress triggered stall cancellation/retry",
        )
        require(
            not resolver.stale_waiting_failure_reason(record, active, {"enabled": False, "seconds": 5 * 60}),
            "disabled stall gate failed active transfer",
        )
        queued = dict(active, state="Queued, Remotely", requestedAt="1970-01-01T02:40:40")
        record["ts"] = 10_000 - 6 * 60
        require(
            not resolver.stale_waiting_failure_reason(record, queued, {"enabled": True, "seconds": 5 * 60}),
            "active stall threshold incorrectly classified an ordinary queued transfer",
        )
        queued_local = dict(active, state="Queued, Locally", requestedAt="1970-01-01T02:40:40")
        require(
            not resolver.stale_waiting_failure_reason(record, queued_local, {"enabled": True, "seconds": 5 * 60}),
            "active stall threshold incorrectly classified an ordinary local queued transfer",
        )

        context = resolver.same_user_active_transfer_context(
            {"id": "old", "username": "peer", "path": r"Series A\001.cbz"},
            {
                "transfers": [
                    {"id": "other", "username": "peer", "path": r"Series B\001.cbz", "state": "InProgress", "bytesTransferred": 10},
                    {"id": "same", "username": "peer", "path": r"Series A\001.cbz", "state": "InProgress", "bytesTransferred": 20},
                ]
            },
        )
        require(context["same_user_active_transfer_count"] == 2, "same-user context count wrong")
        require(context["same_candidate_active_transfer_count"] == 1, "same peer/leaf from a different full locator was treated as the candidate")
        weak_context = resolver.same_user_active_transfer_context(
            {"id": "old", "username": "peer", "filename": "001.cbz"},
            {"transfers": [{"id": "same-leaf", "username": "peer", "filename": "001.cbz", "state": "InProgress", "bytesTransferred": 10}]},
        )
        require(weak_context["same_candidate_active_transfer_count"] == 0, "bare filename leaf was accepted as same-candidate proof")

        qualified = {
            "username": "peer",
            "filename": r"(1988) Batman - The Killing Joke\Batman - The Killing Joke #001 (1988).cbz",
            "size": 84_782_425,
        }
        basename = {
            "username": "peer",
            "filename": "Batman - The Killing Joke #001 (1988).cbz",
            "size": 84_782_425,
        }
        require(
            probe.slskd_candidate_download_url_hash(qualified) == probe.slskd_candidate_download_url_hash(basename),
            "directory-qualified and basename SLSKD representations split shared candidate identity",
        )
        require(
            probe.slskd_private_locator_digest(qualified) != probe.slskd_private_locator_digest(basename),
            "strict private locator digest accepted a relabeled basename",
        )

        compact = resolver.compact_transfer(
            {
                "id": "completed",
                "filename": basename["filename"],
                "directory": "(1988) Batman - The Killing Joke",
                "state": "Completed, Succeeded",
            }
        )
        compact["status"] = "transfer_succeeded"
        require(compact.get("directory") == "(1988) Batman - The Killing Joke", "transfer directory was dropped")
        with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-completed-path-") as temp:
            root = Path(temp)
            completed = root / compact["directory"] / compact["filename"]
            completed.parent.mkdir(parents=True)
            completed.write_bytes(b"comic")
            candidates = resolver.transfer_local_path_candidates(
                SimpleNamespace(SLSKD_DOWNLOAD_ROOT=root),
                {"series": "Batman: The Killing Joke", "filename": compact["filename"]},
                compact,
            )
            require(completed in candidates, f"completed directory-qualified transfer was not found: {candidates}")

            remote_prefixed = {
                "status": "transfer_succeeded",
                "filename": compact["filename"],
                "directory": rf"Comics\DC\{compact['directory']}",
            }
            prefixed_candidates = resolver.transfer_local_path_candidates(
                SimpleNamespace(SLSKD_DOWNLOAD_ROOT=root),
                {"series": "Batman: The Killing Joke", "filename": compact["filename"]},
                remote_prefixed,
            )
            require(prefixed_candidates == [completed.resolve()], f"remote-prefix suffix did not converge: {prefixed_candidates}")

            # Two exact suffixes beneath the root are ambiguous and must fail
            # closed instead of choosing the shortest/basename-like match.
            duplicate = root / "DC" / compact["directory"] / compact["filename"]
            duplicate.parent.mkdir(parents=True)
            duplicate.write_bytes(b"other comic")
            ambiguous = resolver.transfer_local_path_candidates(
                SimpleNamespace(SLSKD_DOWNLOAD_ROOT=root),
                {"series": "Unrelated", "filename": compact["filename"]},
                remote_prefixed,
            )
            require(not ambiguous, f"ambiguous suffix selected a completed file: {ambiguous}")
            duplicate.unlink()

            basename_duplicate = root / compact["filename"]
            basename_duplicate.write_bytes(b"basename collision")
            qualified_still_exact = resolver.transfer_local_path_candidates(
                SimpleNamespace(SLSKD_DOWNLOAD_ROOT=root),
                {"series": "Unrelated", "filename": compact["filename"]},
                remote_prefixed,
            )
            require(
                qualified_still_exact == [completed.resolve()],
                f"qualified locator fell back to ambiguous basename: {qualified_still_exact}",
            )
            basename_duplicate.unlink()

            # The resolver must not turn an arbitrarily deep remote locator
            # into an unbounded filesystem scan.
            deep_prefixed = {
                "status": "transfer_succeeded",
                "filename": compact["filename"],
                "directory": "\\".join([*(f"remote-{index}" for index in range(40)), compact["directory"]]),
            }
            budgeted = resolver.transfer_local_path_candidates(
                SimpleNamespace(SLSKD_DOWNLOAD_ROOT=root),
                {"series": "Unrelated", "filename": compact["filename"]},
                deep_prefixed,
            )
            require(not budgeted, f"over-budget remote suffix was searched: {budgeted}")

            # A valid early suffix plus a second valid suffix beyond the
            # 16-check budget must remain unresolved. Unchecked suffixes can
            # never be treated as proof that the early match was unique.
            budget_prefixes = [f"prefix-{index}" for index in range(18)]
            first_suffix = root.joinpath(*budget_prefixes[1:], compact["directory"], compact["filename"])
            first_suffix.parent.mkdir(parents=True)
            first_suffix.write_bytes(b"early suffix")
            beyond_budget_transfer = {
                "status": "transfer_succeeded",
                "filename": compact["filename"],
                "directory": "\\".join([*budget_prefixes, compact["directory"]]),
            }
            beyond_budget = resolver.transfer_local_path_candidates(
                SimpleNamespace(SLSKD_DOWNLOAD_ROOT=root),
                {"series": "Unrelated", "filename": compact["filename"]},
                beyond_budget_transfer,
            )
            require(not beyond_budget, f"unchecked later suffix was accepted as unique: {beyond_budget}")

            guard_root = root / "guard-root"
            guard_root.mkdir()
            outside = root / "outside-killing-joke.cbz"
            outside.write_bytes(b"must not be detected")
            traversal = resolver.compact_transfer(
                {
                    "id": "traversal",
                    "filename": outside.name,
                    "directory": "..",
                    "state": "Completed, Succeeded",
                }
            )
            traversal["status"] = "transfer_succeeded"
            traversal_candidates = resolver.transfer_local_path_candidates(
                SimpleNamespace(SLSKD_DOWNLOAD_ROOT=guard_root),
                {"series": "Batman: The Killing Joke", "filename": outside.name},
                traversal,
            )
            require(not traversal_candidates, f"directory traversal escaped the SLSKD root: {traversal_candidates}")
            traversal_detected = resolver.completed_transfer_detected_files(
                SimpleNamespace(SLSKD_DOWNLOAD_ROOT=guard_root),
                {"series": "Batman: The Killing Joke", "issue": "1"},
                {"series": "Batman: The Killing Joke", "filename": outside.name},
                traversal,
            )
            require(not traversal_detected, f"outside file reached completed-transfer detection: {traversal_detected}")
            for unsafe_locator in (
                {"filename": outside.name, "directory": "."},
                {"filename": str(outside.resolve()), "directory": ""},
                {"filename": rf"nested\..\{outside.name}", "directory": "nested"},
            ):
                unsafe_transfer = {"status": "transfer_succeeded", **unsafe_locator}
                require(
                    not resolver.transfer_local_path_candidates(
                        SimpleNamespace(SLSKD_DOWNLOAD_ROOT=guard_root),
                        {"series": "Batman: The Killing Joke", "filename": outside.name},
                        unsafe_transfer,
                    ),
                    f"unsafe transfer locator was accepted: {unsafe_locator}",
                )

            # A contained-looking symlink may not escape the configured root.
            symlink_parent = guard_root / "linked"
            try:
                symlink_parent.symlink_to(root, target_is_directory=True)
            except (OSError, NotImplementedError):
                symlink_parent = None
            if symlink_parent is not None:
                escaped = resolver.transfer_local_path_candidates(
                    SimpleNamespace(SLSKD_DOWNLOAD_ROOT=guard_root),
                    {"series": "Unrelated", "filename": compact["filename"]},
                    {
                        "status": "transfer_succeeded",
                        "filename": compact["filename"],
                        "directory": rf"remote\linked\{compact['directory']}",
                    },
                )
                require(not escaped, f"symlink suffix escaped the SLSKD root: {escaped}")
    finally:
        resolver.now = original_now


def configured_completed_transfer_root_smoke():
    with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-configured-root-") as temp:
        temp_root = Path(temp)
        state_dir = temp_root / "state"
        config_dir = temp_root / "config"
        log_dir = temp_root / "logs"
        download_root = temp_root / "configured-downloads"
        for path in (state_dir, config_dir, log_dir, download_root):
            path.mkdir(parents=True, exist_ok=True)
        db = state_dir / inkdrop_state.STATE_DB_NAME
        with inkdrop_state.connect(db) as con:
            inkdrop_state.init_schema(con)
            con.execute(
                """
                insert into provider_configs(
                    id, provider_type, display_name, enabled, base_url,
                    settings_json, source, created_at, updated_at
                ) values(?,?,?,?,?,?,?,?,?)
                """,
                (
                    "slskd",
                    "soulseek",
                    "SLSKD",
                    1,
                    "http://127.0.0.1:5030/api/v0",
                    json.dumps({"download_root": str(download_root)}),
                    "user",
                    1.0,
                    1.0,
                ),
            )

        original_env = {name: os.environ.get(name) for name in ("INKDROP_STATE_DIR", "INKDROP_CONFIG_DIR", "INKDROP_LOG_DIR")}
        try:
            os.environ.update(
                {
                    "INKDROP_STATE_DIR": str(state_dir),
                    "INKDROP_CONFIG_DIR": str(config_dir),
                    "INKDROP_LOG_DIR": str(log_dir),
                }
            )
            configured_probe, settings = resolver.load_configured_probe_module(
                Path(__file__).resolve().parents[1] / "core" / "inkdrop_slskd_source_probe.py"
            )
        finally:
            for name, value in original_env.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        require(settings.get("source") == "user", f"saved provider settings were not loaded: {settings}")
        require(
            configured_probe.SLSKD_DOWNLOAD_ROOT == download_root,
            f"configured download root was not applied: {configured_probe.SLSKD_DOWNLOAD_ROOT}",
        )
        require("api_key" not in settings and "secret_ref" not in settings, "probe settings widened secret exposure")

        directory = "(1988) Batman - The Killing Joke"
        filename = "Batman - The Killing Joke #001 (1988).cbz"
        completed = download_root / directory / filename
        completed.parent.mkdir(parents=True)
        completed.write_bytes(b"comic")
        detected = resolver.completed_transfer_detected_files(
            configured_probe,
            {"series": "Batman: The Killing Joke", "issue": "1"},
            {"series": "Batman: The Killing Joke", "issue": "1", "filename": filename},
            {
                "status": "transfer_succeeded",
                "filename": filename,
                "directory": rf"Comics\DC\{directory}",
            },
        )
        require(len(detected) == 1, f"configured-root completed transfer did not resolve uniquely: {detected}")
        require(Path(detected[0]["path"]) == completed.resolve(), f"wrong completed transfer was selected: {detected}")
        require(detected[0].get("direct_transfer_match") is True, "completed transfer lost direct-import evidence")

        rejected_directory = "Court of Owls and Nights of the Owls"
        rejected_filename = "001 Batman 001 (7 covers) (2011).cbr"
        rejected_path = download_root / rejected_directory / rejected_filename
        rejected_path.parent.mkdir(parents=True)
        rejected_path.write_bytes(b"comic")
        rejected_transfer = {
            "status": "transfer_succeeded",
            "filename": rejected_filename,
            "directory": rf"Remote\Comics\Batman\{rejected_directory}",
        }
        rejected_candidates = resolver.transfer_local_path_candidates(
            configured_probe,
            {"series": "Absolute Batman: The Court of Owls", "issue": "1", "filename": rejected_filename},
            rejected_transfer,
        )
        rejected_detected = resolver.completed_transfer_detected_files(
            configured_probe,
            {"series": "Absolute Batman: The Court of Owls", "issue": "1"},
            {"series": "Absolute Batman: The Court of Owls", "issue": "1", "filename": rejected_filename},
            rejected_transfer,
            candidates=rejected_candidates,
        )
        rejection = resolver.completed_transfer_rejection_evidence(
            configured_probe,
            {"series": "Absolute Batman: The Court of Owls", "issue": "1", "filename": rejected_filename},
            rejected_transfer,
            rejected_detected,
            candidates=rejected_candidates,
        )
        require(not rejected_detected, f"wrong-series completed artifact bypassed acceptance: {rejected_detected}")
        require(rejection and Path(rejection["path"]) == rejected_path.resolve(), f"exact rejected artifact was hidden as missing: {rejection}")
        require(rejection.get("artifact_acceptance_rejected") is True, "rejected artifact evidence lost quarantine marker")
        require(
            resolver.classify_candidate_failure("completed transfer artifact does not match selected item").get("candidate_bad") is True,
            "exact rejected completed artifact would remain retryable forever",
        )
        require(
            resolver.completed_transfer_rejection_evidence(
                configured_probe,
                {"filename": filename},
                {"status": "transfer_succeeded", "filename": filename},
                detected,
                candidates=[completed],
            ) is None,
            "accepted completed artifact was also classified as rejected",
        )
        require(
            resolver.completed_transfer_rejection_evidence(
                configured_probe,
                {"filename": rejected_filename},
                rejected_transfer,
                [],
                candidates=[rejected_path, completed],
            ) is None,
            "ambiguous completed artifacts did not fail closed",
        )

        try:
            resolver.configure_probe_module(SimpleNamespace())
        except RuntimeError as exc:
            require("does not expose" in str(exc), f"missing settings hook was not observable: {exc}")
        else:
            raise AssertionError("probe without a settings hook was accepted")

        def broken_settings():
            raise ValueError("invalid saved provider")

        try:
            resolver.configure_probe_module(SimpleNamespace(apply_slskd_provider_settings=broken_settings))
        except RuntimeError as exc:
            require("could not be applied" in str(exc), f"settings failure was not observable: {exc}")
        else:
            raise AssertionError("invalid saved settings were accepted")


def historical_missing_stage_recovery_smoke():
    with tempfile.TemporaryDirectory(prefix="inkdrop-historical-missing-stage-") as temp:
        root = Path(temp)
        db = root / inkdrop_state.STATE_DB_NAME
        fixtures = (
            ("killing", "Batman: The Killing Joke", "1", "Batman - The Killing Joke #001 (1988).cbz", "(1988) Batman - The Killing Joke"),
            ("descender", "Descender", "16", "Descender 016 (2016).cbr", "Comics\\Descender"),
            ("fullmetal", "Fullmetal Alchemist", "7", "Fullmetal Alchemist v07.cbz", "Manga\\Fullmetal Alchemist"),
            ("gotham", "Gotham Central", "12", "Gotham Central 012.cbr", "Comics\\DC\\Gotham Central"),
            ("progress", "Progress Proof", "3", "Progress Proof 003.cbz", "Comics\\Progress Proof"),
            ("ownerother", "Owner Scoped Recovery", "4", "Owner Scoped Recovery 004.cbz", "Comics\\Owner Scoped Recovery"),
            ("ownersame", "Same Candidate Failure", "5", "Same Candidate Failure 005.cbz", "Comics\\Same Candidate Failure"),
            ("zvolume", "Dorohedoro", "7", "Dorohedoro v07.cbz", "Manga\\Dorohedoro"),
        )
        with inkdrop_state.connect(db) as con:
            inkdrop_state.init_schema(con)
            for key, title, issue, filename, directory in fixtures:
                media_type = "manga" if key in {"fullmetal", "zvolume"} else "comic"
                issue_title = f"Vol. {issue}" if media_type == "manga" else None
                queue_query = f"{title} Vol. {issue}" if media_type == "manga" else title
                queue_raw = {"media_type": media_type, "issue_title": issue_title, "issue_number": issue}
                con.execute("insert into series(id,title,media_type,monitored,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?)", (f"series:{key}", title, media_type, 1, 1, 1, "{}"))
                con.execute("insert into issues(id,series_id,issue_number,title,monitored,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?)", (f"issue:{key}", f"series:{key}", issue, issue_title, 1, 1, 1, "{}"))
                con.execute("insert into wanted_items(id,series_id,issue_id,status,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?)", (f"wanted:{key}", f"series:{key}", f"issue:{key}", "wanted", 1, 1, "{}"))
                con.execute("insert into queue_items(id,wanted_id,series_id,issue_id,state,current_source,query,active,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?,?,?)", (f"queue:{key}", f"wanted:{key}", f"series:{key}", f"issue:{key}", "queued", "slskd", queue_query, 1, 1, 2, json.dumps(queue_raw)))
                transfer = {"id": f"transfer:{key}", "filename": filename, "directory": rf"Remote\\Prefix\\{directory}"}
                raw = {"transfer": transfer, "filename": filename}
                if key == "killing":
                    raw["transfer_state"] = "Completed, Succeeded"
                elif key == "progress":
                    raw.update({"transfer_percent": 100, "transfer_bytes_remaining": 0})
                else:
                    transfer["state"] = "Completed, Succeeded"
                con.execute("insert into download_tasks(id,queue_id,wanted_id,series_id,issue_id,source,provider,protocol,download_client,external_id,title,status,state,size_bytes,started_at,updated_at,completed_at,raw_json) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (f"task:{key}", f"queue:{key}", f"wanted:{key}", f"series:{key}", f"issue:{key}", "slskd", "peer", "soulseek", "SLSKD", transfer["id"], filename, "transfer_succeeded_missing_stage", "failed", 100, 1, 2, 2, json.dumps(raw)))
                completed = root / directory.replace("\\", os.sep) / filename
                completed.parent.mkdir(parents=True, exist_ok=True)
                if key == "zvolume":
                    page_bytes = io.BytesIO()
                    Image.frombytes("RGB", (512, 512), os.urandom(512 * 512 * 3)).save(page_bytes, format="PNG")
                    page_payload = page_bytes.getvalue()
                    with zipfile.ZipFile(completed, "w", compression=zipfile.ZIP_STORED) as archive:
                        for page in range(1, 13):
                            archive.writestr(f"{page:03d}.png", page_payload)
                else:
                    completed.write_bytes(b"comic")

            for key, bad_identity in (("ownerother", "candidate:other"), ("ownersame", "candidate:current")):
                con.execute(
                    "update download_tasks set candidate_identity=?,updated_at=4 where id=?",
                    ("candidate:current", f"task:{key}"),
                )
                con.execute(
                    """insert into source_attempts(
                       id,queue_id,wanted_id,series_id,issue_id,source,download_client,
                       candidate_identity,status,started_at,completed_at,raw_json
                       ) values(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        f"bad-attempt:{key}", f"queue:{key}", f"wanted:{key}", f"series:{key}",
                        f"issue:{key}", "slskd", "SLSKD", bad_identity, "quality_rejected", 3, 3,
                        json.dumps({"filename": "Owner Scoped Recovery 004.cbz" if key == "ownerother" else "Same Candidate Failure 005.cbz"}),
                    ),
                )
            con.execute(
                """insert into download_tasks(
                   id,queue_id,wanted_id,series_id,issue_id,source,download_client,candidate_identity,
                   title,status,state,started_at,updated_at,completed_at,raw_json
                   ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "bad-task:ownerother", "queue:ownerother", "wanted:ownerother", "series:ownerother",
                    "issue:ownerother", "slskd", "SLSKD", "candidate:other",
                    "Owner Scoped Recovery 004.cbz", "quality_rejected", "failed", 3, 3, 3,
                    json.dumps({"filename": "Owner Scoped Recovery 004.cbz"}),
                ),
            )

            con.execute("update download_tasks set local_path=? where id='task:killing'", (str(root / "(1988) Batman - The Killing Joke" / "Batman - The Killing Joke #001 (1988).cbz"),))

            # A genuine bad candidate is not reopened by this recovery path.
            con.execute("insert into download_tasks(id,queue_id,wanted_id,series_id,issue_id,source,download_client,external_id,title,status,state,raw_json) values(?,?,?,?,?,?,?,?,?,?,?,?)", ("task:genuine-bad", "queue:killing", "wanted:killing", "series:killing", "issue:killing", "slskd", "SLSKD", "bad", "wrong title.cbz", "staged_file_mismatch", "failed", "{}"))
            con.execute("insert into download_tasks(id,queue_id,wanted_id,series_id,issue_id,source,download_client,external_id,title,status,state,started_at,updated_at,completed_at,raw_json) select 'task:killing-old',queue_id,wanted_id,series_id,issue_id,source,download_client,'old-transfer',title,status,state,0,0,0,raw_json from download_tasks where id='task:killing'")
            con.execute("update download_tasks set id='z-history',started_at=20,updated_at=20,completed_at=20 where id='task:fullmetal'")
            con.execute("update download_tasks set id='a-history',started_at=20,updated_at=20,completed_at=20 where id='task:descender'")
            con.execute("update download_tasks set id='z-terminal-history',started_at=20,updated_at=20,completed_at=20 where id='task:gotham'")
            fullmetal_path = root / "Manga" / "Fullmetal Alchemist" / "Fullmetal Alchemist v07.cbz"
            descender_path = root / "Comics" / "Descender" / "Descender 016 (2016).cbr"
            con.execute("insert into download_tasks(id,queue_id,wanted_id,series_id,issue_id,source,download_client,title,status,state,local_path,started_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("a-ready", "queue:fullmetal", "wanted:fullmetal", "series:fullmetal", "issue:fullmetal", "slskd", "SLSKD", fullmetal_path.name, "staged_file_ready", "import_ready", str(fullmetal_path), 20, 20, "{}"))
            con.execute("insert into download_tasks(id,queue_id,wanted_id,series_id,issue_id,source,download_client,title,status,state,local_path,started_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("z-ready", "queue:descender", "wanted:descender", "series:descender", "issue:descender", "slskd", "SLSKD", descender_path.name, "staged_file_ready", "import_ready", str(descender_path), 20, 20, "{}"))
            con.execute("insert into download_tasks(id,queue_id,wanted_id,series_id,issue_id,source,download_client,title,status,state,started_at,updated_at,completed_at,raw_json) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("a-terminal", "queue:gotham", "wanted:gotham", "series:gotham", "issue:gotham", "slskd", "SLSKD", "Gotham Central 012.cbr", "transfer_failed", "failed", 20, 20, 20, "{}"))
            for index in range(30):
                key = f"partial:{index}"
                con.execute("insert into series(id,title,media_type,monitored,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?)", (f"series:{key}", f"Partial {index}", "comic", 1, 1, 1, "{}"))
                con.execute("insert into wanted_items(id,series_id,status,created_at,updated_at,raw_json) values(?,?,?,?,?,?)", (f"wanted:{key}", f"series:{key}", "wanted", 1, 1, "{}"))
                con.execute("insert into queue_items(id,wanted_id,series_id,state,current_source,query,active,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?,?)", (f"queue:{key}", f"wanted:{key}", f"series:{key}", "queued", "slskd", f"Partial {index}", 1, 1, 1, "{}"))
                transfer = {"id": f"transfer:{key}", "filename": f"Partial {index}.cbz"}
                raw = {"status": "transfer_succeeded_missing_stage", "transfer_state": "Completed, TimedOut", "transfer_percent": 0, "transfer_bytes_remaining": 49389396, "transfer": transfer}
                if index == 0:
                    raw.update({"transfer_percent": 100, "transfer_bytes_remaining": 0})
                elif index == 1:
                    raw.pop("transfer_state")
                    transfer.update({"state": "Failed", "percentComplete": 100, "bytesRemaining": 0})
                elif index == 2:
                    raw["transfer_state"] = "Completed, Succeeded"
                    transfer["state"] = "Failed"
                elif index == 3:
                    raw.update({"transfer_state": "InProgress", "transfer_percent": 100, "transfer_bytes_remaining": 0})
                    transfer["state"] = "Failed"
                elif index == 4:
                    raw.pop("transfer_state")
                    transfer["state"] = "Completed, Succeeded"
                    raw["slskd_transfer"] = {"stateDescription": "Failed"}
                con.execute("insert into download_tasks(id,queue_id,wanted_id,series_id,source,download_client,external_id,title,status,state,local_path,progress,started_at,updated_at,completed_at,raw_json) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (f"task:{key}", f"queue:{key}", f"wanted:{key}", f"series:{key}", "slskd", "SLSKD", transfer["id"], transfer["filename"], "transfer_succeeded_missing_stage", "failed", str(root / "timed-out" / transfer["filename"]), 100 if index in {0, 1, 3} else 0, 0, 0, 0, json.dumps(raw)))

        original_db = resolver.INKDROP_STATE_DB
        original_root = probe.SLSKD_DOWNLOAD_ROOT
        try:
            resolver.INKDROP_STATE_DB = db
            probe.SLSKD_DOWNLOAD_ROOT = root
            records = resolver.db_import_retry_records(0)
            require(set(records) == {"historical-missing-stage-queue:killing", "historical-missing-stage-queue:ownerother", "historical-missing-stage-queue:progress", "historical-missing-stage-queue:zvolume", "db-import-retry-a-ready", "db-import-retry-z-ready"}, f"flat structured transfer trust failed: {records.keys()}")
            require("historical-missing-stage-queue:ownersame" not in records, "same-candidate failure did not block completed-transfer recovery")
            require(records["historical-missing-stage-queue:ownerother"]["db_download_task_id"] == "task:ownerother", "unrelated failed candidate shadowed the completed transfer owner")
            require(list(records)[:2] == ["db-import-retry-a-ready", "historical-missing-stage-queue:killing"], f"mixed batch did not reserve current-ready and completed-history capacity: {list(records)}")
            require(not any("partial" in key for key in records), "partial historical evidence consumed the bounded batch")
            require(records["historical-missing-stage-queue:killing"]["db_download_task_id"] == "task:killing", "older duplicate evidence won recovery")
            for record in (row for row in records.values() if row.get("historical_false_missing_stage")):
                transfer = resolver.waiting_transfer_status(record, {"ok": True, "transfers": []})
                require(transfer and transfer.get("recorded_snapshot"), f"completed snapshot was not retained: {record}")
                detected = resolver.completed_transfer_detected_files(probe, record, record, transfer)
                require(len(detected) == 1 and detected[0].get("direct_transfer_match"), f"historical suffix did not become exact import evidence: {detected}")
            legacy_volume = records["historical-missing-stage-queue:zvolume"]
            legacy_transfer = resolver.waiting_transfer_status(legacy_volume, {"ok": True, "transfers": []})
            legacy_detected = resolver.completed_transfer_detected_files(probe, legacy_volume, legacy_volume, legacy_transfer)
            require(len(legacy_detected) == 1, f"legacy manga volume was not detected from completed SLSKD transfer: {legacy_detected}")
            with inkdrop_state.connect_read(db) as con:
                queue = dict(con.execute("select * from queue_items where id='queue:zvolume'").fetchone())
            guard_record = {"matched_local_path": legacy_detected[0]["path"], "matched_series": "Dorohedoro"}
            require(
                not inkdrop_state.collection_target_single_part_block_reason(queue, guard_record),
                "exact completed-transfer manga volume was rejected by authoritative collection guard",
            )
            manga_root = root / "library" / "manga"
            manga_root.mkdir(parents=True, exist_ok=True)
            import_target = {
                "id": "series:zvolume",
                "title": "Dorohedoro",
                "aliases": ["Dorohedoro"],
                "media_type": "manga",
                "issue_title": "Vol. 7",
                "issue_number": "7",
                "normalized_number": "7",
                "target_source": "inkdrop_series",
                "native_series_id": "series:zvolume",
                "folder": str(manga_root / "Dorohedoro"),
            }
            old_import_values = {
                name: getattr(completed_import, name)
                for name in (
                    "STATE_DIR", "DB_PATH", "INKDROP_STATE_DB", "COMIC_ROOT", "MANGA_ROOT",
                    "apply_path_provider_settings", "connect", "load_comic_targets", "load_qbit_incomplete_paths",
                    "is_stable", "log",
                )
            }
            preview_connections = []
            try:
                completed_import.STATE_DIR = root / "import-preview-state"
                completed_import.DB_PATH = completed_import.STATE_DIR / "imported-files.sqlite3"
                completed_import.INKDROP_STATE_DB = db
                completed_import.COMIC_ROOT = root / "library" / "comics"
                completed_import.MANGA_ROOT = manga_root
                completed_import.apply_path_provider_settings = lambda: {
                    "comic_root": completed_import.COMIC_ROOT,
                    "manga_root": manga_root,
                    "kavita_comic_root": "/data/comics",
                    "kavita_manga_root": "/data/manga",
                    "manual_comics_inbox": root / "manual-comics",
                    "manual_ebooks_inbox": root / "manual-ebooks",
                    "library_source": "smoke",
                    "manual_inbox_source": "smoke",
                }
                def connect_preview():
                    connection = old_import_values["connect"]()
                    preview_connections.append(connection)
                    return connection
                completed_import.connect = connect_preview
                completed_import.load_comic_targets = lambda series_filter=None: [dict(import_target)]
                completed_import.load_qbit_incomplete_paths = lambda kind: set()
                completed_import.is_stable = lambda path, min_age_seconds: True
                completed_import.log = lambda event: None
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    completed_import.import_files(
                        "comics",
                        dry_run=True,
                        min_age_seconds=0,
                        ignore_cutoff=True,
                        matched_only=True,
                        all_series=True,
                        max_files=1,
                        source_files=[legacy_detected[0]["path"]],
                        trusted_issue="7",
                        trusted_series_id="series:zvolume",
                        trusted_issue_id="issue:zvolume",
                        trusted_issue_title="Vol. 7",
                        wait_for_library_scan=False,
                    )
                preview = json.loads(output.getvalue())
            finally:
                for connection in preview_connections:
                    connection.close()
                for name, value in old_import_values.items():
                    setattr(completed_import, name, value)
            require(preview.get("count") == 1 and len(preview.get("imported") or []) == 1, preview)
            require(not preview.get("skipped"), f"real completed-import preview rejected exact legacy volume: {preview}")
            preview_event = preview["imported"][0]
            planned_name = Path((preview_event.get("media_management_preview") or {}).get("planned_path") or "").name
            require(planned_name == "Dorohedoro v07.cbz", f"completed SLSKD volume did not plan a volume-only canonical destination: {preview_event}")
            require(
                preview_event.get("source_unit") == "volume" and preview_event.get("source_volume_number") == "7",
                f"completed SLSKD volume did not carry explicit volume identity: {preview_event}",
            )
            with inkdrop_state.connect(db) as con:
                con.execute("update queue_items set active=0")
            require(not resolver.db_import_retry_records(0), "inactive/previously imported queues were reconsidered")
        finally:
            resolver.INKDROP_STATE_DB = original_db
            probe.SLSKD_DOWNLOAD_ROOT = original_root

    source = Path(resolver.__file__).read_text(encoding="utf-8")
    require(source.rfind("process_pending_slskd_retries(") > source.index("for review_id, detected, source, quality_reason, record in eligible_batch"), "pending retries still pre-empt eligible recovery rows")


def seed_queue(db_path, queue_id="queue:1"):
    with inkdrop_state.connect(db_path) as con:
        inkdrop_state.init_schema(con)
        con.execute(
            "insert into series(id,title,media_type,monitored,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?)",
            ("series:1", "Series", "comic", 1, 100.0, 100.0, "{}"),
        )
        con.execute(
            "insert into wanted_items(id,series_id,status,created_at,updated_at,raw_json) values(?,?,?,?,?,?)",
            ("wanted:1", "series:1", "wanted", 100.0, 100.0, "{}"),
        )
        con.execute(
            "insert into issues(id,series_id,issue_number,normalized_number,monitored,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?)",
            ("issue:1", "series:1", "1", "1", 1, 100.0, 100.0, json.dumps({"unit_type": "issue", "issue_number": "1"})),
        )
        con.execute("update wanted_items set issue_id='issue:1' where id='wanted:1'")
        con.execute(
            "insert into queue_items(id,wanted_id,series_id,issue_id,state,current_source,active,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?,?)",
            (queue_id, "wanted:1", "series:1", "issue:1", "downloading", "slskd", 1, 100.0, 100.0, json.dumps({"series": "Series", "issue": "1", "queue_identity": "series:1", "unit_type": "issue", "issue_number": "1"})),
        )


def durable_verified_retry_fence_smoke():
    with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-durable-verified-fence-") as temp:
        root = Path(temp)
        db = root / "state.sqlite3"
        actions_path = root / "manual-review-actions.json"
        seed_queue(db)
        queue_raw = {"series": "Series", "issue": "1", "queue_identity": "series:1"}
        with inkdrop_state.connect(db) as con:
            con.execute(
                "update queue_items set state='verified', active=0, raw_json=? where id='queue:1'",
                (json.dumps(queue_raw),),
            )
        retry = {
            "review_id": "review:verified",
            "series": "Series",
            "issue": "1",
            "queue_identity": "series:1",
            "autopilot_queue_key": "queue:1",
            "next_retry_after": 0,
            "ts": resolver.now(),
        }
        actions = {"manual_source_retry_pending": {"review:verified": retry}}
        actions_path.write_text(json.dumps(actions), encoding="utf-8")
        original_db = resolver.INKDROP_STATE_DB
        original_actions = resolver.ACTIONS_FILE
        original_probe = resolver.run_next_slskd_autopick
        try:
            resolver.INKDROP_STATE_DB = db
            resolver.ACTIONS_FILE = actions_path
            resolver.run_next_slskd_autopick = lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("verified durable queue reached SLSKD retry")
            )
            require(resolver.durable_autopilot_queue_item_verified(retry), "exact durable verified identity was not fenced")
            require(
                not resolver.durable_autopilot_queue_item_verified({**retry, "issue": "2"}),
                "mismatched issue was accepted as durable verified proof",
            )
            result = {"skipped": []}
            rows = resolver.process_pending_slskd_retries(SimpleNamespace(live=True), result, actions, limit=1)
            require(rows == [], "verified durable retry emitted a retry row")
            saved = json.loads(actions_path.read_text(encoding="utf-8"))
            require(not saved.get("manual_source_retry_pending"), "verified durable retry was not cleared")
            history = saved.get("manual_source_retry_pending_cleared") or []
            require(history and history[-1].get("reason") == "durable_queue_verified", "verified retry clear audit missing")
        finally:
            resolver.INKDROP_STATE_DB = original_db
            resolver.ACTIONS_FILE = original_actions
            resolver.run_next_slskd_autopick = original_probe


def completed_work_precedes_replacement_search_smoke():
    with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-reconcile-before-retry-") as temp:
        root = Path(temp)
        actions_path = root / "manual-review-actions.json"
        db_path = root / "missing-state.sqlite3"
        original_values = {
            "ACTIONS_FILE": resolver.ACTIONS_FILE,
            "STATUS_FILE": resolver.STATUS_FILE,
            "INKDROP_STATE_DB": resolver.INKDROP_STATE_DB,
            "load_probe_module": resolver.load_probe_module,
            "load_configured_probe_module": resolver.load_configured_probe_module,
            "autopilot_queue_items": resolver.autopilot_queue_items,
            "clear_verified_action_records": resolver.clear_verified_action_records,
            "prune_verified_probe_records": resolver.prune_verified_probe_records,
            "db_import_retry_records": resolver.db_import_retry_records,
            "slskd_stall_policy": resolver.slskd_stall_policy,
            "reconcile_terminal_false_duplicate_review_attempts": resolver.reconcile_terminal_false_duplicate_review_attempts,
            "slskd_download_transfers": resolver.slskd_download_transfers,
            "waiting_transfer_status": resolver.waiting_transfer_status,
            "refresh_probe_rows": resolver.refresh_probe_rows,
            "stable_detected_file": resolver.stable_detected_file,
            "auto_import_quality": resolver.auto_import_quality,
            "import_status_pending_for_source": resolver.import_status_pending_for_source,
            "apply_verified_import_status_resolution": resolver.apply_verified_import_status_resolution,
            "cancel_superseded_slskd_transfer": resolver.cancel_superseded_slskd_transfer,
            "record_slskd_learning_from_row": resolver.record_slskd_learning_from_row,
            "sync_autopilot_and_native_queue_from_result": resolver.sync_autopilot_and_native_queue_from_result,
            "publish_progress": resolver.publish_progress,
            "mark_manual_source_candidate_bad": resolver.mark_manual_source_candidate_bad,
            "cancel_failed_slskd_transfer": resolver.cancel_failed_slskd_transfer,
            "run_next_slskd_autopick": resolver.run_next_slskd_autopick,
            "auto_grab_audit": resolver.auto_grab_audit,
            "log": resolver.log,
        }
        calls = {"retry": 0, "terminal_blocking": []}
        try:
            resolver.ACTIONS_FILE = actions_path
            resolver.INKDROP_STATE_DB = db_path
            resolver.load_probe_module = lambda *_args, **_kwargs: SimpleNamespace(
                record_auto_grab_terminal_attempt=(
                    lambda *_args, **kwargs: calls["terminal_blocking"].append(kwargs.get("blocking")) or False
                )
            )
            resolver.mark_manual_source_candidate_bad = (
                lambda review_id, record, detected, reason, transfer=None: {
                    "review_id": review_id,
                    "series": record.get("series"),
                    "issue": record.get("issue"),
                    "candidate_key": "candidate:failed",
                }
            )
            resolver.cancel_failed_slskd_transfer = lambda *_args, **_kwargs: {}
            resolver.auto_grab_audit = lambda *_args, **_kwargs: None

            def unexpected_retry(*_args, **_kwargs):
                calls["retry"] += 1
                raise AssertionError("replacement search ran before completed-work reconciliation")

            resolver.run_next_slskd_autopick = unexpected_retry
            args = SimpleNamespace(
                live=True,
                probe_script=Path("unused.py"),
                _reconcile_existing_work_before_retry=True,
            )
            result = {
                "bad_candidate_count": 0,
                "retry_probe_count": 0,
                "retry_deferred_count": 0,
                "retry_started_count": 0,
                "retry_pending_count": 0,
                "skipped": [],
            }
            record = {
                "review_id": "review:failed",
                "series": "Series",
                "issue": "1",
                "candidate_source": "slskd_probe",
                "queue_identity": "series:1",
            }
            recovery = resolver.recover_failed_waiting_candidate(
                args,
                result,
                "review:failed",
                record,
                None,
                "staged file did not match waiting candidate",
            )
            require(calls["retry"] == 0, calls)
            require(calls["terminal_blocking"] == [False], calls)
            require(result["retry_probe_count"] == 0, result)
            require(result["retry_deferred_count"] == 1, result)
            require((recovery.get("retry_probe") or {}).get("status") == "deferred", recovery)
            actions = json.loads(actions_path.read_text(encoding="utf-8"))
            pending = (actions.get("manual_source_retry_pending") or {}).get("review:failed") or {}
            require(pending.get("blocked_by") == "completed_work_reconciliation", pending)
            require(pending.get("next_retry_after"), pending)

            lock_modes = []
            with mock.patch.object(
                probe,
                "acquire_auto_grab_state_lock",
                side_effect=lambda blocking=True: lock_modes.append(blocking) or None,
            ):
                recorded = probe.record_auto_grab_terminal_attempt(
                    "review:failed",
                    record,
                    "transfer_failed",
                    "candidate failed",
                    blocking=False,
                )
            require(recorded is False, recorded)
            require(lock_modes == [False], lock_modes)

            resolver.run_next_slskd_autopick = lambda *_args, **_kwargs: (
                calls.__setitem__("retry", calls["retry"] + 1)
                or {"started": True, "started_count": 1, "status": "ok"}
            )
            rows = resolver.process_pending_slskd_retries(args, result, actions, limit=1)
            require(calls["retry"] == 1, calls)
            require(len(rows) == 1 and rows[0].get("status") == "retry_started_after_failure", rows)
            require(result["retry_probe_count"] == 1, result)
            saved = json.loads(actions_path.read_text(encoding="utf-8"))
            require(not saved.get("manual_source_retry_pending"), saved)

            # Exercise the resolver entry point: completed work must be handled
            # before one replacement search, and a bounded import overflow must
            # leave the retry pending without searching.
            resolver.STATUS_FILE = root / "resolver-status.json"
            resolver.autopilot_queue_items = lambda: []
            resolver.clear_verified_action_records = lambda *_args, **_kwargs: 0
            resolver.prune_verified_probe_records = lambda *_args, **_kwargs: 0
            resolver.db_import_retry_records = lambda *_args, **_kwargs: {}
            resolver.slskd_stall_policy = lambda *_args, **_kwargs: {}
            resolver.reconcile_terminal_false_duplicate_review_attempts = lambda *_args, **_kwargs: {
                "review_count": 0, "evidence_count": 0, "retired_count": 0, "rows": []
            }
            resolver.load_configured_probe_module = lambda *_args, **_kwargs: (SimpleNamespace(), {"source": "test"})
            resolver.slskd_download_transfers = lambda: {"ok": True, "transfers": []}
            resolver.waiting_transfer_status = lambda *_args, **_kwargs: None
            resolver.stable_detected_file = lambda *_args, **_kwargs: (True, "stable")
            resolver.auto_import_quality = lambda *_args, **_kwargs: (True, "strong exact match")
            resolver.import_status_pending_for_source = lambda *_args, **_kwargs: None
            resolver.cancel_superseded_slskd_transfer = lambda *_args, **_kwargs: None
            resolver.record_slskd_learning_from_row = lambda *_args, **_kwargs: None
            resolver.sync_autopilot_and_native_queue_from_result = lambda *_args, **_kwargs: None
            resolver.publish_progress = lambda *_args, **_kwargs: None
            resolver.auto_grab_audit = lambda *_args, **_kwargs: None
            resolver.log = lambda *_args, **_kwargs: None

            def run_case(completed_count):
                calls["retry"] = 0
                calls["imports"] = []
                waiting_rows = {
                    f"review:completed:{index}": {
                        "review_id": f"review:completed:{index}",
                        "series": "Completed Series",
                        "issue": str(index),
                        "candidate_source": "slskd_probe",
                    }
                    for index in range(1, completed_count + 1)
                }
                pending_retry = {
                    "review:retry": {
                        "review_id": "review:retry",
                        "series": "Retry Series",
                        "issue": "9",
                        "ts": resolver.now(),
                        "next_retry_after": resolver.now(),
                    }
                }
                actions_path.write_text(json.dumps({
                    "manual_source_waiting": waiting_rows,
                    "manual_source_retry_pending": pending_retry,
                }), encoding="utf-8")
                resolver.refresh_probe_rows = lambda _probe, records: ([
                    {
                        "review_id": review_id,
                        "detected_files": [{"path": str(root / f"{review_id}.cbz"), "filename": f"{review_id}.cbz"}],
                    }
                    for review_id in records
                ], {})

                def resolve_import(_args, _result, row, _path, _pending, review_id, _record, _detected):
                    calls["imports"].append(review_id)
                    row["manual_source_resolved"] = True
                    return True

                resolver.apply_verified_import_status_resolution = resolve_import
                resolver.run_next_slskd_autopick = lambda *_args, **_kwargs: (
                    calls.__setitem__("retry", calls["retry"] + 1)
                    or {"started": True, "started_count": 1, "status": "ok"}
                )
                return resolver.run(SimpleNamespace(
                    live=True,
                    include_ready=False,
                    max_imports=1,
                    min_age_seconds=30,
                    probe_script=Path("unused.py"),
                ))

            reconciled = run_case(1)
            require(calls["imports"] == ["review:completed:1"], calls)
            require(calls["retry"] == 1, calls)
            require(reconciled.get("eligible_deferred_count") == 0, reconciled)

            bounded = run_case(2)
            require(calls["imports"] == ["review:completed:1"], calls)
            require(calls["retry"] == 0, calls)
            require(bounded.get("eligible_deferred_count") == 1, bounded)
            still_pending = json.loads(actions_path.read_text(encoding="utf-8")).get("manual_source_retry_pending") or {}
            require("review:retry" in still_pending, still_pending)
        finally:
            for name, value in original_values.items():
                setattr(resolver, name, value)


def durable_slot_request_lifecycle_smoke():
    def slot_attempt():
        return {
            "source": "slskd",
            "provider_id": "slskd",
            "provider": "peer",
            "protocol": "soulseek",
            "download_client": "SLSKD",
            "status": "waiting_for_slot",
            "lifecycle_phase": "provider_wait",
            "reason": "peer is at the configured transfer limit",
            "title": "Series 001.cbz",
            "filename": "Series 001.cbz",
            "candidate_identity": "slskd:candidate:series-001",
            "retry_eligible": True,
        }

    with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-slot-lifecycle-") as temp:
        db = Path(temp) / "state.sqlite3"
        seed_queue(db)
        with inkdrop_state.connect(db) as con:
            con.execute("update queue_items set state='queued',current_source=null where id='queue:1'")

        first = inkdrop_state.record_slskd_slot_request(
            db, "queue:1", slot_attempt(), requested_at=100.0, retry_seconds=60, ttl_seconds=600,
        )
        require(first.get("ok") and first.get("created"), f"slot wait did not create a durable task: {first}")
        second = inkdrop_state.record_slskd_slot_request(
            db, "queue:1", slot_attempt(), requested_at=101.0, retry_seconds=60, ttl_seconds=600,
        )
        require(second.get("idempotent"), f"repeated slot wait was not idempotent: {second}")
        require(second.get("download_task_id") == first.get("download_task_id"), "repeated slot wait changed task ownership")
        require(second.get("slot_request_retry_at") == first.get("slot_request_retry_at"), "repeated slot wait reset retry timing")

        with inkdrop_state.connect_read(db) as con:
            task = con.execute(
                "select * from download_tasks where id=?", (first.get("download_task_id"),),
            ).fetchone()
            active_count = con.execute(
                "select count(*) from download_tasks where queue_id='queue:1' and state in ('queued','downloading','import_ready','importing')",
            ).fetchone()[0]
        require(task["status"] == "waiting_for_slot" and task["state"] == "queued", "slot wait projected the wrong task state")
        require(not task["external_id"], "slot wait invented an external transfer identity")
        require(active_count == 1, "slot wait created duplicate active tasks")

        started = {**slot_attempt(), "status": "started_waiting", "lifecycle_phase": "downloading", "transfer_id": "transfer:1"}
        progressed = inkdrop_state.record_queue_source_attempt(
            db, "queue:1", started, attempt_id="started:1", started_at=110.0,
        )
        require(progressed.get("ok"), f"real SLSKD handoff did not update the slot task: {progressed}")
        with inkdrop_state.connect_read(db) as con:
            active = con.execute(
                "select id,status,state,external_id from download_tasks where queue_id='queue:1' and state in ('queued','downloading','import_ready','importing')",
            ).fetchall()
        require(len(active) == 1, f"real handoff duplicated slot ownership: {active}")
        require(active[0]["external_id"] == "transfer:1", "authoritative external transfer ID was not captured")
        fenced = inkdrop_state.record_slskd_slot_request(
            db, "queue:1", slot_attempt(), requested_at=120.0, retry_seconds=60, ttl_seconds=600,
        )
        require(fenced.get("reason") == "queue_has_active_candidate_task", f"active transfer did not fence rearming: {fenced}")
        with inkdrop_state.connect(db) as con:
            con.execute("update queue_items set state='queued',active=1 where id='queue:1'")
        candidate_fenced = inkdrop_state.record_slskd_slot_request(
            db, "queue:1", slot_attempt(), requested_at=120.5, retry_seconds=60, ttl_seconds=600,
        )
        require(candidate_fenced.get("reason") == "queue_has_active_candidate_task", f"active candidate task did not fence duplicate ownership: {candidate_fenced}")
        with inkdrop_state.connect(db) as con:
            con.execute("update queue_items set state='importing',active=1 where id='queue:1'")
        import_fenced = inkdrop_state.record_slskd_slot_request(
            db, "queue:1", slot_attempt(), requested_at=121.0, retry_seconds=60, ttl_seconds=600,
        )
        require(import_fenced.get("reason") == "queue_has_active_candidate_task", f"active import did not fence rearming: {import_fenced}")
        verified = {**started, "status": "verified", "lifecycle_phase": "verified"}
        require(
            inkdrop_state.record_queue_source_attempt(
                db, "queue:1", verified, attempt_id="verified:1", started_at=110.0, completed_at=130.0,
            ).get("ok"),
            "verified candidate evidence was not persisted",
        )
        with inkdrop_state.connect(db) as con:
            con.execute("update queue_items set state='queued',active=1,current_source=null where id='queue:1'")
            before_count = con.execute("select count(*) from download_tasks where queue_id='queue:1'").fetchone()[0]
        completion_fenced = inkdrop_state.record_slskd_slot_request(
            db, "queue:1", slot_attempt(), requested_at=140.0, retry_seconds=60, ttl_seconds=600,
        )
        require(completion_fenced.get("reason") == "candidate_completion_fence", f"verified candidate was rearmed: {completion_fenced}")
        with inkdrop_state.connect_read(db) as con:
            after_count = con.execute("select count(*) from download_tasks where queue_id='queue:1'").fetchone()[0]
        require(after_count == before_count, "completion fence created duplicate candidate ownership")

    with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-slot-expiry-") as temp:
        db = Path(temp) / "state.sqlite3"
        seed_queue(db)
        with inkdrop_state.connect(db) as con:
            con.execute("update queue_items set state='queued',current_source=null where id='queue:1'")
        first = inkdrop_state.record_slskd_slot_request(
            db, "queue:1", slot_attempt(), requested_at=100.0, retry_seconds=60, ttl_seconds=60,
        )
        expired = inkdrop_state.record_slskd_slot_request(
            db, "queue:1", slot_attempt(), requested_at=161.0, retry_seconds=60, ttl_seconds=60,
        )
        require(expired.get("expired") and expired.get("status") == "slot_request_expired", f"expired slot ownership was not retired: {expired}")
        with inkdrop_state.connect_read(db) as con:
            queue = con.execute("select state,current_source,retry_after from queue_items where id='queue:1'").fetchone()
            old_task = con.execute("select state,status,retry_eligible from download_tasks where id=?", (first.get("download_task_id"),)).fetchone()
        require(queue["state"] == "queued" and queue["current_source"] is None, "expired slot wait did not return Wanted to queued work")
        require(queue["retry_after"] == 221.0, "expired slot wait did not preserve bounded backoff")
        require(old_task["state"] == "failed" and old_task["status"] == "slot_request_expired" and old_task["retry_eligible"], "expired slot task was not retired safely")
        fresh = inkdrop_state.record_slskd_slot_request(
            db, "queue:1", slot_attempt(), requested_at=222.0, retry_seconds=60, ttl_seconds=60,
        )
        require(fresh.get("created") and fresh.get("download_task_id") != first.get("download_task_id"), "retry did not create one fresh slot task")
        again = inkdrop_state.record_slskd_slot_request(
            db, "queue:1", slot_attempt(), requested_at=223.0, retry_seconds=60, ttl_seconds=60,
        )
        require(again.get("idempotent") and again.get("download_task_id") == fresh.get("download_task_id"), "fresh retry duplicated active ownership")
        failed = inkdrop_state.transition_slskd_candidate_task(
            db, "queue:1", fresh["reservation_id"], "transfer_failed",
            reason="provider reported terminal transfer failure", observed_at=224.0,
        )
        require(failed.get("ok") and failed.get("download_task_id") == fresh.get("download_task_id"), failed)
        terminal_retry = inkdrop_state.record_slskd_slot_request(
            db, "queue:1", slot_attempt(), requested_at=405.0, retry_seconds=60, ttl_seconds=60,
        )
        require(terminal_retry.get("created") and terminal_retry.get("download_task_id") != fresh.get("download_task_id"), "terminal task blocked retry")
        terminal_retry_replay = inkdrop_state.record_slskd_slot_request(
            db, "queue:1", slot_attempt(), requested_at=406.0, retry_seconds=60, ttl_seconds=60,
        )
        require(terminal_retry_replay.get("idempotent") and terminal_retry_replay.get("download_task_id") == terminal_retry.get("download_task_id"), "retained candidate retry created more than one task")
        with inkdrop_state.connect_read(db) as con:
            active_count = con.execute(
                "select count(*) from download_tasks where queue_id='queue:1' and state in ('queued','downloading','import_ready','importing')",
            ).fetchone()[0]
        require(active_count == 1, "expiry and retry left duplicate active tasks")

    with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-sibling-unit-") as temp:
        db = Path(temp) / "state.sqlite3"
        seed_queue(db)
        with inkdrop_state.connect(db) as con:
            con.execute("insert into wanted_items(id,series_id,issue_id,status,created_at,updated_at,raw_json) values('wanted:sibling','series:1','issue:1','wanted',1,1,'{}')")
            sibling_raw = json.dumps({"series": "Series", "issue": "1", "queue_identity": "series:1", "unit_type": "issue", "issue_number": "1"})
            con.execute("insert into queue_items(id,wanted_id,series_id,issue_id,state,active,created_at,updated_at,raw_json) values('queue:sibling','wanted:sibling','series:1','issue:1','queued',1,1,1,?)", (sibling_raw,))
        owner = inkdrop_state.record_slskd_slot_request(db, "queue:1", slot_attempt(), requested_at=100.0)
        with inkdrop_state.connect(db) as con:
            for number in range(1001):
                con.execute(
                    "insert into download_tasks(id,source,download_client,candidate_identity,title,status,state,lifecycle_phase,started_at,updated_at,completed_at,raw_json) values(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"terminal-noise:{number}", "slskd", "SLSKD", f"noise:{number}", f"Noise {number}.cbz", "transfer_failed", "failed", "failed_candidate", 1000 + number, 1000 + number, 1000 + number, "{}"),
                )
        sibling_attempt = {**slot_attempt(), "candidate_identity": "slskd:candidate:sibling", "filename": "Series 001 alternate.cbz", "title": "Series 001 alternate.cbz"}
        sibling = inkdrop_state.record_slskd_slot_request(db, "queue:sibling", sibling_attempt, requested_at=101.0)
        require(owner.get("created") and sibling.get("reason") == "sibling_exact_unit_active" and sibling.get("owner_queue_id") == "queue:1", f"old sibling owner behind more than 1000 terminal tasks was missed: {sibling}")

    with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-durable-identity-claim-") as temp:
        db = Path(temp) / "state.sqlite3"
        seed_queue(db)
        with inkdrop_state.connect(db) as con:
            con.execute("update queue_items set state='queued',current_source=null where id='queue:1'")
        direct_attempt = {
            **slot_attempt(),
            "candidate_instance_identity": "instance:direct",
            "candidate_locator_digest": "sha256:direct",
            "candidate_safe": True,
            "unit_type": "issue",
            "issue_number": "999",
        }
        contradiction = inkdrop_state.reserve_slskd_candidate(db, "queue:1", direct_attempt, requested_at=100.0)
        require(not contradiction.get("ok") and contradiction.get("reason") == "candidate_unit_contradicts_durable_identity", contradiction)
        direct_attempt["issue_number"] = "1"
        reserved = inkdrop_state.reserve_slskd_candidate(
            db, "queue:1", direct_attempt, requested_at=100.0, claim_owner_id="claim:expired", claim_seconds=15,
        )
        require(reserved.get("ok") and reserved.get("created"), reserved)
        expired_claim = inkdrop_state.transition_slskd_candidate_task(
            db, "queue:1", reserved["reservation_id"], "started_waiting",
            transfer_id="transfer:must-not-bind", observed_at=116.0, claim_owner_id="claim:expired",
        )
        require(not expired_claim.get("ok") and expired_claim.get("reason") == "candidate_reservation_claim_lost", expired_claim)
        with inkdrop_state.connect_read(db) as con:
            untouched = con.execute("select status,external_id from download_tasks where id=?", (reserved["download_task_id"],)).fetchone()
        require(untouched["status"] == "waiting_for_slot" and not untouched["external_id"], "expired claim authorized a transition")

    with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-slot-invalid-") as temp:
        db = Path(temp) / "state.sqlite3"
        seed_queue(db)
        invalid = slot_attempt()
        for key in ("candidate_identity", "provider", "title", "filename"):
            invalid.pop(key, None)
        result = inkdrop_state.record_slskd_slot_request(db, "queue:1", invalid, requested_at=100.0)
        require(not result.get("ok") and result.get("reason") == "candidate_reservation_identity_incomplete", "incomplete candidate identity was accepted")
        with inkdrop_state.connect_read(db) as con:
            require(con.execute("select count(*) from download_tasks").fetchone()[0] == 0, "failed slot persistence left stale ownership")


def durable_manga_volume_binding_smoke():
    require(
        not inkdrop_state.slskd_durable_manga_volume_type(
            {"media_type": "comic"}, {}, {"title": "Vol. 13", "normalized_number": "0013"},
            {"issue": "13", "chapter": "13"},
        ),
        "western issue metadata was inferred as a manga volume",
    )
    require(
        not inkdrop_state.slskd_durable_manga_volume_type(
            {"media_type": "manga"}, {}, {"title": "Vol. 12", "normalized_number": "0013"},
            {"issue": "13", "chapter": "13"},
        ),
        "mismatched volume title was accepted as durable identity",
    )
    def seed_manga_volume_queue(db, queue_raw, normalized_number="0013"):
        with inkdrop_state.connect(db) as con:
            inkdrop_state.init_schema(con)
            con.execute(
                "insert into series(id,title,media_type,monitored,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?)",
                ("series:manga", "Legacy Manga", "manga", 1, 100.0, 100.0, json.dumps({"media_type": "manga"})),
            )
            con.execute(
                "insert into issues(id,series_id,issue_number,normalized_number,title,monitored,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?)",
                ("issue:manga:13", "series:manga", "13", normalized_number, "Vol. 13", 1, 100.0, 100.0, "{}"),
            )
            con.execute(
                "insert into wanted_items(id,series_id,issue_id,status,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?)",
                ("wanted:manga:13", "series:manga", "issue:manga:13", "wanted", 100.0, 100.0, "{}"),
            )
            con.execute(
                "insert into queue_items(id,wanted_id,series_id,issue_id,state,active,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?)",
                (
                    "queue:manga:13", "wanted:manga:13", "series:manga", "issue:manga:13", "queued", 1, 100.0, 100.0,
                    json.dumps(queue_raw),
                ),
            )

    attempt = {
        "source": "slskd", "provider_id": "slskd", "provider": "peer", "protocol": "soulseek",
        "download_client": "SLSKD", "status": "waiting_for_slot", "title": "Legacy Manga v13.cbz",
        "filename": "Legacy Manga v13.cbz", "candidate_identity": "slskd:candidate:manga:13",
        "candidate_instance_identity": "instance:manga:13", "candidate_locator_digest": "sha256:manga:13",
        "candidate_safe": True, "unit_type": "volume", "volume_number": "13",
        "series_id": "series:manga", "queue_identity": "series:manga",
    }
    valid_queue_raw = {
        "series": "Legacy Manga", "issue": "13", "chapter": "13",
        "media_type": "manga", "queue_identity": "series:manga",
    }
    with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-durable-manga-volume-") as temp:
        db = Path(temp) / "state.sqlite3"
        seed_manga_volume_queue(db, valid_queue_raw)
        reserved = inkdrop_state.reserve_slskd_candidate(db, "queue:manga:13", attempt, requested_at=100.0)
        require(reserved.get("ok") and reserved.get("created"), reserved)
        replay = inkdrop_state.reserve_slskd_candidate(db, "queue:manga:13", attempt, requested_at=101.0)
        require(replay.get("ok") and replay.get("idempotent"), replay)
        with inkdrop_state.connect_read(db) as con:
            tasks = con.execute("select raw_json from download_tasks where queue_id='queue:manga:13'").fetchall()
        require(len(tasks) == 1, f"manga volume reservation duplicated tasks: {len(tasks)}")
        task_raw = json.loads(tasks[0]["raw_json"])
        require(task_raw.get("exact_unit_type") == "volume" and task_raw.get("exact_unit_number") == "0013", task_raw)
    for chapter_alias in (None, "12"):
        with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-durable-manga-volume-negative-") as temp:
            db = Path(temp) / "state.sqlite3"
            queue_raw = dict(valid_queue_raw)
            if chapter_alias is None:
                queue_raw.pop("chapter")
            else:
                queue_raw["chapter"] = chapter_alias
            seed_manga_volume_queue(db, queue_raw)
            blocked = inkdrop_state.reserve_slskd_candidate(db, "queue:manga:13", attempt, requested_at=100.0)
            require(
                not blocked.get("ok") and blocked.get("reason") == "candidate_unit_contradicts_durable_identity",
                f"missing/conflicting chapter alias authorized a volume reservation: {blocked}",
            )
    with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-durable-manga-volume-no-normalized-") as temp:
        db = Path(temp) / "state.sqlite3"
        seed_manga_volume_queue(db, valid_queue_raw, normalized_number=None)
        blocked = inkdrop_state.reserve_slskd_candidate(db, "queue:manga:13", attempt, requested_at=100.0)
        require(
            not blocked.get("ok") and blocked.get("reason") == "candidate_unit_contradicts_durable_identity",
            f"raw issue number replaced missing durable normalized volume identity: {blocked}",
        )


def auto_grab_slot_request_bridge_smoke():
    with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-slot-bridge-") as temp:
        db = Path(temp) / "state.sqlite3"
        seed_queue(db)
        with inkdrop_state.connect(db) as con:
            con.execute("update queue_items set state='queued',current_source=null where id='queue:1'")
            con.execute("update issues set issue_number='12',normalized_number='12',raw_json=? where id='issue:1'", (json.dumps({"unit_type": "issue", "issue_number": "12"}),))
            con.execute("update queue_items set raw_json=? where id='queue:1'", (json.dumps({"series": "Series", "issue": "12", "queue_identity": "series:1", "unit_type": "issue", "issue_number": "12"}),))
        entry = {
            "review_id": "review:dorohedoro:12",
            "series": "Dorohedoro",
            "issue": "12",
            "query": "Dorohedoro manga",
            "autopilot_queue": True,
            "autopilot_queue_key": "queue:1",
            "queue_identity": "series:1",
        }
        candidate = {
            "filename": r"Literature\Manga (English)\Dorohedoro v01-23 [digital]\v12.zip",
            "username": "peer-with-free-slot",
            "size": 94160000,
            "score": 91,
            "auto_grab": {"verdict": "auto_grab_safe"},
        }
        original_db = probe.INKDROP_STATE_DB
        original = {}
        names = (
            "load_auto_grab_state", "save_auto_grab_state", "auto_grab_review_rows",
            "active_auto_grab_user_load", "auto_grab_audit", "log",
        )
        for name in names:
            original[name] = getattr(probe, name)
        audits = []
        try:
            probe.INKDROP_STATE_DB = db
            probe.load_auto_grab_state = lambda: {}
            probe.save_auto_grab_state = lambda state: None
            probe.auto_grab_review_rows = lambda result, state=None: ([
                (entry["review_id"], entry, candidate)
            ], [], [])
            probe.active_auto_grab_user_load = lambda: {probe.normalize(candidate["username"]): probe.AUTO_GRAB_MAX_ACTIVE_PER_USER}
            probe.auto_grab_audit = lambda event, **payload: audits.append({"event": event, **payload})
            probe.log = lambda *args, **kwargs: None
            outcome = probe._run_auto_grab_with_ephemeral_candidates(
                SimpleNamespace(auto_grab_live=True, auto_grab_dry_run=False, auto_grab_max=1),
                {"items": {entry["review_id"]: entry}},
            )
            recorded = probe.record_slskd_queue_attempt(
                entry,
                candidate,
                "started_waiting",
                "SLSKD started exact candidate",
                transfer={"id": "transfer:dorohedoro:12", "state": "InProgress"},
            )
        finally:
            probe.INKDROP_STATE_DB = original_db
            for name, value in original.items():
                setattr(probe, name, value)
        require(outcome["user_load_skipped_count"] == 1, f"user-load branch was not exercised: {outcome}")
        slot_row = outcome["user_load_skipped"][0]
        require(slot_row.get("status") == "waiting_for_slot", f"capacity wait did not persist a slot request: {slot_row}")
        require((slot_row.get("slot_request") or {}).get("download_task_id"), "capacity wait has no durable task identity")
        require(recorded.get("ok"), f"real SLSKD result recorder did not progress the slot task: {recorded}")
        public = json.dumps({"outcome": outcome, "audits": audits}, sort_keys=True)
        require("_slot_entry" not in public and "_slot_candidate" not in public, "private handoff context escaped the persistence boundary")
        require(r"Literature\Manga (English)" not in public, "private SLSKD locator escaped public output")
        with inkdrop_state.connect_read(db) as con:
            tasks = con.execute("select * from download_tasks where queue_id='queue:1'").fetchall()
            durable_evidence = "\n".join(
                str(row[0] or "")
                for row in con.execute(
                    "select raw_json from source_attempts union all select raw_json from download_tasks union all select raw_json from history_events",
                ).fetchall()
            )
        require(len(tasks) == 1, f"real SLSKD result recorder duplicated durable tasks: {tasks}")
        task = tasks[0]
        require(task["status"] == "started_waiting" and task["external_id"] == "transfer:dorohedoro:12", "real handoff did not capture authoritative transfer ownership")
        require(task["candidate_identity"], "durable slot task lost exact candidate identity")
        require(r"Literature\Manga (English)" not in durable_evidence, "private SLSKD locator escaped durable evidence")
        require(autopilot.slskd_user_load_limited({"last_slskd_autopick_status": "waiting_for_slot", "last_slskd_auto_grab_safe_count": 1}), "new slot status bypassed the scheduler's user-load branch")

    with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-slot-autopilot-expiry-") as temp:
        db = Path(temp) / "state.sqlite3"
        seed_queue(db)
        with inkdrop_state.connect(db) as con:
            con.execute("update queue_items set state='queued',current_source=null where id='queue:1'")
            con.execute("update issues set issue_number='12',normalized_number='12',raw_json=? where id='issue:1'", (json.dumps({"unit_type": "issue", "issue_number": "12"}),))
            con.execute("update queue_items set raw_json=? where id='queue:1'", (json.dumps({"series": "Series", "issue": "12", "queue_identity": "series:1", "unit_type": "issue", "issue_number": "12"}),))
        original_db = probe.INKDROP_STATE_DB
        original = {}
        names = (
            "load_auto_grab_state", "save_auto_grab_state", "auto_grab_review_rows",
            "active_auto_grab_user_load", "auto_grab_audit", "log",
        )
        for name in names:
            original[name] = getattr(probe, name)
        try:
            probe.INKDROP_STATE_DB = db
            probe.load_auto_grab_state = lambda: {}
            probe.save_auto_grab_state = lambda state: None
            probe.auto_grab_review_rows = lambda result, state=None: ([(entry["review_id"], entry, candidate)], [], [])
            probe.active_auto_grab_user_load = lambda: {probe.normalize(candidate["username"]): probe.AUTO_GRAB_MAX_ACTIVE_PER_USER}
            probe.auto_grab_audit = lambda *args, **kwargs: None
            probe.log = lambda *args, **kwargs: None
            first = probe._run_auto_grab_with_ephemeral_candidates(
                SimpleNamespace(auto_grab_live=True, auto_grab_dry_run=False, auto_grab_max=1),
                {"items": {entry["review_id"]: entry}},
            )
            with inkdrop_state.connect(db) as con:
                row = con.execute("select id,raw_json from download_tasks where queue_id='queue:1'").fetchone()
                raw = json.loads(row["raw_json"])
                raw["slot_request_deadline"] = 0
                raw["reservation_deadline"] = 0
                con.execute("update download_tasks set raw_json=? where id=?", (json.dumps(raw), row["id"]))
            expired = probe._run_auto_grab_with_ephemeral_candidates(
                SimpleNamespace(auto_grab_live=True, auto_grab_dry_run=False, auto_grab_max=1),
                {"items": {entry["review_id"]: entry}},
            )
            queue = {"items": {"fixture": {"series": "Dorohedoro", "issue": "12", "present_in_watch": True}}}
            autopilot.apply_slskd_auto_grab(queue, {"auto_grab": expired})
            with inkdrop_state.connect(db) as con:
                con.execute("update queue_items set retry_after=0,retry_after_iso=null where id='queue:1'")
            fresh = probe._run_auto_grab_with_ephemeral_candidates(
                SimpleNamespace(auto_grab_live=True, auto_grab_dry_run=False, auto_grab_max=1),
                {"items": {entry["review_id"]: entry}},
            )
        finally:
            probe.INKDROP_STATE_DB = original_db
            for name, value in original.items():
                setattr(probe, name, value)
        require(first["user_load_skipped"][0].get("status") == "waiting_for_slot", "real capacity path did not create the initial slot task")
        expired_row = expired["user_load_skipped"][0]
        require(expired_row.get("status") == "slot_request_expired", f"real capacity path did not retire expired ownership: {expired_row}")
        item = queue["items"]["fixture"]
        require(item.get("state") == "queued" and item.get("last_slskd_autopick_status") == "slot_request_expired", "autopilot did not project expiry as retryable work")
        require(fresh["user_load_skipped"][0].get("status") == "waiting_for_slot", "normal retry did not create one fresh slot task")
        with inkdrop_state.connect_read(db) as con:
            active_count = con.execute(
                "select count(*) from download_tasks where queue_id='queue:1' and state in ('queued','downloading','import_ready','importing')",
            ).fetchone()[0]
        require(active_count == 1, "real expiry/retry path left duplicate active ownership")


def staged_file_path_durability_smoke():
    with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-staged-path-") as temp:
        root = Path(temp)
        db = root / "state.sqlite3"
        queue_file = root / "series-autopilot-queue.json"
        staged = root / "completed" / "Love and Rockets" / "Love & Rockets #16.zip"
        staged.parent.mkdir(parents=True)
        staged.write_bytes(b"staged fixture")
        seed_queue(db)
        entry = {
            "review_id": "review:staged:16",
            "series": "Series",
            "issue": "1",
            "query": "Series comic",
            "autopilot_queue": True,
            "autopilot_queue_key": "queue:1",
            "queue_identity": "series:1",
        }
        candidate = {
            "filename": staged.name,
            "username": "SLSKD",
            "size": staged.stat().st_size,
            "score": 95,
            "auto_grab": {"verdict": "auto_grab_safe"},
        }
        queue_file.write_text(
            json.dumps({"items": {"queue:1": {
                "series": "Series",
                "issue": "1",
                "query": "Series comic",
                "review_id": entry["review_id"],
                "queue_identity": "series:1",
                "state": "downloading",
            }}}),
            encoding="utf-8",
        )
        original_db = probe.INKDROP_STATE_DB
        original_resolver_db = resolver.INKDROP_STATE_DB
        original_queue = probe.SERIES_AUTOPILOT_QUEUE_FILE
        original_export = probe.export_autopilot_queue_from_inkdrop_state
        try:
            probe.INKDROP_STATE_DB = db
            resolver.INKDROP_STATE_DB = db
            probe.SERIES_AUTOPILOT_QUEUE_FILE = queue_file
            probe.export_autopilot_queue_from_inkdrop_state = lambda *_args, **_kwargs: {"ok": True}
            with inkdrop_state.connect(db) as con:
                con.execute("update queue_items set state='queued',current_source=null where id='queue:1'")
            reserved = probe.reserve_slskd_candidate(entry, candidate, "exact staged fixture")
            require(reserved.get("ok") and reserved.get("created"), f"real SLSKD reservation failed: {reserved}")
            task_id = reserved["download_task_id"]
            started = probe.record_slskd_queue_attempt(
                entry,
                candidate,
                "started_waiting",
                "SLSKD started exact candidate",
                transfer={"id": "transfer:staged:16", "state": "InProgress"},
            )
            require(started.get("ok"), f"real SLSKD transfer transition failed: {started}")
            result = probe.update_autopilot_queue_from_staged_entry(
                entry,
                {
                    "path": str(staged),
                    "filename": staged.name,
                    "mtime": staged.stat().st_mtime,
                    "size": staged.stat().st_size,
                },
            )
            importer_records = resolver.db_import_retry_records(0)
        finally:
            probe.INKDROP_STATE_DB = original_db
            resolver.INKDROP_STATE_DB = original_resolver_db
            probe.SERIES_AUTOPILOT_QUEUE_FILE = original_queue
            probe.export_autopilot_queue_from_inkdrop_state = original_export
        require(result.get("updated"), f"real staged-file transition did not update the queue: {result}")
        with inkdrop_state.connect_read(db) as con:
            tasks = [dict(row) for row in con.execute("select * from download_tasks where queue_id='queue:1'").fetchall()]
            attempts = [dict(row) for row in con.execute("select * from source_attempts where queue_id='queue:1' and status='staged_file_ready'").fetchall()]
        require(len(tasks) == 1, f"staged-file transition created duplicate task ownership: {tasks}")
        require(len(attempts) == 1, f"staged-file transition did not persist one source attempt: {attempts}")
        task = tasks[0]
        require(task["id"] == task_id, f"staged-file transition replaced the authoritative task: {tasks}")
        require(task["state"] == "import_ready" and task["status"] == "staged_file_ready", task)
        require(task["local_path"] == str(staged), f"staged task lost its exact local import path: {task}")
        require(Path(inkdrop_state.download_task_import_path(task)) == staged, "importer cannot recover the staged artifact path")
        require(task["external_id"] == "transfer:staged:16", "staged transition lost the authoritative SLSKD transfer ID")
        require(json.loads(task["raw_json"]).get("local_path") == str(staged), "durable task evidence lost the local staged path")
        require(json.loads(attempts[0]["raw_json"]).get("local_path") == str(staged), "durable source evidence lost the local staged path")
        require(len(importer_records) == 1, f"real importer did not claim exactly one staged task: {importer_records}")
        importer_record = next(iter(importer_records.values()))
        require(importer_record.get("db_download_task_id") == task_id, f"importer claimed the wrong task: {importer_record}")
        require(Path(importer_record.get("local_path") or "") == staged, f"importer lost the exact staged path: {importer_record}")
        durable = json.dumps({"tasks": tasks, "attempts": attempts}, sort_keys=True)
        require("private-peer-path" not in durable, "private SLSKD locator escaped durable evidence")


def stale_staged_match_memory_revalidation_smoke():
    with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-staged-memory-") as temp:
        root = Path(temp)
        staged = root / "Fairy Tail" / "Fairy Tail v26 (2013) (Digital).cbz"
        staged.parent.mkdir(parents=True)
        staged.write_bytes(b"current exact volume fixture")
        candidate = {
            "source": "slskd_downloads",
            "root": root,
            "path": staged,
            "filename": staged.name,
            "size": staged.stat().st_size,
            "mtime": staged.stat().st_mtime,
            "extension": ".cbz",
        }
        item = probe.queue_source_review_item({
            "review_id": "review:fairy-tail:26",
            "series": "Fairy Tail",
            "issue": "26",
            "query": "Fairy Tail Vol. 26",
            "media_type": "manga",
            "unit_type": "volume",
            "volume_number": "26",
            "last_slskd_user": "current-peer",
        })
        require(item and item.get("username") == "current-peer", f"queue item lost SLSKD ownership: {item}")
        item["review_id"] = "review:fairy-tail:26"
        stale_match_memory = {
            "reason": "staged_file_low_confidence",
            "failure_kind": "match",
            "detail": "older matcher lacked volume evidence",
        }
        with (
            mock.patch.object(probe, "scan_staged_file_candidates", return_value=[candidate]),
            mock.patch.object(probe, "matching_bad_candidate_rows", return_value=[stale_match_memory]),
            mock.patch.object(probe, "durable_bad_source_candidate_match", return_value=None),
        ):
            detected = probe.detected_staged_files(item, review_id=item["review_id"])
        require(len(detected) == 1 and detected[0]["path"] == str(staged), detected)

        durable_identity_failure = {
            "reason": "verification_identity_mismatch",
            "failure_kind": "identity",
            "detail": "verified wrong artifact",
        }
        with (
            mock.patch.object(probe, "scan_staged_file_candidates", return_value=[candidate]),
            mock.patch.object(probe, "matching_bad_candidate_rows", return_value=[durable_identity_failure]),
            mock.patch.object(probe, "durable_bad_source_candidate_match", return_value=None),
        ):
            require(not probe.detected_staged_files(item, review_id=item["review_id"]), "identity failure was bypassed")

        wrong_work = {**candidate, "path": root / "Batman 026.cbz", "filename": "Batman 026.cbz"}
        with (
            mock.patch.object(probe, "scan_staged_file_candidates", return_value=[wrong_work]),
            mock.patch.object(probe, "matching_bad_candidate_rows", return_value=[stale_match_memory]),
            mock.patch.object(probe, "durable_bad_source_candidate_match", return_value=None),
        ):
            require(not probe.detected_staged_files(item, review_id=item["review_id"]), "wrong work was revalidated")

        for unsafe_name in ("Fairy Tail v26 Preview.cbz", "Fairy Tail v26 Sample.cbz", "Fairy Tail v27.cbz"):
            unsafe_candidate = {**candidate, "path": root / unsafe_name, "filename": unsafe_name}
            with (
                mock.patch.object(probe, "scan_staged_file_candidates", return_value=[unsafe_candidate]),
                mock.patch.object(probe, "matching_bad_candidate_rows", return_value=[stale_match_memory]),
                mock.patch.object(probe, "durable_bad_source_candidate_match", return_value=None),
            ):
                require(not probe.detected_staged_files(item, review_id=item["review_id"]), f"unsafe unit was revalidated: {unsafe_name}")

        with (
            mock.patch.object(probe, "scan_staged_file_candidates", return_value=[candidate]),
            mock.patch.object(probe, "matching_bad_candidate_rows", return_value=[stale_match_memory, durable_identity_failure]),
            mock.patch.object(probe, "durable_bad_source_candidate_match", return_value=None),
        ):
            require(not probe.detected_staged_files(item, review_id=item["review_id"]), "later identity memory was shadowed")

        with (
            mock.patch.object(probe, "scan_staged_file_candidates", return_value=[candidate]),
            mock.patch.object(probe, "matching_bad_candidate_rows", return_value=[stale_match_memory]),
            mock.patch.object(probe, "durable_bad_source_candidate_match", return_value=durable_identity_failure),
        ):
            require(not probe.detected_staged_files(item, review_id=item["review_id"]), "durable source failure was shadowed")

        different_peer_failure = {
            "username": "old-peer",
            "filename_leaf": staged.name,
            "reason": "candidate_failed",
            "failure_kind": "candidate",
            "detail": "unrelated peer returned an unusable payload",
        }
        current_peer_stale_match = {
            **stale_match_memory,
            "username": "current-peer",
            "filename_leaf": staged.name,
        }
        with (
            mock.patch.object(probe, "scan_staged_file_candidates", return_value=[candidate]),
            mock.patch.object(
                probe,
                "manual_source_bad_candidate_rows",
                return_value=[current_peer_stale_match, different_peer_failure],
            ),
            mock.patch.object(probe, "durable_bad_source_candidate_match", return_value=None),
        ):
            detected = probe.detected_staged_files(item, review_id=item["review_id"])
        require(len(detected) == 1, f"unrelated peer history shadowed the authoritative transfer: {detected}")

        same_peer_failure = {**different_peer_failure, "username": "current-peer"}
        with (
            mock.patch.object(probe, "scan_staged_file_candidates", return_value=[candidate]),
            mock.patch.object(
                probe,
                "manual_source_bad_candidate_rows",
                return_value=[current_peer_stale_match, same_peer_failure],
            ),
            mock.patch.object(probe, "durable_bad_source_candidate_match", return_value=None),
        ):
            require(
                not probe.detected_staged_files(item, review_id=item["review_id"]),
                "same-peer failure was bypassed",
            )


def insert_task(db_path, task_id, external_id, title, started_at, progress=None):
    with inkdrop_state.connect(db_path) as con:
        con.execute(
            """
            insert into download_tasks(
                id,queue_id,wanted_id,series_id,source,provider,protocol,download_client,
                external_id,candidate_identity,title,status,state,progress,started_at,updated_at,raw_json
            ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                task_id, "queue:1", "wanted:1", "series:1", "slskd", "peer", "soulseek", "SLSKD",
                external_id, title.lower(), title, "transfer_in_progress", "downloading", progress, started_at, started_at, "{}",
            ),
        )


def durable_terminal_smoke():
    with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-terminal-") as temp:
        db = Path(temp) / "state.sqlite3"
        seed_queue(db)
        insert_task(db, "task:old", "transfer-old", "Series 001.cbz", 100.0)
        attempt = {
            "source": "slskd",
            "protocol": "soulseek",
            "download_client": "SLSKD",
            "status": "transfer_stalled",
            "reason": "zero progress",
            "transfer_id": "transfer-old",
            "candidate_identity": "series 001.cbz",
            "filename": "Series 001.cbz",
        }
        first = inkdrop_state.record_queue_source_attempt(db, "queue:1", attempt, attempt_id="terminal:one", started_at=200.0, completed_at=200.0)
        second = inkdrop_state.record_queue_source_attempt(db, "queue:1", attempt, attempt_id="terminal:one", started_at=200.0, completed_at=200.0)
        require(first.get("ok") and second.get("ok"), "terminal attempt was not idempotently recordable")
        with inkdrop_state.connect_read(db) as con:
            queue = con.execute("select state,current_source,raw_json from queue_items where id='queue:1'").fetchone()
            task = con.execute("select state,status,retry_eligible from download_tasks where id='task:old'").fetchone()
            count = con.execute("select count(*) from source_attempts where id='terminal:one'").fetchone()[0]
        require(queue["state"] == "searching" and queue["current_source"] is None, "terminal recovery did not release SLSKD ownership")
        require(task["state"] == "failed" and task["retry_eligible"], "old durable task was not retired")
        require(count == 1, "terminal retry duplicated durable source attempts")
        raw = json.loads(queue["raw_json"])
        require(raw.get("slskd_terminal_recovery") is True, "terminal recovery marker missing")


def persisted_stall_policy_smoke():
    with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-stall-policy-", ignore_cleanup_errors=True) as temp:
        db = Path(temp) / "state.sqlite3"
        inkdrop_state.sync_settings(
            db,
            settings=[
                {
                    "key": "automation.queue_watchdog_enabled", "scope": "automation", "label": "Queue Watchdog",
                    "value": True, "description": "test", "source": "runtime",
                },
                {
                    "key": resolver.SLSKD_STALL_SETTING_KEY, "scope": "automation", "label": "SLSKD Active Stall Threshold",
                    "value": 45, "description": "test", "source": "runtime",
                },
            ],
        )
        old_environment = os.environ.get("INKDROP_SLSKD_ZERO_PROGRESS_STALE_SECONDS")
        try:
            os.environ.pop("INKDROP_SLSKD_ZERO_PROGRESS_STALE_SECONDS", None)
            default_policy = resolver.slskd_stall_policy(db)
            require(default_policy["minutes"] == 45 and default_policy["source"] == "runtime_default", f"backward-compatible default changed: {default_policy}")

            os.environ["INKDROP_SLSKD_ZERO_PROGRESS_STALE_SECONDS"] = "7200"
            environment_policy = resolver.slskd_stall_policy(db)
            with inkdrop_state.connect(db) as con:
                watchdog_environment_policy = inkdrop_state.queue_watchdog_policy(con)
            active_stall = watchdog_environment_policy["slskd_stall"]
            require(
                environment_policy["seconds"] == 7200
                and watchdog_environment_policy["slskd_stale_seconds"] == 7200
                # resolver.slskd_stall_policy() merges the active-stall policy with the
                # queued-wait policy into one dict for its callers; inkdrop_state keeps
                # them as two separate objects for its own watchdog consumers -- so
                # compare the active-stall fields, then the queued-wait fields, rather
                # than asserting whole-dict equality across the two shapes.
                and all(environment_policy.get(key) == value for key, value in active_stall.items())
                and environment_policy["queued_wait_seconds"] == watchdog_environment_policy["slskd_queued_wait_stale_seconds"],
                f"worker and durable watchdog split effective environment policy: {environment_policy} {watchdog_environment_policy}",
            )

            inkdrop_state.update_app_setting(db, resolver.SLSKD_STALL_SETTING_KEY, 45)
            promoted = inkdrop_state.app_setting(db, resolver.SLSKD_STALL_SETTING_KEY)
            saved_policy = resolver.slskd_stall_policy(db)
            with inkdrop_state.connect(db) as con:
                watchdog_saved_policy = inkdrop_state.queue_watchdog_policy(con)
            require(promoted["source"] == "user", f"unchanged displayed value did not persist user intent: {promoted}")
            require(
                saved_policy["enabled"] and saved_policy["minutes"] == 45 and saved_policy["seconds"] == 45 * 60
                and saved_policy["source"] == "settings" and saved_policy["queued_waiting_excluded"] is True
                and watchdog_saved_policy["slskd_stale_seconds"] == saved_policy["seconds"],
                f"saved SLSKD stall policy was not effective for both consumers: {saved_policy} {watchdog_saved_policy}",
            )
            inkdrop_state.update_app_setting(db, "automation.queue_watchdog_enabled", False)
            require(resolver.slskd_stall_policy(db)["enabled"] is False, "Queue Watchdog disable did not disable active stall gate")
        finally:
            if old_environment is None:
                os.environ.pop("INKDROP_SLSKD_ZERO_PROGRESS_STALE_SECONDS", None)
            else:
                os.environ["INKDROP_SLSKD_ZERO_PROGRESS_STALE_SECONDS"] = old_environment


def durable_stall_cleanup_scope_smoke():
    with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-unknown-progress-cleanup-", ignore_cleanup_errors=True) as temp:
        db = Path(temp) / "state.sqlite3"
        seed_queue(db)
        insert_task(db, "task:unknown", "transfer-unknown", "Series unknown.cbz", 100.0)
        with inkdrop_state.connect(db) as con:
            con.execute(
                "update download_tasks set raw_json=? where id='task:unknown'",
                (json.dumps({"slskd_transfer_cancelled": True}),),
            )
            unknown = dict(con.execute("select * from download_tasks where id='task:unknown'").fetchone())
            require(
                inkdrop_state.slskd_download_task_zero_progress(unknown) is False,
                "missing progress evidence was classified as affirmative zero progress",
            )
            require(
                inkdrop_state.cleanup_stale_active_slskd_download_tasks(con, 10_000.0, 5 * 60) == 0,
                "durable watchdog retired a cancellation-acknowledged task with unknown progress",
            )
        with inkdrop_state.connect_read(db) as con:
            unknown = con.execute("select state,status,progress from download_tasks where id='task:unknown'").fetchone()
        require(
            unknown["state"] == "downloading" and unknown["status"] == "transfer_in_progress" and unknown["progress"] is None,
            "unknown-progress durable task truth changed",
        )

    with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-stall-cleanup-", ignore_cleanup_errors=True) as temp:
        db = Path(temp) / "state.sqlite3"
        seed_queue(db)
        insert_task(db, "task:active", "transfer-active", "Series active.cbz", 100.0, progress=0.0)
        with inkdrop_state.connect(db) as con:
            explicit_zero = dict(con.execute("select * from download_tasks where id='task:active'").fetchone())
            require(
                inkdrop_state.slskd_download_task_zero_progress(explicit_zero) is True,
                "explicit numeric zero was not classified as zero progress",
            )
            require(inkdrop_state.cleanup_stale_active_slskd_download_tasks(con, 699.0, 10 * 60) == 0, "durable task retired below threshold")
            require(
                inkdrop_state.cleanup_stale_active_slskd_download_tasks(con, 700.0, 10 * 60) == 0,
                "durable watchdog released an uncancelled remote transfer at threshold",
            )
        with inkdrop_state.connect_read(db) as con:
            active = con.execute("select state,status,retry_eligible from download_tasks where id='task:active'").fetchone()
        require(active["state"] == "downloading" and not active["retry_eligible"], "uncancelled zero-progress task truth changed")

        with inkdrop_state.connect(db) as con:
            con.execute(
                "update download_tasks set raw_json=? where id='task:active'",
                (json.dumps({"slskd_transfer_cancelled": True}),),
            )
            require(
                inkdrop_state.cleanup_stale_active_slskd_download_tasks(con, 700.0, 10 * 60) == 1,
                "cancel-acknowledged zero-progress durable task did not retire",
            )
        with inkdrop_state.connect_read(db) as con:
            active = con.execute("select state,status,retry_eligible from download_tasks where id='task:active'").fetchone()
        require(active["state"] == "failed" and active["retry_eligible"], "cancel-acknowledged stalled task did not become retry eligible")

    with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-progress-cleanup-", ignore_cleanup_errors=True) as temp:
        db = Path(temp) / "state.sqlite3"
        seed_queue(db)
        insert_task(db, "task:progress", "transfer-progress", "Series progress.cbz", 100.0, progress=50.0)
        with inkdrop_state.connect(db) as con:
            con.execute(
                "update download_tasks set raw_json=? where id='task:progress'",
                (json.dumps({"slskd_transfer_cancelled": True}),),
            )
            require(
                inkdrop_state.cleanup_stale_active_slskd_download_tasks(con, 10_000.0, 5 * 60) == 0,
                "durable watchdog retired an active SLSKD transfer with positive progress",
            )
        with inkdrop_state.connect_read(db) as con:
            progressing = con.execute("select state,status,progress from download_tasks where id='task:progress'").fetchone()
        require(
            progressing["state"] == "downloading" and progressing["status"] == "transfer_in_progress" and progressing["progress"] == 50.0,
            "positive-progress durable task truth changed",
        )

    # started_waiting (and its SLSKD_PRE_TRANSFER_WAIT_STATUSES siblings) is a
    # different failure shape than an active stall: no transfer ever began, so
    # slskd_download_task_zero_progress/cancel_acknowledged can never be
    # satisfied and the 45-minute active-stall window (stale_seconds, the 3rd
    # arg) never applies here at all. It gets its own, much longer
    # never_started_stale_seconds window (the 4th arg; production default 24h
    # via automation.queue_watchdog_slskd_never_started_hours). Below that
    # window an ordinary queued/waiting task is untouched, same as before this
    # fix existed; at or past it, a task with NO recorded transfer evidence
    # releases -- but a task in the same status WITH evidence still needs the
    # same strict guards as an active stall, not a free pass.
    NEVER_STARTED_SECONDS = 24 * 60 * 60

    with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-queue-cleanup-below-threshold-", ignore_cleanup_errors=True) as temp:
        db = Path(temp) / "state.sqlite3"
        seed_queue(db)
        insert_task(db, "task:queued", "transfer-queued", "Series queued.cbz", 100.0)
        with inkdrop_state.connect(db) as con:
            con.execute("update download_tasks set status='started_waiting' where id='task:queued'")
            require(
                inkdrop_state.cleanup_stale_active_slskd_download_tasks(
                    con, 100.0 + NEVER_STARTED_SECONDS - 1, 5 * 60, NEVER_STARTED_SECONDS,
                ) == 0,
                "durable watchdog retired a queued/waiting task before its never-started window elapsed",
            )
        with inkdrop_state.connect_read(db) as con:
            queued = con.execute("select state,status from download_tasks where id='task:queued'").fetchone()
        require(queued["state"] == "downloading" and queued["status"] == "started_waiting", "queued task truth changed below threshold")

    with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-queue-cleanup-no-evidence-", ignore_cleanup_errors=True) as temp:
        db = Path(temp) / "state.sqlite3"
        seed_queue(db)
        insert_task(db, "task:wedged", "transfer-wedged", "Series wedged.cbz", 100.0)
        with inkdrop_state.connect(db) as con:
            con.execute("update download_tasks set status='started_waiting' where id='task:wedged'")
            wedged = dict(con.execute("select * from download_tasks where id='task:wedged'").fetchone())
            require(
                inkdrop_state.slskd_download_task_never_started_transfer(wedged) is True,
                "a started_waiting task with no recorded transfer evidence was not recognized as never-started",
            )
            require(
                inkdrop_state.cleanup_stale_active_slskd_download_tasks(
                    con, 100.0 + NEVER_STARTED_SECONDS, 5 * 60, NEVER_STARTED_SECONDS,
                ) == 1,
                "a genuinely wedged never-started task past its window was not released",
            )
        with inkdrop_state.connect_read(db) as con:
            wedged_after = con.execute("select state,status,retry_eligible from download_tasks where id='task:wedged'").fetchone()
        require(
            wedged_after["state"] == "failed" and wedged_after["retry_eligible"],
            "released never-started task did not become retry eligible",
        )

    with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-queue-cleanup-with-evidence-", ignore_cleanup_errors=True) as temp:
        db = Path(temp) / "state.sqlite3"
        seed_queue(db)
        insert_task(db, "task:started-with-evidence", "transfer-started-with-evidence", "Series started.cbz", 100.0, progress=0.0)
        with inkdrop_state.connect(db) as con:
            con.execute("update download_tasks set status='started_waiting' where id='task:started-with-evidence'")
            with_evidence = dict(con.execute("select * from download_tasks where id='task:started-with-evidence'").fetchone())
            require(
                inkdrop_state.slskd_download_task_never_started_transfer(with_evidence) is False,
                "a started_waiting task with a recorded progress value was wrongly treated as never-started",
            )
            require(
                inkdrop_state.cleanup_stale_active_slskd_download_tasks(
                    con, 100.0 + NEVER_STARTED_SECONDS, 5 * 60, NEVER_STARTED_SECONDS,
                ) == 0,
                "a started_waiting task WITH transfer evidence was released without an acknowledged cancellation -- "
                "the never-started exemption leaked into the strict-guard path",
            )
        with inkdrop_state.connect_read(db) as con:
            with_evidence_after = con.execute(
                "select state,status from download_tasks where id='task:started-with-evidence'"
            ).fetchone()
        require(
            with_evidence_after["state"] == "downloading" and with_evidence_after["status"] == "started_waiting",
            "started_waiting task with evidence changed despite lacking an acknowledged cancellation",
        )


def late_event_fence_smoke():
    with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-late-") as temp:
        db = Path(temp) / "state.sqlite3"
        seed_queue(db)
        insert_task(db, "task:new", "transfer-new", "Series 001 alternate.cbz", 300.0)
        result = inkdrop_state.record_queue_source_attempt(
            db,
            "queue:1",
            {
                "source": "slskd",
                "protocol": "soulseek",
                "download_client": "SLSKD",
                "status": "transfer_failed",
                "reason": "late failure from old transfer",
                "transfer_id": "transfer-old",
                "candidate_identity": "series 001 old.cbz",
                "filename": "Series 001 old.cbz",
            },
            attempt_id="terminal:late-old",
            started_at=400.0,
            completed_at=400.0,
        )
        require(result.get("ok"), "late terminal event record failed")
        with inkdrop_state.connect_read(db) as con:
            queue = con.execute("select state,current_source,raw_json from queue_items where id='queue:1'").fetchone()
            successor = con.execute("select state from download_tasks where id='task:new'").fetchone()
        require(queue["state"] == "downloading" and queue["current_source"] == "slskd", "late old event displaced active successor")
        require(successor["state"] == "downloading", "late old event retired successor")
        require(json.loads(queue["raw_json"]).get("late_slskd_terminal_event_ignored") is True, "late-event fence was not recorded")


def verified_fence_smoke():
    with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-verified-") as temp:
        db = Path(temp) / "state.sqlite3"
        seed_queue(db)
        insert_task(db, "task:stale", "transfer-stale", "Series stale.cbz", 300.0)
        verified_raw = {
            "state": "verified",
            "current_source": None,
            "last_event": "library verified",
            "updated_at": 900.0,
            "updated_at_iso": inkdrop_state.utc_stamp(900.0),
            "completed_at": 850.0,
            "verified_at": 875.0,
            "last_import_status": "library_visible",
            "next_automatic_step": "none_verified",
        }
        with inkdrop_state.connect(db) as con:
            con.execute(
                """
                update queue_items
                set state='verified',current_source=null,active=0,last_event='library verified',
                    updated_at=900.0,raw_json=?
                where id='queue:1'
                """,
                (json.dumps(verified_raw),),
            )
        with inkdrop_state.connect_read(db) as con:
            tasks_before_unmatched = [tuple(row) for row in con.execute("select * from download_tasks order by id").fetchall()]
        result = inkdrop_state.record_queue_source_attempt(
            db,
            "queue:1",
            {
                "source": "slskd", "protocol": "soulseek", "download_client": "SLSKD",
                "status": "transfer_failed", "reason": "late old failure", "transfer_id": "transfer-old",
                "candidate_identity": "series old.cbz", "filename": "Series old.cbz",
            },
            attempt_id="terminal:verified-late",
            started_at=400.0,
            completed_at=400.0,
        )
        require(result.get("ok"), "verified late event could not be audited")
        with inkdrop_state.connect_read(db) as con:
            tasks_after_unmatched = [tuple(row) for row in con.execute("select * from download_tasks order by id").fetchall()]
            unmatched_attempt_count = con.execute("select count(*) from source_attempts where id='terminal:verified-late'").fetchone()[0]
            unmatched_history_count = con.execute("select count(*) from history_events where entity_id='terminal:verified-late'").fetchone()[0]
        require(tasks_after_unmatched == tasks_before_unmatched, "unmatched fenced event inserted or mutated a durable task")
        require(unmatched_attempt_count == 1 and unmatched_history_count == 1, "unmatched fenced event did not retain exactly one attempt and history audit")
        result = inkdrop_state.record_queue_source_attempt(
            db,
            "queue:1",
            {
                "source": "slskd", "protocol": "soulseek", "download_client": "SLSKD",
                "status": "transfer_stalled", "reason": "late matching failure", "transfer_id": "transfer-stale",
                "candidate_identity": "series stale.cbz", "filename": "Series stale.cbz",
            },
            attempt_id="terminal:verified-matching",
            started_at=425.0,
            completed_at=425.0,
        )
        require(result.get("ok"), "matching verified late event could not be audited")
        with inkdrop_state.connect_read(db) as con:
            queue = con.execute("select state,current_source,active,last_event,updated_at,raw_json from queue_items where id='queue:1'").fetchone()
            tasks_after_matching = [tuple(row) for row in con.execute("select * from download_tasks order by id").fetchall()]
            matching_attempt_count = con.execute("select count(*) from source_attempts where id='terminal:verified-matching'").fetchone()[0]
            matching_history_count = con.execute("select count(*) from history_events where entity_id='terminal:verified-matching'").fetchone()[0]
        require(queue["state"] == "verified" and queue["active"] == 0, "late terminal event reactivated verified queue")
        require(queue["current_source"] is None and queue["last_event"] == "library verified", "verified fence did not preserve terminal truth")
        require(queue["updated_at"] == 900.0, "older late event regressed verified queue updated_at")
        require(tasks_after_matching == tasks_before_unmatched, "matching fenced event mutated the durable task set")
        require(matching_attempt_count == 1 and matching_history_count == 1, "matching fenced event did not retain exactly one attempt and history audit")
        raw = json.loads(queue["raw_json"])
        require(raw.get("state") == "verified" and raw.get("current_source") is None, "verified raw queue truth changed")
        require(raw.get("last_event") == "library verified" and raw.get("updated_at") == 900.0, "verified raw audit truth changed")
        require(raw.get("completed_at") == 850.0, "late event rewrote verified completion evidence")
        require(raw.get("verified_at") == 875.0, "late event rewrote verification evidence")
        require(raw.get("last_import_status") == "library_visible", "late event rewrote import evidence")
        require(raw.get("next_automatic_step") == "none_verified", "late event rewrote verified retry action")
        require(raw.get("late_slskd_terminal_event_fence") == "inactive_or_terminal_queue", "verified fence evidence missing")
        require(raw.get("late_slskd_terminal_event_at") == 425.0, "verified fence did not retain the older event timestamp as audit evidence")


def direct_failover_smoke(with_alternate, delete_failure=False):
    lifecycle_temp = tempfile.TemporaryDirectory(prefix="inkdrop-slskd-direct-failover-")
    lifecycle_db = Path(lifecycle_temp.name) / "state.sqlite3"
    seed_queue(lifecycle_db)
    with inkdrop_state.connect(lifecycle_db) as con:
        con.execute("update queue_items set state='queued',current_source=null where id='queue:1'")
    first = {"filename": "Series 001 old.cbz", "username": "peer", "size": 1000, "score": 100, "auto_grab": {"verdict": "auto_grab_safe"}}
    alternate = {"filename": "Series 001 alt.cbz", "username": "peer2", "size": 1100, "score": 90, "auto_grab": {"verdict": "auto_grab_safe"}}
    entry = {"review_id": "review:1", "series": "Series", "issue": "1", "autopilot_queue": True, "autopilot_queue_key": "queue:1", "queue_identity": "series:1", "candidates": [first] + ([alternate] if with_alternate else [])}
    deleted = []
    enqueued = []
    strict_path_calls = []
    original = {}
    names = (
        "load_auto_grab_state", "save_auto_grab_state", "auto_grab_review_rows", "select_auto_grab_rows",
        "auto_grab_attempt_allowed", "bad_candidate_match", "mark_manual_source_waiting_local",
        "slskd_existing_download", "slskd_delete_download_transfer", "slskd_enqueue_candidate",
        "auto_grab_transfer_from_enqueue", "mark_probe_candidate_bad", "record_slskd_queue_attempt",
        "record_auto_grab_attempt", "auto_grab_audit", "log",
    )
    for name in names:
        original[name] = getattr(probe, name)
    old_db = probe.INKDROP_STATE_DB
    try:
        probe.INKDROP_STATE_DB = lifecycle_db
        probe.load_auto_grab_state = lambda: {}
        probe.save_auto_grab_state = lambda state: None
        probe.auto_grab_review_rows = lambda result, state=None: ([("review:1", entry, first)], [], [])
        probe.select_auto_grab_rows = lambda rows, max_grabs: (rows, [])
        probe.auto_grab_attempt_allowed = lambda state, review_id, candidate: (True, "", candidate["filename"])
        probe.bad_candidate_match = lambda review_id, candidate: None
        probe.mark_manual_source_waiting_local = lambda *args, **kwargs: {"record": {"review_id": "review:1"}}
        def existing_download(candidate, *, strict_path=False):
            strict_path_calls.append(("existing", candidate, strict_path))
            require(strict_path is bool((candidate.get("auto_grab") or {}).get("auto_inspect_eligible")), "wrong existing-transfer binding mode")
            return {"id": "old", "username": "peer", "state": "Failed", "filename": candidate["filename"]} if candidate is first else None
        probe.slskd_existing_download = existing_download
        probe.slskd_existing_download(
            {"filename": "Series 001 inspect.cbz", "username": "peer", "auto_grab": {"auto_inspect_eligible": True}},
            strict_path=True,
        )
        def delete_transfer(transfer, dry_run):
            deleted.append(transfer["id"])
            if delete_failure:
                raise RuntimeError("delete failed")
            return {"deleted": True}
        probe.slskd_delete_download_transfer = delete_transfer
        probe.slskd_enqueue_candidate = lambda candidate, dry_run: enqueued.append(candidate["filename"]) or {"ok": True}
        def transfer_from_enqueue(response, candidate, *, strict_path=False):
            strict_path_calls.append(("enqueue", candidate, strict_path))
            require(strict_path is bool((candidate.get("auto_grab") or {}).get("auto_inspect_eligible")), "wrong enqueue-transfer binding mode")
            return {"transfer": {"id": "new", "username": candidate["username"], "state": "InProgress", "filename": candidate["filename"]}}
        probe.auto_grab_transfer_from_enqueue = transfer_from_enqueue
        probe.mark_probe_candidate_bad = lambda *args, **kwargs: {"candidate_key": "old"}
        probe.record_slskd_queue_attempt = lambda *args, **kwargs: {"ok": True}
        probe.record_auto_grab_attempt = lambda *args, **kwargs: None
        probe.auto_grab_audit = lambda *args, **kwargs: None
        probe.log = lambda *args, **kwargs: None
        outcome = probe.run_auto_grab(SimpleNamespace(auto_grab_live=True, auto_grab_dry_run=False, auto_grab_max=1), {"items": {"review:1": entry}})
    finally:
        probe.INKDROP_STATE_DB = old_db
        for name, value in original.items():
            setattr(probe, name, value)
        lifecycle_temp.cleanup()
    require(deleted == ["old"], f"terminal transfer must be cancelled exactly once: deleted={deleted} outcome={outcome}")
    require(strict_path_calls, "failover did not exercise transfer lookup binding")
    require(any(strict_path for _kind, _candidate, strict_path in strict_path_calls), "inspection lookup did not require strict path binding")
    if delete_failure:
        require(not enqueued and outcome["started_count"] == 0, "alternate enqueue occurred before terminal delete succeeded")
        return
    if with_alternate:
        require(enqueued == [alternate["filename"]] and outcome["started_count"] == 1, "bounded alternate was not selected exactly once")
    else:
        require(not enqueued and outcome["started_count"] == 0, "no-alternate path unexpectedly enqueued")


def verified_before_handoff_smoke():
    with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-handoff-fence-") as temp:
        db = Path(temp) / "state.sqlite3"
        seed_queue(db)
        queue_raw = {"series": "Series", "issue": "1", "queue_identity": "series:1"}
        with inkdrop_state.connect(db) as con:
            con.execute(
                "update queue_items set state='verified', active=0, raw_json=? where id='queue:1'",
                (json.dumps(queue_raw),),
            )
        entry = {
            "review_id": "review:verified-handoff",
            "series": "Series",
            "issue": "1",
            "queue_identity": "series:1",
            "autopilot_queue": True,
            "autopilot_queue_key": "queue:1",
        }
        candidate = {
            "filename": "Series 001.cbz",
            "username": "peer",
            "size": 80_000_000,
            "score": 100,
            "auto_grab": {"verdict": "auto_grab_safe"},
        }
        original_db = probe.INKDROP_STATE_DB
        original = {}
        names = (
            "load_auto_grab_state", "save_auto_grab_state", "auto_grab_review_rows", "select_auto_grab_rows",
            "auto_grab_attempt_allowed", "bad_candidate_match", "mark_manual_source_waiting_local",
            "slskd_existing_download", "slskd_enqueue_candidate", "slskd_delete_download_transfer", "auto_grab_audit", "log",
        )
        for name in names:
            original[name] = getattr(probe, name)
        enqueued = []
        try:
            probe.INKDROP_STATE_DB = db
            for terminal_state in ("verified", "satisfied", "superseded_duplicate"):
                with inkdrop_state.connect(db) as con:
                    con.execute(
                        "update queue_items set state=?, active=?, raw_json=? where id='queue:1'",
                        (terminal_state, 0, json.dumps(queue_raw)),
                    )
                gate = probe.claim_autopilot_handoff(entry, "candidate:terminal")
                require(not gate.get("allowed") and not gate.get("claimed"), f"{terminal_state} queue remained claimable")
            with inkdrop_state.connect(db) as con:
                con.execute(
                    "update queue_items set state='queued', active=1, raw_json=? where id='queue:1'",
                    (json.dumps(queue_raw),),
                )
            require(
                not probe.claim_autopilot_handoff({**entry, "issue": "2"}, "candidate:mismatch").get("allowed"),
                "mismatched issue acquired a durable handoff claim",
            )
            require(
                not probe.claim_autopilot_handoff({**entry, "issue": ""}, "candidate:incomplete").get("allowed"),
                "identity-less handoff acquired a durable claim",
            )
            require(
                not probe.claim_autopilot_handoff({**entry, "queue_identity": ""}, "candidate:no-identity").get("allowed"),
                "entry without an exact queue identity acquired a durable claim",
            )
            require(
                not probe.claim_autopilot_handoff({**entry, "issue": "1 2"}, "candidate:compound").get("allowed"),
                "compound issue label acquired a durable claim for one overlapping unit",
            )
            with inkdrop_state.connect(db) as con:
                con.execute(
                    "update queue_items set state='queued', active=1, raw_json=? where id='queue:1'",
                    (json.dumps({"series": "Series", "issue": "1"}),),
                )
            require(
                not probe.claim_autopilot_handoff(entry, "candidate:durable-no-identity").get("allowed"),
                "durable row without an exact queue identity acquired a claim",
            )
            with inkdrop_state.connect(db) as con:
                con.execute("update queue_items set raw_json='not-json' where id='queue:1'")
            require(
                not probe.claim_autopilot_handoff(entry, "candidate:malformed").get("allowed"),
                "malformed durable identity acquired a handoff claim",
            )
            with inkdrop_state.connect(db) as con:
                con.execute(
                    "update queue_items set state='queued', active=1, raw_json=? where id='queue:1'",
                    (json.dumps(queue_raw),),
                )
            allowed_claim = probe.claim_autopilot_handoff(entry, "candidate:allowed")
            require(allowed_claim.get("allowed") and allowed_claim.get("claimed"), "exact active queue was not claimable")
            duplicate_claim = probe.claim_autopilot_handoff(entry, "candidate:allowed")
            require(not duplicate_claim.get("allowed"), "concurrent same-candidate attempt shared the existing claim owner")
            require(probe.release_autopilot_handoff_claim(allowed_claim), "exact active queue claim did not release")
            with inkdrop_state.connect(db) as con:
                con.execute(
                    "update queue_items set state='queued', active=1, raw_json=? where id='queue:1'",
                    (json.dumps(queue_raw),),
                )
            probe.load_auto_grab_state = lambda: {}
            probe.save_auto_grab_state = lambda state: None
            probe.auto_grab_review_rows = lambda result, state=None: ([(entry["review_id"], entry, candidate)], [], [])
            probe.select_auto_grab_rows = lambda rows, max_grabs: (rows, [])
            probe.auto_grab_attempt_allowed = lambda state, review_id, row: (True, "", "candidate:1")
            probe.bad_candidate_match = lambda review_id, row: None
            probe.mark_manual_source_waiting_local = lambda *args, **kwargs: {
                "result": {"record": {
                    "review_id": entry["review_id"],
                    "username": "peer-private",
                    "remoteFilename": "Private/Series 001.cbz",
                    "slskd_transfer_id": "waiting-private-id",
                }}
            }
            probe.slskd_existing_download = lambda row, *, strict_path=False: None
            cancelled = []
            def enqueue_then_verify(row, dry_run):
                enqueued.append(row)
                with inkdrop_state.connect(db) as con:
                    con.execute(
                        "update queue_items set state='verified', active=0, raw_json=? where id='queue:1'",
                        (json.dumps(queue_raw),),
                    )
                return {"enqueued": [{"id": "race-transfer", "username": row["username"], "filename": row["filename"]}]}
            probe.slskd_enqueue_candidate = enqueue_then_verify
            probe.slskd_delete_download_transfer = lambda transfer, dry_run: cancelled.append(transfer["id"]) or {"deleted": True}
            audits = []
            probe.auto_grab_audit = lambda event, **kwargs: audits.append({"event": event, **kwargs})
            probe.log = lambda *args, **kwargs: None
            outcome = probe.run_auto_grab(
                SimpleNamespace(auto_grab_live=True, auto_grab_dry_run=False, auto_grab_max=1),
                {"items": {entry["review_id"]: entry}},
            )
            require(len(enqueued) == 1, f"race fixture did not enqueue exactly once: {outcome}")
            require(cancelled == ["race-transfer"], "handoff racing durable completion was not cancelled")
            require(outcome["started_count"] == 0, "verified durable queue reported a started handoff")
            require(outcome["rows"][0]["status"] == "skipped_durable_queue_gate", outcome)
            audit_text = json.dumps(audits)
            for private_value in ("race-transfer", "peer-private", "Private/Series 001.cbz", "waiting-private-id"):
                require(private_value not in audit_text, "race cancellation audit leaked SLSKD locator evidence")
            with inkdrop_state.connect_read(db) as con:
                require(con.execute("select count(*) from queue_claims").fetchone()[0] == 0, "terminal gate leaked a claim")
        finally:
            probe.INKDROP_STATE_DB = original_db
            for name, value in original.items():
                setattr(probe, name, value)


def terminal_false_duplicate_attempt_reconciliation_smoke():
    with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-attempt-reconcile-") as temp:
        root = Path(temp)
        db = root / inkdrop_state.STATE_DB_NAME
        state_path = root / "slskd-auto-grab-state.json"
        shared_lock_path = root / "inkdrop-series-autopilot.lock"
        actions_path = root / "manual-review-actions.json"
        review_id = "review:terminal-false-duplicate"
        queue_id = "queue:eligible"
        candidate_identity = "candidate:terminal-false-duplicate"

        def seed_case(
            key, *, review=None, identity=None, task_issue=None, attempt_issue=None,
            wanted_status="wanted", current_source=None, queue_state="searching",
            attempt_lifecycle="failed_candidate", attempt_status="preview_not_importable",
        ):
            review = review or f"review:{key}"
            identity = candidate_identity if identity is None else identity
            series_id = f"series:{key}"
            issue_id = f"issue:{key}"
            wanted_id = f"wanted:{key}"
            case_queue_id = f"queue:{key}"
            attempt_id = f"attempt:{key}"
            task_id = f"task:{key}"
            raw = {"review_id": review, "filename": f"Series {key} 001.cbz", "candidate_identity": identity}
            with inkdrop_state.connect(db) as con:
                inkdrop_state.init_schema(con)
                con.execute(
                    "insert into series(id,title,media_type,monitored,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?)",
                    (series_id, f"Series {key}", "comic", 1, 1, 1, "{}"),
                )
                con.execute(
                    "insert into issues(id,series_id,issue_number,monitored,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?)",
                    (issue_id, series_id, "1", 1, 1, 1, "{}"),
                )
                con.execute(
                    "insert into wanted_items(id,series_id,issue_id,status,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?)",
                    (wanted_id, series_id, issue_id, wanted_status, 1, 1, "{}"),
                )
                con.execute(
                    "insert into queue_items(id,wanted_id,series_id,issue_id,state,current_source,active,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?,?)",
                    (
                        case_queue_id, wanted_id, series_id, issue_id, queue_state, current_source, 1, 1, 2,
                        json.dumps({
                            "series": f"Series {key}", "issue": "1", "queue_identity": series_id,
                            "current_source": current_source,
                            "last_slskd_waiting_review_id": review if current_source == "slskd" else None,
                            "last_slskd_transfer_id": f"transfer:{key}" if current_source == "slskd" else None,
                        }),
                    ),
                )
                con.execute(
                    """
                    insert into source_attempts(
                        id,queue_id,wanted_id,series_id,issue_id,source,download_client,candidate_identity,
                        lifecycle_phase,failure_reason,status,started_at,completed_at,raw_json
                    ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        attempt_id, case_queue_id, wanted_id, series_id, attempt_issue or issue_id,
                        "slskd", "SLSKD", identity, attempt_lifecycle, "already_verified_duplicate",
                        attempt_status, 2, 3, "{}",
                    ),
                )
                con.execute(
                    """
                    insert into download_tasks(
                        id,queue_id,wanted_id,series_id,issue_id,source_attempt_id,source,download_client,
                        candidate_identity,lifecycle_phase,failure_reason,title,status,state,started_at,updated_at,
                        completed_at,raw_json
                    ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        task_id, case_queue_id, wanted_id, series_id, task_issue or issue_id, attempt_id,
                        "slskd", "SLSKD", identity, "failed_candidate", "already_verified_duplicate",
                        raw["filename"], "preview_not_importable", "failed", 2, 3, 3, json.dumps(raw),
                    ),
                )
            return {
                "review_id": review,
                "queue_id": case_queue_id,
                "series_id": series_id,
                "issue_id": issue_id,
                "wanted_id": wanted_id,
                "attempt_id": attempt_id,
                "task_id": task_id,
            }

        eligible = seed_case(
            "eligible", review=review_id, wanted_status="in_progress", current_source="slskd", queue_state="queued"
        )
        blank_identity = seed_case("blank-identity", identity="")
        mismatched_identity = seed_case("mismatched-identity")
        wrong_queue = seed_case("wrong-queue")
        wrong_unit = seed_case("wrong-unit", task_issue="issue:eligible")
        active_task = seed_case("active-task", current_source="slskd")
        active_claim = seed_case("active-claim", current_source="slskd")
        active_candidate = seed_case("active-candidate", current_source="slskd")
        different_source = seed_case("different-source", wanted_status="in_progress", current_source="prowlarr")
        linked_active = seed_case(
            "linked-active", review="review:linked-active", wanted_status="in_progress",
            current_source="slskd", queue_state="queued",
            attempt_lifecycle="downloading", attempt_status="started_waiting",
        )
        verified = seed_case("verified")
        with inkdrop_state.connect(db) as con:
            eligible_raw = json.loads(con.execute(
                "select raw_json from queue_items where id=?", (eligible["queue_id"],)
            ).fetchone()[0])
            eligible_raw.update({"retry_after": 1234, "retry_after_iso": "1970-01-01T00:20:34Z"})
            con.execute(
                "update queue_items set retry_after=1234,retry_after_iso='1970-01-01T00:20:34Z',raw_json=? where id=?",
                (json.dumps(eligible_raw), eligible["queue_id"]),
            )
            con.execute(
                "update source_attempts set candidate_identity='candidate:other' where id=?",
                (mismatched_identity["attempt_id"],),
            )
            con.execute(
                "update source_attempts set queue_id=? where id=?",
                (eligible["queue_id"], wrong_queue["attempt_id"]),
            )
            con.execute(
                "insert into download_tasks(id,queue_id,wanted_id,series_id,issue_id,source,status,state,lifecycle_phase,raw_json) values(?,?,?,?,?,?,?,?,?,?)",
                (
                    "task:block-active", active_task["queue_id"], active_task["wanted_id"], active_task["series_id"],
                    active_task["issue_id"], "slskd", "transfer_in_progress", "downloading", "downloading", "{}",
                ),
            )
            con.execute(
                "insert into queue_claims(queue_id,owner_id,operation,claimed_at,heartbeat_at,expires_at,raw_json) values(?,?,?,?,?,?,?)",
                (active_claim["queue_id"], "owner", "slskd_auto_grab_handoff", 1, 1, resolver.now() + 3600, "{}"),
            )
            con.execute(
                "insert into source_attempts(id,queue_id,wanted_id,series_id,issue_id,source,download_client,lifecycle_phase,status,started_at,raw_json) values(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "attempt:block-active", active_candidate["queue_id"], active_candidate["wanted_id"],
                    active_candidate["series_id"], active_candidate["issue_id"], "slskd", "SLSKD",
                    "downloading", "started_waiting", 5, "{}",
                ),
            )
        inkdrop_state.sync_settings(
            db,
            settings=[{
                "key": "media_management.comic_root", "scope": "media_management", "label": "Comic root",
                "value": str(root), "description": "test", "source": "user",
            }],
        )
        verified_file = root / "Series verified 001.cbz"
        with zipfile.ZipFile(verified_file, "w") as archive:
            archive.writestr("001.jpg", b"image")
        with inkdrop_state.connect(db) as con:
            con.execute(
                "insert into import_results(id,queue_id,series_id,issue_id,dest_path,status,verified,imported_count,created_at,raw_json) values(?,?,?,?,?,?,?,?,?,?)",
                (
                    "import:verified", verified["queue_id"], verified["series_id"], verified["issue_id"],
                    str(verified_file), "verified", 1, 1, 10, "{}",
                ),
            )

        old_globals = (
            probe.INKDROP_STATE_DB, probe.SLSKD_AUTO_GRAB_STATE_FILE,
            probe.SERIES_AUTOPILOT_LOCK, probe.MANUAL_REVIEW_ACTIONS_FILE,
        )
        old_resolver_lock = resolver.SERIES_AUTOPILOT_LOCK
        originals = {}
        names = (
            "auto_grab_review_rows", "active_auto_grab_user_load", "bad_candidate_match",
            "mark_manual_source_waiting_local", "slskd_existing_download", "slskd_enqueue_candidate",
            "auto_grab_transfer_from_enqueue", "record_slskd_queue_attempt", "auto_grab_audit", "log",
        )
        for name in names:
            originals[name] = getattr(probe, name)
        try:
            probe.INKDROP_STATE_DB = db
            probe.SLSKD_AUTO_GRAB_STATE_FILE = state_path
            probe.SERIES_AUTOPILOT_LOCK = shared_lock_path
            probe.MANUAL_REVIEW_ACTIONS_FILE = actions_path
            resolver.SERIES_AUTOPILOT_LOCK = shared_lock_path
            entry = {
                "review_id": review_id, "series": "Series eligible", "issue": "1",
                "queue_identity": "series:eligible", "autopilot_queue": True,
                "autopilot_queue_key": queue_id,
            }
            first_candidate = {
                "filename": "Series eligible 001.cbz", "username": "peer-one", "size": 100,
                "score": 100, "auto_grab": {"verdict": "auto_grab_safe"},
            }
            probe.auto_grab_review_rows = lambda result, state=None: ([(review_id, entry, first_candidate)], [], [])
            probe.active_auto_grab_user_load = lambda: {}
            probe.bad_candidate_match = lambda *_args, **_kwargs: None
            probe.mark_manual_source_waiting_local = lambda *_args, **_kwargs: {"record": {"review_id": review_id}}
            probe.slskd_existing_download = lambda *_args, **_kwargs: None
            probe.slskd_enqueue_candidate = lambda candidate, dry_run: {
                "transfers": [{"id": "transfer:one", "username": candidate["username"], "filename": candidate["filename"]}]
            }
            probe.auto_grab_transfer_from_enqueue = lambda _response, candidate, **_kwargs: {
                "transfer": {"id": "transfer:one", "username": candidate["username"], "filename": candidate["filename"]}
            }
            probe.record_slskd_queue_attempt = lambda *_args, **_kwargs: {"ok": True}
            probe.auto_grab_audit = lambda *_args, **_kwargs: None
            probe.log = lambda *_args, **_kwargs: None
            handoff = probe.run_auto_grab(
                SimpleNamespace(auto_grab_live=True, auto_grab_dry_run=False, auto_grab_max=1),
                {"items": {review_id: entry}},
            )
            require(handoff["started_count"] == 1 and handoff["rows"][0]["status"] == "started_waiting", handoff)
            reservation = inkdrop_state.transition_matching_slskd_candidate_task(
                db,
                queue_id,
                probe.slskd_candidate_identity(entry, first_candidate),
                status="transfer_failed",
                reason="terminal_false_duplicate_fixture_transfer_ended",
            )
            require(reservation.get("ok") and reservation.get("state") == "failed", reservation)
            with inkdrop_state.connect(db) as con:
                queue_raw = json.loads(con.execute("select raw_json from queue_items where id=?", (queue_id,)).fetchone()[0])
                for stale_key in ("last_slskd_transfer_id", "last_slskd_waiting_review_id", "slskd_transfer_id"):
                    queue_raw.pop(stale_key, None)
                queue_raw.update({"retry_after": 1234, "retry_after_iso": "1970-01-01T00:20:34Z"})
                con.execute(
                    "update queue_items set current_source='slskd',retry_after=1234,retry_after_iso='1970-01-01T00:20:34Z',raw_json=? where id=?",
                    (json.dumps(queue_raw), queue_id),
                )
            state = probe.load_auto_grab_state()
            old_candidate_key = probe.auto_grab_candidate_key(review_id, first_candidate)
            require(state["review_attempts"][review_id] == 1, "real handoff did not consume a review attempt")
            state["review_attempts"][review_id] = 12
            state["review_attempts"][linked_active["review_id"]] = 1
            state["candidate_attempts"][old_candidate_key] = 4
            probe.save_auto_grab_state(state)
            actions_path.write_text(json.dumps({
                "manual_source_bad_candidates": {review_id: [{"candidate_key": old_candidate_key, "reason": "preserve"}]}
            }), encoding="utf-8")

            evidence = resolver.terminal_false_duplicate_review_attempt_evidence(db)
            require(set(evidence) == {review_id}, f"unsafe terminal evidence was admitted: {evidence}")
            held_lock, held_reason = resolver.acquire_series_autopilot_lock()
            require(held_lock is not None, f"shared lock fixture could not acquire lock: {held_reason}")
            try:
                before_deferred = state_path.read_bytes()
                deferred = resolver.reconcile_terminal_false_duplicate_review_attempts(probe, db)
                require(deferred.get("deferred") and deferred["retired_count"] == 0, deferred)
                require(state_path.read_bytes() == before_deferred, "busy shared lock mutated auto-grab state")
            finally:
                resolver.release_series_autopilot_lock(held_lock)
            first = resolver.reconcile_terminal_false_duplicate_review_attempts(probe, db)
            require(not first.get("deferred") and first["retired_count"] == 1, first)
            with inkdrop_state.connect_read(db) as con:
                repaired = con.execute(
                    "select state,current_source,retry_after,retry_after_iso,raw_json from queue_items where id=?", (eligible["queue_id"],)
                ).fetchone()
                repaired_wanted = con.execute(
                    "select status from wanted_items where id=?", (eligible["wanted_id"],)
                ).fetchone()
                active_row = con.execute(
                    "select current_source from queue_items where id=?", (active_task["queue_id"],)
                ).fetchone()
                different_row = con.execute(
                    "select current_source from queue_items where id=?", (different_source["queue_id"],)
                ).fetchone()
                linked_active_row = con.execute(
                    """
                    select q.state,q.current_source,w.status as wanted_status
                    from queue_items q join wanted_items w on w.id=q.wanted_id where q.id=?
                    """,
                    (linked_active["queue_id"],),
                ).fetchone()
                history_count = con.execute(
                    "select count(*) from history_events where entity_id=? and event_type='slskd_terminal_false_duplicate_reconciled'",
                    (eligible["queue_id"],),
                ).fetchone()[0]
            repaired_raw = json.loads(repaired["raw_json"])
            require(repaired["state"] == "queued" and repaired["current_source"] is None, "stale SLSKD ownership was not cleared")
            require(repaired["retry_after"] == 1234 and repaired["retry_after_iso"] == "1970-01-01T00:20:34Z", "retry backoff was not retained")
            require(repaired_wanted["status"] == "wanted", "in-progress Wanted row was not returned to searchable state")
            require("last_slskd_transfer_id" not in repaired_raw and "last_slskd_waiting_review_id" not in repaired_raw, "stale transfer ownership survived")
            require(active_row["current_source"] == "slskd", "active SLSKD ownership was cleared")
            require(different_row["current_source"] == "prowlarr", "different source ownership was cleared")
            require(
                linked_active_row["state"] == "queued"
                and linked_active_row["current_source"] == "slskd"
                and linked_active_row["wanted_status"] == "in_progress",
                "active linked source attempt was rearmed",
            )
            require(history_count == 1, "stale SLSKD ownership repair history was not recorded exactly once")
            second = resolver.reconcile_terminal_false_duplicate_review_attempts(probe, db)
            require(second["retired_count"] == 0, "repeated resolver reconciliation decremented twice")
            state = probe.load_auto_grab_state()
            require(state["review_attempts"][review_id] == 11, state)
            require(state["review_attempts"][linked_active["review_id"]] == 1, "active linked attempt retired a review slot")
            require(
                linked_active["review_id"] not in state.get("retired_terminal_review_attempt_evidence", {}),
                "active linked attempt recorded retirement evidence",
            )
            with inkdrop_state.connect(db) as con:
                con.execute(
                    "update source_attempts set lifecycle_phase='failed_candidate',status='preview_not_importable' where id=?",
                    (linked_active["attempt_id"],),
                )
            linked_terminal = resolver.reconcile_terminal_false_duplicate_review_attempts(probe, db)
            require(linked_terminal["retired_count"] == 1, linked_terminal)
            with inkdrop_state.connect_read(db) as con:
                linked_rearmed = con.execute(
                    """
                    select q.state,q.current_source,w.status as wanted_status
                    from queue_items q join wanted_items w on w.id=q.wanted_id where q.id=?
                    """,
                    (linked_active["queue_id"],),
                ).fetchone()
            state = probe.load_auto_grab_state()
            require(
                linked_rearmed["state"] == "queued"
                and linked_rearmed["current_source"] is None
                and linked_rearmed["wanted_status"] == "wanted",
                "terminal linked source attempt did not complete authoritative rearm",
            )
            require(state["review_attempts"][linked_active["review_id"]] == 0, "terminal linked attempt did not retire once")
            linked_replay = resolver.reconcile_terminal_false_duplicate_review_attempts(probe, db)
            require(linked_replay["retired_count"] == 0, "terminal linked attempt replay was not idempotent")
            require(state["candidate_attempts"][old_candidate_key] == 4, "candidate attempt protection was cleared")
            require(
                json.loads(actions_path.read_text(encoding="utf-8"))["manual_source_bad_candidates"][review_id],
                "bad-candidate protection was cleared",
            )
            fresh_candidate = {
                "filename": "Series eligible 001 alternate.cbz", "username": "peer-two", "size": 101,
                "score": 99, "auto_grab": {"verdict": "auto_grab_safe"},
            }
            allowed, _reason, _key = probe.auto_grab_attempt_allowed(state, review_id, fresh_candidate)
            require(allowed, "retired review attempt did not return the Wanted row to normal eligibility")
            probe.auto_grab_review_rows = lambda result, state=None: ([(review_id, entry, fresh_candidate)], [], [])
            fresh = probe.run_auto_grab(
                SimpleNamespace(auto_grab_live=True, auto_grab_dry_run=False, auto_grab_max=1),
                {"items": {review_id: entry}},
            )
            require(fresh["started_count"] == 1, f"eligible row did not permit one fresh real handoff: {fresh}")
            fresh_terminal = inkdrop_state.transition_matching_slskd_candidate_task(
                db,
                queue_id,
                probe.slskd_candidate_identity(entry, fresh_candidate),
                status="transfer_failed",
                reason="concurrent_reconciliation_fixture_transfer_ended",
            )
            require(fresh_terminal.get("ok") and fresh_terminal.get("state") == "failed", fresh_terminal)

            # Simulate a probe that loaded before resolver reconciliation and
            # commits its new attempt afterward. The merge must preserve both.
            probe_base = probe.load_auto_grab_state()
            probe_run = json.loads(json.dumps(probe_base))
            concurrent_candidate = {
                "filename": "Series eligible 001 concurrent.cbz", "username": "peer-three", "size": 102,
                "score": 98, "auto_grab": {"verdict": "auto_grab_safe"},
            }
            concurrent_key = probe.record_auto_grab_attempt(
                probe_run, review_id, concurrent_candidate,
                {"status": "started_waiting", "filename": concurrent_candidate["filename"], "username": "peer-three", "score": 98},
            )
            concurrent_identity = "candidate:concurrent-terminal"
            concurrent_attempt_id = "attempt:eligible-concurrent-terminal"
            concurrent_task_id = "task:eligible-concurrent-terminal"
            with inkdrop_state.connect(db) as con:
                con.execute(
                    """
                    insert into source_attempts(
                        id,queue_id,wanted_id,series_id,issue_id,source,download_client,candidate_identity,
                        lifecycle_phase,failure_reason,status,started_at,completed_at,raw_json
                    ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        concurrent_attempt_id, eligible["queue_id"], eligible["wanted_id"], eligible["series_id"],
                        eligible["issue_id"], "slskd", "SLSKD", concurrent_identity, "failed_candidate",
                        "already_verified_duplicate", "preview_not_importable", 20, 21, "{}",
                    ),
                )
                con.execute(
                    """
                    insert into download_tasks(
                        id,queue_id,wanted_id,series_id,issue_id,source_attempt_id,source,download_client,
                        candidate_identity,lifecycle_phase,failure_reason,title,status,state,started_at,updated_at,
                        completed_at,raw_json
                    ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        concurrent_task_id, eligible["queue_id"], eligible["wanted_id"], eligible["series_id"],
                        eligible["issue_id"], concurrent_attempt_id, "slskd", "SLSKD", concurrent_identity,
                        "failed_candidate", "already_verified_duplicate", concurrent_candidate["filename"],
                        "preview_not_importable", "failed", 20, 21, 21,
                        json.dumps({
                            "review_id": review_id, "filename": concurrent_candidate["filename"],
                            "candidate_identity": concurrent_identity,
                        }),
                    ),
                )
            concurrent_retirement = resolver.reconcile_terminal_false_duplicate_review_attempts(probe, db)
            require(concurrent_retirement["retired_count"] == 1, concurrent_retirement)
            merged = probe.commit_auto_grab_state_changes(probe_base, probe_run)
            require(merged["review_attempts"][review_id] == probe_base["review_attempts"][review_id], "probe/resolver counter merge lost an update")
            require(merged["candidate_attempts"][concurrent_key] == 1, "concurrent probe attempt was not added exactly once")
            retired_evidence = merged["retired_terminal_review_attempt_evidence"][review_id]
            require(any(concurrent_task_id in value for value in retired_evidence), "resolver retirement evidence was overwritten")
            concurrent_replay = resolver.reconcile_terminal_false_duplicate_review_attempts(probe, db)
            require(concurrent_replay["retired_count"] == 0, "concurrent evidence replay was not idempotent")
        finally:
            (
                probe.INKDROP_STATE_DB, probe.SLSKD_AUTO_GRAB_STATE_FILE,
                probe.SERIES_AUTOPILOT_LOCK, probe.MANUAL_REVIEW_ACTIONS_FILE,
            ) = old_globals
            resolver.SERIES_AUTOPILOT_LOCK = old_resolver_lock
            for name, value in originals.items():
                setattr(probe, name, value)


def missing_record_and_lookup_smoke():
    row = {"transfer": {"id": "orphan", "username": "peer", "state": "InProgress"}, "reason": "missing"}
    original_delete = probe.slskd_delete_download_transfer
    original_bad = probe.mark_probe_candidate_bad
    try:
        probe.slskd_delete_download_transfer = lambda transfer, dry_run: {"deleted": True}
        probe.mark_probe_candidate_bad = lambda *args, **kwargs: {"candidate_key": "missing"}
        probe.mark_waiting_record_missing_retry("review", {"series": "Series"}, {"filename": "Series.cbz"}, row, dry_run=False)
    finally:
        probe.slskd_delete_download_transfer = original_delete
        probe.mark_probe_candidate_bad = original_bad
    require(row.get("orphan_transfer_cleared") and row.get("retry_next_candidate"), "missing waiting record did not recover")
    item = {"state": "downloading", "current_source": "slskd"}
    autopilot.apply_slskd_transfer_status(item, {"status": "transfer_lookup_error", "reason": "temporary lookup"}, 100.0)
    require(item["state"] == "downloading" and item["current_source"] == "slskd", "lookup error must not trigger duplicate enqueue")


def replay_smoke():
    with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-replay-") as temp:
        state_dir = Path(temp) / "state"
        config_dir = Path(temp) / "config"
        log_dir = Path(temp) / "logs"
        for path in (state_dir, config_dir, log_dir):
            path.mkdir(parents=True, exist_ok=True)
        db = state_dir / inkdrop_state.STATE_DB_NAME
        seed_queue(db)
        env = os.environ.copy()
        env.update({"INKDROP_STATE_DIR": str(state_dir), "INKDROP_CONFIG_DIR": str(config_dir), "INKDROP_LOG_DIR": str(log_dir)})
        writer = r'''
import json
from core import inkdrop_manual_source_autoresolve as resolver
payload = {
  "state": "deferred",
  "processed": [{"autopilot_queue": True, "autopilot_queue_key": "queue:1", "status": "transfer_failed"}],
  "skipped": [],
  "native_attempt_replay": [{
    "queue_id": "queue:1",
    "attempt": {"source": "slskd", "protocol": "soulseek", "download_client": "SLSKD", "status": "transfer_failed", "reason": "deferred terminal"},
    "attempt_id": "terminal:restart-replay", "started_at": 500.0, "completed_at": 500.0
  }]
}
print(json.dumps(resolver.persist_deferred_autopilot_queue_sync(payload, "restart-test")))
'''
        persisted = subprocess.run([sys.executable, "-c", writer], cwd=Path(__file__).resolve().parents[1], env=env, text=True, capture_output=True, check=True)
        require(json.loads(persisted.stdout)["status"] == "stored", "deferred serializer did not store restart fixture")
        reader = r'''
import json
from core import inkdrop_series_autopilot as autopilot
snapshots = autopilot.manual_source_autoresolve_snapshots({})
assert len(snapshots) == 1, snapshots
snapshot = snapshots[0]
assert snapshot.get("native_attempt_replay"), snapshot
result = autopilot.replay_deferred_native_autoresolve_attempts(snapshot)
assert result.get("recorded") == 1, result
autopilot.mark_deferred_manual_source_sync_applied(snapshot.get("_deferred_queue_sync_id"))
autopilot.ack_deferred_manual_source_queue_syncs()
print(json.dumps(result))
'''
        replayed = subprocess.run([sys.executable, "-c", reader], cwd=Path(__file__).resolve().parents[1], env=env, text=True, capture_output=True, check=True)
        require(json.loads(replayed.stdout)["recorded"] == 1, "fresh-process replay failed")
        verifier = r'''
import json
from core import inkdrop_series_autopilot as autopilot
print(json.dumps({"remaining": len(autopilot.deferred_manual_source_queue_sync_entries())}))
'''
        verified = subprocess.run([sys.executable, "-c", verifier], cwd=Path(__file__).resolve().parents[1], env=env, text=True, capture_output=True, check=True)
        require(json.loads(verified.stdout)["remaining"] == 0, "replayed deferred entry was not acknowledged across restart")
        with inkdrop_state.connect_read(db) as con:
            count = con.execute("select count(*) from source_attempts where id='terminal:restart-replay'").fetchone()[0]
        require(count == 1, "round-trip replay did not durably record exactly one native attempt")


def authoritative_reservation_privacy_and_legacy_smoke():
    nested = {
        "candidate_locator_digest": "sha256:safe-digest",
        "candidate": {
            "username": "private-peer",
            "download_url": "https://user:secret@example.invalid/private/file.cbz?token=secret",
            "magnet": "magnet:?xt=urn:btih:0123456789012345678901234567890123456789&dn=secret",
            "path": r"C:\Private\Downloads\Series 001.cbz",
            "filename": r"\\peer\Private Share\Series 001.cbz",
            "headers": {"Authorization": "Bearer secret", "Cookie": "session=secret"},
            "credentials": {
                "password": "password-value", "passwd": "passwd-value",
                "api_key": "api-key-value", "apikey": "apikey-value", "token": "token-value",
                "secret": "secret-value", "access_token": "access-value", "refresh_token": "refresh-value",
            },
        },
    }
    safe = inkdrop_state.slskd_private_evidence_payload(nested)
    encoded = json.dumps(safe, sort_keys=True)
    require(safe["candidate_locator_digest"] == "sha256:safe-digest", "candidate locator digest was not retained")
    require(safe["candidate"]["filename"] == "Series 001.cbz", f"safe basename was not retained: {safe}")
    for secret in ("private-peer", "user:secret", "token=secret", "magnet:?", r"C:\Private", r"\\peer\Private Share", "Bearer secret", "session=secret", "password-value", "passwd-value", "api-key-value", "apikey-value", "token-value", "secret-value", "access-value", "refresh-value"):
        require(secret not in encoded, f"nested private locator escaped redaction: {secret}")
    managed_path = r"C:\InkDrop\staging\Series 001.cbz"
    direct_url = "https://pixeldrain.com/api/file/certified-route"
    ordinary = inkdrop_state.privacy_safe_evidence_payload({"local_path": managed_path, "destination_path": managed_path})
    require(ordinary["local_path"] == managed_path and ordinary["destination_path"] == managed_path, "global privacy handling destroyed legitimate local path evidence")
    slskd_local = inkdrop_state.slskd_private_evidence_payload({"local_path": managed_path, "destination_path": managed_path})
    require(slskd_local["local_path"] == managed_path and slskd_local["destination_path"] == managed_path, "SLSKD privacy handling destroyed managed local path evidence")
    local_task = inkdrop_state.download_task_from_attempt(
        "queue:local", "wanted:local", "series:local", "issue:local",
        {"source": "rss_getcomics", "protocol": "http", "download_client": "inkdrop_direct", "status": "sent", "title": "Series 001.cbz", "external_id": "direct:local", "download_url": direct_url, "local_path": managed_path, "destination_path": managed_path},
        "attempt:local",
    )
    local_raw = json.loads(local_task["raw_json"])
    require(local_task["local_path"] == managed_path and local_raw["destination_path"] == managed_path, "legitimate task local/destination path evidence was hashed")
    require(local_raw["download_url"] == direct_url, "generic direct-download operational URL did not survive task persistence")
    require(inkdrop_state.privacy_safe_evidence_payload({"download_url": direct_url})["download_url"].startswith("sha256:"), "public direct-download evidence retained an operational URL")

    with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-provider-legacy-") as temp:
        db = Path(temp) / "state.sqlite3"
        seed_queue(db)
        old_db = probe.INKDROP_STATE_DB
        try:
            probe.INKDROP_STATE_DB = db
            health = probe.record_slskd_provider_wait_attempt({
                "status": "provider_unavailable", "autopilot_queue": True,
                "autopilot_queue_key": "queue:1", "review_id": "review:health",
                "series": "Series", "issue": "1", "provider_error": "offline",
            })
        finally:
            probe.INKDROP_STATE_DB = old_db
        require(health.get("ok"), health)
        with inkdrop_state.connect_read(db) as con:
            health_tasks = [dict(row) for row in con.execute("select * from download_tasks").fetchall()]
            require(not health_tasks, f"provider health synthesized a candidate task: {health_tasks}")

        with inkdrop_state.connect(db) as con:
            for number in (1, 2):
                attempt_id, task_id = f"legacy-attempt:{number}", f"legacy-task:{number}"
                raw = json.dumps({"filename": "Series 001.cbz", "username": f"legacy-peer-{number}", "remote_path": rf"\\legacy-peer-{number}\Private\Series 001.cbz", "candidate_locator_digest": f"sha256:legacy-{number}"})
                con.execute("insert into source_attempts(id,queue_id,wanted_id,series_id,issue_id,source,download_client,status,lifecycle_phase,started_at,raw_json) values(?,?,?,?,?,?,?,?,?,?,?)", (attempt_id, "queue:1", "wanted:1", "series:1", "issue:1", "slskd", "SLSKD", "user_load_wait", "provider_wait", number, raw))
                con.execute("insert into download_tasks(id,queue_id,wanted_id,series_id,issue_id,source_attempt_id,source,download_client,candidate_identity,title,status,state,lifecycle_phase,started_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (task_id, "queue:1", "wanted:1", "series:1", "issue:1", attempt_id, "slskd", "SLSKD", f"legacy-candidate:{number}", "Series 001.cbz", "user_load_wait", "queued", "provider_wait", number, number, raw))
            changed = inkdrop_state.reconcile_slskd_candidate_reservations(con, now=100.0, retry_seconds=60)
            replay = inkdrop_state.reconcile_slskd_candidate_reservations(con, now=101.0, retry_seconds=60)
        require(changed == 2 and replay == 0, f"legacy reconciliation was not exact and idempotent: {changed}, {replay}")
        with inkdrop_state.connect_read(db) as con:
            tasks = con.execute("select status,state,lifecycle_phase,raw_json from download_tasks where id like 'legacy-task:%' order by id").fetchall()
            attempts = con.execute("select status,lifecycle_phase from source_attempts where id like 'legacy-attempt:%' order by id").fetchall()
        require(all(row["status"] == "reservation_failed" and row["state"] == "failed" and row["lifecycle_phase"] == "failed_candidate" for row in tasks), tasks)
        require(all(row["status"] == "reservation_failed" and row["lifecycle_phase"] == "failed_candidate" for row in attempts), attempts)
        durable = "\n".join(row["raw_json"] for row in tasks)
        require("legacy-peer" not in durable and "\\\\legacy-peer" not in durable, "legacy reconciliation retained private locator material")
        require("sha256:legacy-1" in durable and "sha256:legacy-2" in durable, "legacy reconciliation lost safe locator digests")


def queue_only_legacy_wait_reconciliation_smoke():
    def legacy_wait(db, *, retry_after=90.0):
        with inkdrop_state.connect(db) as con:
            row = con.execute("select raw_json from queue_items where id='queue:1'").fetchone()
            raw = json.loads(row[0] or "{}")
            raw.update({
                "state": "queued", "current_source": None,
                "last_event": "SLSKD candidate ready; waiting for transfer slot",
                "last_slskd_autopick_status": "user_load_wait",
                "last_slskd_auto_grab_safe_count": 1,
                "last_slskd_candidate_count": 1,
                "last_slskd_candidate": "Series 001.cbz",
                "last_slskd_user": "legacy-private-peer",
                "retry_after": retry_after,
            })
            con.execute(
                "update queue_items set state='queued',current_source=null,last_event=?,retry_after=?,raw_json=? where id='queue:1'",
                ("SLSKD candidate ready; waiting for transfer slot", retry_after, json.dumps(raw)),
            )

    def entry():
        return {
            "review_id": "review:legacy-retry", "series": "Series", "issue": "1",
            "autopilot_queue": True, "autopilot_queue_key": "queue:1",
            "queue_identity": "series:1",
        }

    def candidate(label):
        return {
            "filename": rf"\\private-peer\Private Share\Series 001 {label}.cbz",
            "username": "private-peer", "size": 10_000_000, "score": 100,
            "auto_grab": {"verdict": "auto_grab_safe"},
        }

    with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-queue-only-legacy-") as temp:
        db = Path(temp) / "state.sqlite3"
        seed_queue(db)
        legacy_wait(db)
        with inkdrop_state.connect(db) as con:
            changed = inkdrop_state.reconcile_slskd_candidate_reservations(con, now=100.0, retry_seconds=60)
            replay = inkdrop_state.reconcile_slskd_candidate_reservations(con, now=101.0, retry_seconds=60)
        require(changed == 1 and replay == 0, f"queue-only legacy wait was not rearmed exactly once: {changed}, {replay}")
        with inkdrop_state.connect_read(db) as con:
            queue = dict(con.execute("select * from queue_items where id='queue:1'").fetchone())
            raw = json.loads(queue["raw_json"] or "{}")
        require(queue["state"] == "queued" and queue["current_source"] is None, queue)
        require(raw.get("last_slskd_autopick_status") == "reservation_failed", raw)
        require(raw.get("last_slskd_auto_grab_safe_count") == 0, raw)
        require("last_slskd_candidate" not in raw and "last_slskd_user" not in raw, raw)

        with inkdrop_state.connect(db) as con:
            con.execute("update queue_items set retry_after=0,retry_after_iso=null where id='queue:1'")
        old_db = probe.INKDROP_STATE_DB
        try:
            probe.INKDROP_STATE_DB = db
            decision = probe.decide_automatic_slskd_handoff(entry(), candidate("fresh"), "fresh proof")
        finally:
            probe.INKDROP_STATE_DB = old_db
        require(decision.get("decision") == "authorize_enqueue" and decision.get("download_task_id"), decision)
        transition = inkdrop_state.transition_slskd_candidate_task(
            db, "queue:1", decision["reservation_id"], "started_waiting",
            transfer_id="transfer:fresh", observed_at=110.0,
            claim_owner_id=decision.get("claim_owner_id"),
        )
        require(transition.get("ok") and transition.get("external_id") == "transfer:fresh", transition)
        legacy_wait(db, retry_after=0.0)
        with inkdrop_state.connect(db) as con:
            active_block = inkdrop_state.reconcile_slskd_candidate_reservations(con, now=120.0, retry_seconds=60)
        require(active_block == 0, f"active transfer did not fence queue-only rearm: {active_block}")
        with inkdrop_state.connect_read(db) as con:
            active_tasks = [dict(row) for row in con.execute("select * from download_tasks where queue_id='queue:1'").fetchall() if inkdrop_state.download_task_is_activeish(dict(row))]
        require(len(active_tasks) == 1 and active_tasks[0]["external_id"] == "transfer:fresh", active_tasks)

    with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-queue-only-folder-fence-") as temp:
        db = Path(temp) / "state.sqlite3"
        seed_queue(db)
        legacy_wait(db)
        with inkdrop_state.connect(db) as con:
            con.execute(
                """insert into import_results(
                       id,queue_id,series_id,issue_id,status,outcome,display_phase,
                       completion_truth,folder_imported,verified,imported_count,created_at,raw_json
                   ) values(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "import:folder", "queue:1", "series:1", "issue:1",
                    "folder_verified", "productive", "verified", "folder",
                    1, 0, 1, 95.0, "{}",
                ),
            )
            changed = inkdrop_state.reconcile_slskd_candidate_reservations(con, now=100.0, retry_seconds=60)
        require(changed == 0, f"folder-complete import evidence did not fence rearm: {changed}")
        with inkdrop_state.connect_read(db) as con:
            queue = dict(con.execute("select state,last_event,raw_json from queue_items where id='queue:1'").fetchone())
        raw = json.loads(queue["raw_json"] or "{}")
        require(raw.get("last_slskd_autopick_status") == "user_load_wait", queue)


def production_candidate_handoff_outcomes_smoke():
    cases = {
        "immediate": "started_waiting",
        "existing": "already_downloading",
        "ambiguous": "enqueue_response_ambiguous",
        "no_rows": "enqueue_response_ambiguous",
        "definitive_error": "error",
    }
    for case, expected_status in cases.items():
        with tempfile.TemporaryDirectory(prefix=f"inkdrop-slskd-production-{case}-") as temp:
            db = Path(temp) / "state.sqlite3"
            seed_queue(db)
            with inkdrop_state.connect(db) as con:
                con.execute("update queue_items set state='queued',current_source=null where id='queue:1'")
            entry = {
                "review_id": f"review:{case}", "series": "Series", "issue": "1",
                "autopilot_queue": True, "autopilot_queue_key": "queue:1", "queue_identity": "series:1",
            }
            candidate = {
                "filename": rf"\\private-peer\Private Share\Series 001 {case}.cbz",
                "username": "private-peer", "size": 1000, "score": 100,
                "auto_grab": {"verdict": "auto_grab_safe"},
            }
            originals = {}
            names = (
                "load_auto_grab_state", "save_auto_grab_state", "auto_grab_review_rows",
                "active_auto_grab_user_load", "bad_candidate_match", "mark_manual_source_waiting_local",
                "slskd_existing_download", "slskd_enqueue_candidate", "auto_grab_transfer_from_enqueue",
                "auto_grab_audit", "log",
            )
            for name in names:
                originals[name] = getattr(probe, name)
            old_db = probe.INKDROP_STATE_DB
            enqueue_calls = []
            try:
                probe.INKDROP_STATE_DB = db
                probe.load_auto_grab_state = lambda: {}
                probe.save_auto_grab_state = lambda _state: None
                probe.auto_grab_review_rows = lambda _result, state=None: ([(entry["review_id"], entry, candidate)], [], [])
                probe.active_auto_grab_user_load = lambda: {}
                probe.bad_candidate_match = lambda *_args, **_kwargs: None
                probe.mark_manual_source_waiting_local = lambda *_args, **_kwargs: {"record": {"review_id": entry["review_id"]}}
                existing_transfer = {"id": f"transfer:{case}", "state": "InProgress", "filename": candidate["filename"], "username": candidate["username"]}
                probe.slskd_existing_download = lambda *_args, **_kwargs: existing_transfer if case == "existing" else None

                def enqueue(_candidate, dry_run=False):
                    enqueue_calls.append(case)
                    if case == "definitive_error":
                        raise TimeoutError("definitive provider request failure before transfer creation")
                    return {"ok": True, "transfers": [existing_transfer]}

                probe.slskd_enqueue_candidate = enqueue
                probe.auto_grab_transfer_from_enqueue = lambda *_args, **_kwargs: (
                    {"ambiguous": True, "reason": "multiple unmatched transfer rows", "candidate_rows": [{"id": "one"}, {"id": "two"}]}
                    if case == "ambiguous"
                    else {"transfer": {}, "match_status": "no_rows", "reason": "SLSKD enqueue response did not include transfer rows"}
                    if case == "no_rows"
                    else {"transfer": existing_transfer}
                )
                probe.auto_grab_audit = lambda *_args, **_kwargs: None
                probe.log = lambda *_args, **_kwargs: None
                outcome = probe._run_auto_grab_with_ephemeral_candidates(
                    SimpleNamespace(auto_grab_live=True, auto_grab_dry_run=False, auto_grab_max=1),
                    {"items": {entry["review_id"]: entry}},
                )
            finally:
                probe.INKDROP_STATE_DB = old_db
                for name, value in originals.items():
                    setattr(probe, name, value)
            row = outcome["rows"][0]
            require(row.get("status") == expected_status, f"{case} production handoff status mismatch: {row}")
            require((row.get("candidate_reservation") or {}).get("decision") == "authorize_enqueue", row)
            require(bool(enqueue_calls) == (case != "existing"), f"{case} enqueue call contract was wrong: {enqueue_calls}")
            transition = row.get("candidate_transition") or {}
            require(transition.get("ok"), f"{case} did not durably transition its reservation: {row}")
            with inkdrop_state.connect_read(db) as con:
                tasks = con.execute("select id,status,state,external_id,raw_json from download_tasks where queue_id='queue:1'").fetchall()
                claims = con.execute("select count(*) from queue_claims where queue_id='queue:1'").fetchone()[0]
            require(len(tasks) == 1 and tasks[0]["id"] == transition.get("download_task_id"), f"{case} manufactured a successor task: {tasks}")
            require(tasks[0]["status"] == expected_status, f"{case} task transition mismatch: {dict(tasks[0])}")
            require(claims == 0, f"{case} released no claim after durable transition")
            if case in {"immediate", "existing"}:
                require(tasks[0]["external_id"] == f"transfer:{case}", f"{case} lost authoritative transfer identity")
            elif case in {"ambiguous", "no_rows"}:
                require(tasks[0]["state"] == "queued" and not tasks[0]["external_id"], "ambiguous response asserted transfer ownership")
            else:
                require(tasks[0]["state"] == "failed" and not tasks[0]["external_id"], "definitive error was not compensated terminally")
            require("private-peer" not in tasks[0]["raw_json"] and "Private Share" not in tasks[0]["raw_json"], f"{case} retained private candidate evidence")


def authoritative_transfer_identity_reconciliation_smoke():
    def entry():
        return {
            "review_id": "review:identity", "series": "Series", "issue": "1",
            "autopilot_queue": True, "autopilot_queue_key": "queue:1",
            "queue_identity": "series:1",
        }

    def candidate():
        return {
            "filename": r"\\private-peer\Private Share\Series 001.cbz",
            "username": "private-peer", "size": 10_000_000, "score": 100,
            "auto_grab": {"verdict": "auto_grab_safe"},
        }

    transfer = {
        "id": "transfer:exact", "username": "private-peer",
        "directory": r"\\private-peer\Private Share", "filename": "Series 001.cbz",
        "size": 10_000_000, "state": "InProgress",
    }

    with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-external-id-recover-") as temp:
        db = Path(temp) / "state.sqlite3"
        seed_queue(db)
        old_db = probe.INKDROP_STATE_DB
        try:
            probe.INKDROP_STATE_DB = db
            reservation = probe.reserve_slskd_candidate(entry(), candidate(), "fixture reservation")
            require(reservation.get("created"), reservation)
            unresolved = inkdrop_state.transition_slskd_candidate_task(
                db, "queue:1", reservation["reservation_id"], "started_waiting",
                observed_at=100.0,
            )
            require(
                unresolved.get("ok")
                and unresolved.get("status") == "enqueue_response_ambiguous"
                and unresolved.get("state") == "queued"
                and not unresolved.get("external_id"),
                unresolved,
            )
            blocked = probe.decide_automatic_slskd_handoff(
                entry(), candidate(), "duplicate check", acquire_claim=False,
            )
            require(blocked.get("decision") == "reuse_existing", blocked)
            recovered = probe.reconcile_slskd_transfer_identity_tasks(
                observed_at=101.0, transfer_rows=[transfer],
            )
            replay = probe.reconcile_slskd_transfer_identity_tasks(
                observed_at=102.0, transfer_rows=[transfer],
            )
        finally:
            probe.INKDROP_STATE_DB = old_db
        require(recovered.get("recovered") == 1 and recovered.get("retired") == 0, recovered)
        require(replay.get("unresolved_tasks") == 0 and replay.get("recovered") == 0, replay)
        with inkdrop_state.connect_read(db) as con:
            task = dict(con.execute("select * from download_tasks where id=?", (reservation["download_task_id"],)).fetchone())
            queue = dict(con.execute("select * from queue_items where id='queue:1'").fetchone())
        require(task["external_id"] == "transfer:exact" and task["state"] == "downloading", task)
        require(queue["state"] == "downloading" and queue["current_source"] == "slskd", queue)

    with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-external-id-retire-") as temp:
        db = Path(temp) / "state.sqlite3"
        seed_queue(db)
        old_db = probe.INKDROP_STATE_DB
        try:
            probe.INKDROP_STATE_DB = db
            reservation = probe.reserve_slskd_candidate(entry(), candidate(), "fixture missing transfer")
            require(reservation.get("created"), reservation)
            unresolved = inkdrop_state.transition_slskd_candidate_task(
                db, "queue:1", reservation["reservation_id"], "started_waiting",
                observed_at=100.0,
            )
            require(unresolved.get("status") == "enqueue_response_ambiguous", unresolved)
            with inkdrop_state.connect(db) as con:
                raw = json.loads(con.execute(
                    "select raw_json from download_tasks where id=?",
                    (reservation["download_task_id"],),
                ).fetchone()[0])
                raw.update({"reservation_deadline": 100.0, "slot_request_deadline": 100.0})
                con.execute(
                    "update download_tasks set raw_json=? where id=?",
                    (json.dumps(raw), reservation["download_task_id"]),
                )
            retired = probe.reconcile_slskd_transfer_identity_tasks(
                observed_at=101.0, transfer_rows=[],
            )
            replay = probe.reconcile_slskd_transfer_identity_tasks(
                observed_at=102.0, transfer_rows=[],
            )
        finally:
            probe.INKDROP_STATE_DB = old_db
        require(retired.get("retired") == 1 and retired.get("recovered") == 0, retired)
        require(replay.get("unresolved_tasks") == 0 and replay.get("retired") == 0, replay)
        with inkdrop_state.connect_read(db) as con:
            task = dict(con.execute("select * from download_tasks where id=?", (reservation["download_task_id"],)).fetchone())
            queue = dict(con.execute("select * from queue_items where id='queue:1'").fetchone())
            wanted = dict(con.execute("select * from wanted_items where id='wanted:1'").fetchone())
        require(task["state"] == "failed" and task["status"] == "reservation_failed", task)
        require(queue["state"] == "queued" and queue["current_source"] is None, queue)
        require(wanted["status"] == "wanted", wanted)

    with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-external-id-sibling-fence-") as temp:
        db = Path(temp) / "state.sqlite3"
        seed_queue(db)
        old_db = probe.INKDROP_STATE_DB
        try:
            probe.INKDROP_STATE_DB = db
            reservation = probe.reserve_slskd_candidate(entry(), candidate(), "sibling fence fixture")
            require(reservation.get("created"), reservation)
            unresolved = inkdrop_state.transition_slskd_candidate_task(
                db, "queue:1", reservation["reservation_id"], "started_waiting",
                transfer_id="transfer:failed", observed_at=100.0,
            )
            require(unresolved.get("ok") and unresolved.get("external_id") == "transfer:failed", unresolved)
            with inkdrop_state.connect(db) as con:
                con.execute(
                    """insert into download_tasks(
                       id,queue_id,wanted_id,series_id,issue_id,source,download_client,
                       external_id,candidate_identity,title,status,state,lifecycle_phase,
                       started_at,updated_at,raw_json
                    ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "task:sibling-active", "queue:1", "wanted:1", "series:1", "issue:1",
                        "qbittorrent", "qBittorrent", "qbittorrent:sibling-active", "candidate:sibling",
                        "Series 001 alternate.cbz", "downloading", "downloading", "downloading",
                        100.0, 100.0, "{}",
                    ),
                )
            blocked = inkdrop_state.transition_slskd_candidate_task(
                db, "queue:1", reservation["reservation_id"], "reservation_failed",
                transfer_id="transfer:failed", reason="simulated failed transfer", observed_at=101.0,
            )
            require(
                blocked.get("ok")
                and blocked.get("authoritative_sibling_task_id") == "task:sibling-active",
                blocked,
            )
        finally:
            probe.INKDROP_STATE_DB = old_db
        with inkdrop_state.connect_read(db) as con:
            task = dict(con.execute("select status,state,external_id from download_tasks where id=?", (reservation["download_task_id"],)).fetchone())
            queue = dict(con.execute("select state,current_source from queue_items where id='queue:1'").fetchone())
        require(task["state"] == "failed" and task["external_id"] == "transfer:failed", task)
        require(queue["state"] == "downloading" and queue["current_source"] == "qbittorrent", queue)

    for sibling_kind, sibling_status, sibling_state, sibling_phase in (
        ("active-cross-queue", "downloading", "downloading", "downloading"),
        ("verified-cross-queue", "verified", "verified", "verified"),
    ):
        with tempfile.TemporaryDirectory(prefix=f"inkdrop-slskd-external-id-{sibling_kind}-") as temp:
            db = Path(temp) / "state.sqlite3"
            seed_queue(db)
            old_db = probe.INKDROP_STATE_DB
            try:
                probe.INKDROP_STATE_DB = db
                reservation = probe.reserve_slskd_candidate(entry(), candidate(), f"{sibling_kind} fixture")
                require(reservation.get("created"), reservation)
                unresolved = inkdrop_state.transition_slskd_candidate_task(
                    db, "queue:1", reservation["reservation_id"], "started_waiting",
                    transfer_id="transfer:failed", observed_at=100.0,
                )
                require(unresolved.get("ok"), unresolved)
                claim_now = time.time()
                with inkdrop_state.connect(db) as con:
                    con.execute(
                        """insert into queue_items(
                           id,wanted_id,series_id,issue_id,state,active,created_at,updated_at,raw_json
                        ) values(?,?,?,?,?,?,?,?,?)""",
                        (
                            "queue:2", "wanted:1", "series:1", "issue:1", sibling_state, 1,
                            100.0, 100.0, json.dumps({"queue_identity": "series:1"}),
                        ),
                    )
                    con.execute(
                        """insert into download_tasks(
                           id,queue_id,wanted_id,series_id,issue_id,source,download_client,
                           external_id,candidate_identity,title,status,state,lifecycle_phase,
                           started_at,updated_at,raw_json
                        ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            f"task:{sibling_kind}", "queue:2", "wanted:1", "series:1", "issue:1",
                            "qbittorrent", "qBittorrent", f"qbittorrent:{sibling_kind}",
                            f"candidate:{sibling_kind}", "Series 001 sibling.cbz", sibling_status,
                            sibling_state, sibling_phase, 100.0, 100.0, "{}",
                        ),
                    )
                    con.execute(
                        """insert into queue_claims(
                           queue_id,owner_id,operation,claimed_at,heartbeat_at,expires_at,raw_json
                        ) values(?,?,?,?,?,?,?)""",
                        (
                            "queue:1", "claim-owner", "slskd_auto_grab_handoff",
                            claim_now, claim_now, claim_now + 120.0,
                            json.dumps({"reservation_id": reservation["reservation_id"]}),
                        ),
                    )
                blocked = inkdrop_state.transition_slskd_candidate_task(
                    db, "queue:1", reservation["reservation_id"], "reservation_failed",
                    transfer_id="transfer:failed", claim_owner_id="claim-owner",
                    reason="simulated failed transfer", observed_at=101.0,
                )
            finally:
                probe.INKDROP_STATE_DB = old_db
            require(
                blocked.get("ok")
                and blocked.get("authoritative_sibling_task_id") == f"task:{sibling_kind}",
                blocked,
            )
            with inkdrop_state.connect_read(db) as con:
                task = dict(con.execute("select state,external_id from download_tasks where id=?", (reservation["download_task_id"],)).fetchone())
                queue = dict(con.execute("select state,current_source,active,retry_after from queue_items where id='queue:1'").fetchone())
                wanted = dict(con.execute("select status from wanted_items where id='wanted:1'").fetchone())
                claim = con.execute("select 1 from queue_claims where queue_id='queue:1'").fetchone()
            require(task["state"] == "failed" and task["external_id"] == "transfer:failed", task)
            require(claim is None, "failed sibling fence did not release the caller-owned claim")
            if sibling_kind == "verified-cross-queue":
                require(queue["state"] == "verified" and queue["active"] == 0 and not queue["retry_after"], queue)
                require(wanted["status"] == "satisfied", wanted)
            else:
                require(queue["state"] == "downloading" and queue["current_source"] == "qbittorrent" and queue["active"] == 1, queue)
                require(wanted["status"] == "downloading", wanted)
                with inkdrop_state.connect(db) as con:
                    sibling = dict(con.execute("select * from download_tasks where id=?", (f"task:{sibling_kind}",)).fetchone())
                    inkdrop_state.retire_download_task(
                        con, sibling, status="transfer_failed", state="failed", ts=102.0,
                        raw_payload={"failure_reason": "simulated sibling terminal"},
                    )
                retry_db = probe.INKDROP_STATE_DB
                try:
                    probe.INKDROP_STATE_DB = db
                    retryable = probe.reconcile_slskd_transfer_identity_tasks(
                        observed_at=103.0, transfer_rows=[],
                    )
                    replay = probe.reconcile_slskd_transfer_identity_tasks(
                        observed_at=104.0, transfer_rows=[],
                    )
                finally:
                    probe.INKDROP_STATE_DB = retry_db
                require(retryable.get("sibling_projection", {}).get("reconciled") == 1, retryable)
                require(replay.get("sibling_projection", {}).get("checked") == 0, replay)
                with inkdrop_state.connect_read(db) as con:
                    queue = dict(con.execute("select state,current_source,active,retry_after from queue_items where id='queue:1'").fetchone())
                    wanted = dict(con.execute("select status from wanted_items where id='wanted:1'").fetchone())
                    retry_at = queue["retry_after"]
                require(queue["state"] == "queued" and queue["current_source"] is None and queue["active"] == 1, queue)
                require(wanted["status"] == "wanted", wanted)
                require(retry_at == 281.0, queue)
                retry_db = probe.INKDROP_STATE_DB
                try:
                    probe.INKDROP_STATE_DB = db
                    fresh = probe.reserve_slskd_candidate(entry(), candidate(), "fresh retry after siblings terminal")
                finally:
                    probe.INKDROP_STATE_DB = retry_db
                require(fresh.get("created"), fresh)
                with inkdrop_state.connect_read(db) as con:
                    active_tasks = [dict(row) for row in con.execute("select * from download_tasks where queue_id='queue:1'").fetchall() if inkdrop_state.download_task_is_activeish(dict(row))]
                require(len(active_tasks) == 1 and active_tasks[0]["id"] == fresh["download_task_id"], active_tasks)

    with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-legacy-no-id-retire-") as temp:
        db = Path(temp) / "state.sqlite3"
        seed_queue(db)
        old_db = probe.INKDROP_STATE_DB
        try:
            probe.INKDROP_STATE_DB = db
            reservation = probe.reserve_slskd_candidate(entry(), candidate(), "legacy fixture")
            require(reservation.get("created"), reservation)
            with inkdrop_state.connect(db) as con:
                raw = json.loads(con.execute(
                    "select raw_json from download_tasks where id=?",
                    (reservation["download_task_id"],),
                ).fetchone()[0])
                for key in (
                    "reservation_id", "candidate_locator_digest",
                    "reservation_deadline", "slot_request_deadline",
                ):
                    raw.pop(key, None)
                con.execute(
                    """update download_tasks set status='started_waiting',state='downloading',
                           lifecycle_phase='downloading',external_id=null,updated_at=1,raw_json=?
                       where id=?""",
                    (json.dumps(raw), reservation["download_task_id"]),
                )
                con.execute(
                    """update queue_items set state='downloading',current_source='slskd'
                       where id='queue:1'"""
                )
            active_fence = probe.reconcile_slskd_transfer_identity_tasks(
                observed_at=100_000.0, transfer_rows=[transfer],
            )
            retired = probe.reconcile_slskd_transfer_identity_tasks(
                observed_at=100_001.0, transfer_rows=[],
            )
            replay = probe.reconcile_slskd_transfer_identity_tasks(
                observed_at=100_002.0, transfer_rows=[],
            )
        finally:
            probe.INKDROP_STATE_DB = old_db
        require(active_fence.get("retired") == 0 and active_fence.get("unchanged") == 1, active_fence)
        require(retired.get("retired") == 1, retired)
        require(replay.get("unresolved_tasks") == 0 and replay.get("retired") == 0, replay)
        with inkdrop_state.connect_read(db) as con:
            task = dict(con.execute("select * from download_tasks where id=?", (reservation["download_task_id"],)).fetchone())
            queue = dict(con.execute("select * from queue_items where id='queue:1'").fetchone())
        require(task["state"] == "failed" and task["status"] == "reservation_failed", task)
        require(queue["state"] == "queued" and queue["current_source"] is None, queue)

    for fence in ("blocked_queue", "verified_import", "active_claim"):
        with tempfile.TemporaryDirectory(prefix=f"inkdrop-slskd-id-fence-{fence}-") as temp:
            db = Path(temp) / "state.sqlite3"
            seed_queue(db)
            old_db = probe.INKDROP_STATE_DB
            try:
                probe.INKDROP_STATE_DB = db
                reservation = probe.reserve_slskd_candidate(entry(), candidate(), f"{fence} fixture")
                require(reservation.get("created"), reservation)
                unresolved = inkdrop_state.transition_slskd_candidate_task(
                    db, "queue:1", reservation["reservation_id"], "started_waiting",
                    observed_at=100.0,
                )
                require(unresolved.get("status") == "enqueue_response_ambiguous", unresolved)
                with inkdrop_state.connect(db) as con:
                    raw = json.loads(con.execute(
                        "select raw_json from download_tasks where id=?",
                        (reservation["download_task_id"],),
                    ).fetchone()[0])
                    raw.update({"reservation_deadline": 100.0, "slot_request_deadline": 100.0})
                    con.execute(
                        "update download_tasks set raw_json=? where id=?",
                        (json.dumps(raw), reservation["download_task_id"]),
                    )
                    if fence == "blocked_queue":
                        con.execute("update queue_items set state='blocked' where id='queue:1'")
                    elif fence == "verified_import":
                        con.execute(
                            """insert into import_results(
                                   id,queue_id,series_id,issue_id,status,outcome,display_phase,
                                   completion_truth,folder_imported,verified,imported_count,created_at,raw_json
                               ) values(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                "import:fence", "queue:1", "series:1", "issue:1",
                                "folder_verified", "productive", "verified", "folder",
                                1, 1, 1, 100.0, "{}",
                            ),
                        )
                    else:
                        con.execute(
                            """insert into queue_claims(
                                   queue_id,owner_id,operation,claimed_at,heartbeat_at,
                                   expires_at,raw_json
                               ) values(?,?,?,?,?,?,?)""",
                            (
                                "queue:1", "other-owner", "slskd_auto_grab_handoff",
                                100.0, 100.0, 200.0,
                                json.dumps({"reservation_id": reservation["reservation_id"]}),
                            ),
                        )
                fenced = probe.reconcile_slskd_transfer_identity_tasks(
                    observed_at=101.0, transfer_rows=[],
                )
            finally:
                probe.INKDROP_STATE_DB = old_db
            expected_retired = 0 if fence == "active_claim" else 1
            require(fenced.get("retired") == expected_retired, (fence, fenced))
            with inkdrop_state.connect_read(db) as con:
                task = dict(con.execute(
                    "select * from download_tasks where id=?",
                    (reservation["download_task_id"],),
                ).fetchone())
                claim_count = con.execute(
                    "select count(*) from queue_claims where queue_id='queue:1'"
                ).fetchone()[0]
            if fence == "active_claim":
                require(task["state"] == "queued" and task["status"] == "enqueue_response_ambiguous", (fence, task))
            else:
                require(task["state"] == "failed" and task["status"] == "reservation_failed", (fence, task))
            if fence == "active_claim":
                require(claim_count == 1, "active claim was deleted by fenced reconciliation")

    completed_transfer = dict(transfer)
    completed_transfer.update({
        "id": "transfer:completed-old",
        "state": "Completed, Succeeded",
        "stateDescription": "Completed, Succeeded",
        "percentComplete": 100,
        "bytesRemaining": 0,
        "bytesTransferred": 10_000_000,
    })
    with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-completed-owner-recovery-") as temp:
        db = Path(temp) / "state.sqlite3"
        seed_queue(db)
        old_db = probe.INKDROP_STATE_DB
        try:
            probe.INKDROP_STATE_DB = db
            reservation = probe.reserve_slskd_candidate(entry(), candidate(), "completed owner fixture")
            require(reservation.get("created"), reservation)
            started = inkdrop_state.transition_slskd_candidate_task(
                db, "queue:1", reservation["reservation_id"], "started_waiting",
                transfer_id="transfer:completed-old", observed_at=100.0,
            )
            require(started.get("ok") and started.get("external_id") == "transfer:completed-old", started)
            failed = inkdrop_state.transition_slskd_candidate_task(
                db, "queue:1", reservation["reservation_id"], "transfer_stale_unknown",
                transfer_id="transfer:completed-old", reason="simulated stale transfer", observed_at=101.0,
            )
            require(failed.get("ok") and failed.get("state") == "failed", failed)
            recovered = probe.reconcile_slskd_transfer_identity_tasks(
                observed_at=102.0, transfer_rows=[completed_transfer],
            )
            replay = probe.reconcile_slskd_transfer_identity_tasks(
                observed_at=103.0, transfer_rows=[completed_transfer],
            )
            fenced = probe.reserve_slskd_candidate(entry(), candidate(), "must not redownload completed owner")
        finally:
            probe.INKDROP_STATE_DB = old_db
        require(recovered.get("completed_recovered") == 1, recovered)
        require(replay.get("completed_recovered") == 0 and replay.get("completed_unchanged") >= 1, replay)
        require(not fenced.get("created") and fenced.get("reason") == "queue_has_active_candidate_task", fenced)
        old_resolver_db = resolver.INKDROP_STATE_DB
        try:
            resolver.INKDROP_STATE_DB = db
            import_retry_records = resolver.db_import_retry_records(min_age_seconds=0)
        finally:
            resolver.INKDROP_STATE_DB = old_resolver_db
        require(
            any(
                record.get("download_task_id") == reservation["download_task_id"]
                and record.get("external_id") == "transfer:completed-old"
                for record in import_retry_records.values()
            ),
            import_retry_records,
        )
        with inkdrop_state.connect_read(db) as con:
            tasks = [dict(row) for row in con.execute("select * from download_tasks where queue_id='queue:1'").fetchall()]
            queue = dict(con.execute("select * from queue_items where id='queue:1'").fetchone())
            wanted = dict(con.execute("select * from wanted_items where id='wanted:1'").fetchone())
        require(len(tasks) == 1, tasks)
        require(
            tasks[0]["id"] == reservation["download_task_id"]
            and tasks[0]["external_id"] == "transfer:completed-old"
            and tasks[0]["status"] == "transfer_succeeded_missing_stage"
            and tasks[0]["state"] == "import_ready",
            tasks[0],
        )
        require(queue["state"] == "importing" and queue["current_source"] == "slskd", queue)
        require(wanted["status"] == "in_progress", wanted)

    with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-completed-owner-sibling-fence-") as temp:
        db = Path(temp) / "state.sqlite3"
        seed_queue(db)
        old_db = probe.INKDROP_STATE_DB
        try:
            probe.INKDROP_STATE_DB = db
            reservation = probe.reserve_slskd_candidate(entry(), candidate(), "completed sibling fence fixture")
            require(reservation.get("created"), reservation)
            started = inkdrop_state.transition_slskd_candidate_task(
                db, "queue:1", reservation["reservation_id"], "started_waiting",
                transfer_id="transfer:completed-old", observed_at=100.0,
            )
            require(started.get("ok"), started)
            failed = inkdrop_state.transition_slskd_candidate_task(
                db, "queue:1", reservation["reservation_id"], "transfer_stale_unknown",
                transfer_id="transfer:completed-old", reason="simulated stale transfer", observed_at=101.0,
            )
            require(failed.get("ok"), failed)
            with inkdrop_state.connect(db) as con:
                con.execute(
                    """insert into download_tasks(
                       id,queue_id,wanted_id,series_id,issue_id,source,download_client,
                       external_id,candidate_identity,title,status,state,lifecycle_phase,
                       started_at,updated_at,raw_json
                    ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "task:active-sibling", "queue:1", "wanted:1", "series:1", "issue:1",
                        "slskd", "SLSKD", "transfer:active-sibling", "candidate:sibling",
                        "Series 001 sibling.cbz", "downloading", "downloading", "downloading",
                        101.0, 101.0, "{}",
                    ),
                )
            fenced = probe.reconcile_slskd_transfer_identity_tasks(
                observed_at=102.0, transfer_rows=[completed_transfer],
            )
        finally:
            probe.INKDROP_STATE_DB = old_db
        require(fenced.get("completed_recovered") == 0 and fenced.get("completed_unchanged") >= 1, fenced)
        with inkdrop_state.connect_read(db) as con:
            old_task = dict(con.execute("select * from download_tasks where id=?", (reservation["download_task_id"],)).fetchone())
            sibling = dict(con.execute("select * from download_tasks where id='task:active-sibling'").fetchone())
        require(old_task["state"] == "failed" and old_task["status"] == "transfer_stale_unknown", old_task)
        require(inkdrop_state.download_task_is_activeish(sibling), sibling)

    for legacy_case, candidate_filename, transfer_patch, expected_recovered in (
        ("exact", r"\\private-peer\Private Share\Series 001.cbz", {}, True),
        ("peer-substitution", r"\\private-peer\Private Share\Series 001.cbz", {"username": "other-peer"}, False),
        ("path-substitution", r"\\private-peer\Private Share\Series 001.cbz", {"directory": r"\\private-peer\Other Share"}, False),
        ("filename-mismatch", r"\\private-peer\Private Share\Series 001.cbz", {"filename": r"\\private-peer\Private Share\Different Series 001.cbz"}, False),
        ("size-mismatch", r"\\private-peer\Private Share\Series 001.cbz", {"size": 9_999_999}, False),
        ("wrong-unit", r"\\private-peer\Private Share\Series 002.cbz", {}, False),
    ):
        with tempfile.TemporaryDirectory(prefix=f"inkdrop-slskd-completed-legacy-{legacy_case}-") as temp:
            db = Path(temp) / "state.sqlite3"
            seed_queue(db)
            old_db = probe.INKDROP_STATE_DB
            old_actions = probe.MANUAL_REVIEW_ACTIONS_FILE
            try:
                probe.INKDROP_STATE_DB = db
                probe.MANUAL_REVIEW_ACTIONS_FILE = Path(temp) / "manual-review-actions.json"
                legacy_candidate = dict(candidate(), filename=candidate_filename)
                reservation = probe.reserve_slskd_candidate(entry(), legacy_candidate, f"legacy {legacy_case} fixture")
                require(reservation.get("created"), reservation)
                started = inkdrop_state.transition_slskd_candidate_task(
                    db, "queue:1", reservation["reservation_id"], "started_waiting",
                    transfer_id="transfer:completed-old", observed_at=100.0,
                )
                require(started.get("ok"), started)
                with inkdrop_state.connect(db) as con:
                    for table, row_id in (
                        ("download_tasks", reservation["download_task_id"]),
                        ("source_attempts", reservation["reservation_id"]),
                    ):
                        row = con.execute(f"select raw_json from {table} where id=?", (row_id,)).fetchone()
                        payload = json.loads(row[0] or "{}")
                        for key in ("exact_unit_key", "exact_unit_type", "exact_unit_number", "exact_series_identity"):
                            payload.pop(key, None)
                        con.execute(f"update {table} set raw_json=? where id=?", (json.dumps(payload), row_id))
                normalized_candidate_path = candidate_filename.replace("/", "\\").rstrip("\\")
                persisted_transfer = dict(
                    completed_transfer,
                    directory=normalized_candidate_path.rsplit("\\", 1)[0],
                    filename=normalized_candidate_path.rsplit("\\", 1)[-1],
                )
                probe.MANUAL_REVIEW_ACTIONS_FILE.write_text(
                    json.dumps({"history": {"slskd_transfer": persisted_transfer}}),
                    encoding="utf-8",
                )
                legacy_transfer = dict(persisted_transfer)
                legacy_transfer.update(transfer_patch)
                recovered = probe.reconcile_slskd_transfer_identity_tasks(
                    observed_at=102.0, transfer_rows=[legacy_transfer],
                )
            finally:
                probe.INKDROP_STATE_DB = old_db
                probe.MANUAL_REVIEW_ACTIONS_FILE = old_actions
            require(bool(recovered.get("completed_recovered")) is expected_recovered, (legacy_case, recovered))
            with inkdrop_state.connect_read(db) as con:
                task = dict(con.execute(
                    "select status,state,raw_json from download_tasks where id=?",
                    (reservation["download_task_id"],),
                ).fetchone())
            task_raw = json.loads(task["raw_json"] or "{}")
            if expected_recovered:
                require(task["state"] == "import_ready", task)
                require(task_raw.get("legacy_exact_unit_identity_recovered") is True, task_raw)
                require(task_raw.get("exact_unit_key"), task_raw)
            else:
                require(task["state"] == "downloading" and not task_raw.get("exact_unit_key"), task)

    with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-completed-owner-active-import-fence-") as temp:
        db = Path(temp) / "state.sqlite3"
        seed_queue(db)
        old_db = probe.INKDROP_STATE_DB
        try:
            probe.INKDROP_STATE_DB = db
            reservation = probe.reserve_slskd_candidate(entry(), candidate(), "completed active import fence fixture")
            require(reservation.get("created"), reservation)
            started = inkdrop_state.transition_slskd_candidate_task(
                db, "queue:1", reservation["reservation_id"], "started_waiting",
                transfer_id="transfer:completed-old", observed_at=100.0,
            )
            require(started.get("ok"), started)
            failed = inkdrop_state.transition_slskd_candidate_task(
                db, "queue:1", reservation["reservation_id"], "transfer_stale_unknown",
                transfer_id="transfer:completed-old", reason="simulated stale transfer", observed_at=101.0,
            )
            require(failed.get("ok"), failed)
            with inkdrop_state.connect(db) as con:
                queue_before = dict(con.execute("select * from queue_items where id='queue:1'").fetchone())
                con.execute(
                    """insert into import_results(
                       id,queue_id,series_id,issue_id,status,outcome,display_phase,
                       completion_truth,folder_imported,verified,imported_count,created_at,raw_json
                    ) values(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "import:active", "queue:1", "series:1", "issue:1",
                        "importing", "in_progress", "importing", "",
                        0, 0, 0, 101.0, "{}",
                    ),
                )
            fenced = probe.reconcile_slskd_transfer_identity_tasks(
                observed_at=102.0, transfer_rows=[completed_transfer],
            )
        finally:
            probe.INKDROP_STATE_DB = old_db
        require(fenced.get("completed_recovered") == 0 and fenced.get("completed_unchanged") >= 1, fenced)
        with inkdrop_state.connect_read(db) as con:
            task = dict(con.execute("select * from download_tasks where id=?", (reservation["download_task_id"],)).fetchone())
            queue_after = dict(con.execute("select * from queue_items where id='queue:1'").fetchone())
            import_row = dict(con.execute("select * from import_results where id='import:active'").fetchone())
        require(task["state"] == "failed" and task["status"] == "transfer_stale_unknown", task)
        require(queue_after["state"] == queue_before["state"] and queue_after["current_source"] == queue_before["current_source"], (queue_before, queue_after))
        require(import_row["status"] == "importing" and not int(import_row["verified"] or 0), import_row)

    for failed_completion_state in ("Completed, TimedOut", "Completed, Error", "Completed, Stalled"):
        with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-failed-completion-fence-") as temp:
            db = Path(temp) / "state.sqlite3"
            seed_queue(db)
            old_db = probe.INKDROP_STATE_DB
            try:
                probe.INKDROP_STATE_DB = db
                reservation = probe.reserve_slskd_candidate(entry(), candidate(), "failed completion fixture")
                require(reservation.get("created"), reservation)
                started = inkdrop_state.transition_slskd_candidate_task(
                    db, "queue:1", reservation["reservation_id"], "started_waiting",
                    transfer_id="transfer:failed-completion", observed_at=100.0,
                )
                require(started.get("ok"), started)
                failed_transfer = dict(completed_transfer)
                failed_transfer.update({
                    "id": "transfer:failed-completion",
                    "state": failed_completion_state,
                    "stateDescription": failed_completion_state,
                })
                reconciled = probe.reconcile_slskd_transfer_identity_tasks(
                    observed_at=101.0, transfer_rows=[failed_transfer],
                )
            finally:
                probe.INKDROP_STATE_DB = old_db
            require(
                reconciled.get("completed_recovered") == 0,
                (failed_completion_state, reconciled),
            )
            with inkdrop_state.connect_read(db) as con:
                task = dict(con.execute("select * from download_tasks where id=?", (reservation["download_task_id"],)).fetchone())
                queue = dict(con.execute("select * from queue_items where id='queue:1'").fetchone())
            require(task["state"] == "downloading" and task["status"] == "started_waiting", (failed_completion_state, task))
            require(queue["state"] == "downloading", (failed_completion_state, queue))


def automatic_handoff_negative_decision_smoke():
    def entry_for(queue_id="queue:1", *, autopilot=True):
        return {
            "review_id": f"review:{queue_id}", "series": "Series", "issue": "1",
            "autopilot_queue": autopilot, "autopilot_queue_key": queue_id,
            "queue_identity": "series:1",
        }

    def candidate_for(label):
        return {
            "filename": rf"\\private-peer\Private Share\Series 001 {label}.cbz",
            "username": "private-peer", "size": 1000, "score": 100,
            "auto_grab": {"verdict": "auto_grab_safe"},
        }

    def invoke(db, entry, candidate):
        calls = {"lookup": 0, "enqueue": 0}
        transfer = {
            "id": "transfer:authorized", "state": "InProgress",
            "filename": candidate["filename"], "username": candidate["username"],
        }

        def lookup(*_args, **_kwargs):
            calls["lookup"] += 1
            return {}

        def enqueue(*_args, **_kwargs):
            calls["enqueue"] += 1
            return {"transfers": [transfer]}

        old_db = probe.INKDROP_STATE_DB
        try:
            probe.INKDROP_STATE_DB = db
            with (
                mock.patch.object(probe, "load_auto_grab_state", return_value={}),
                mock.patch.object(probe, "save_auto_grab_state", return_value=None),
                mock.patch.object(probe, "auto_grab_review_rows", return_value=([(entry["review_id"], entry, candidate)], [], [])),
                mock.patch.object(probe, "active_auto_grab_user_load", return_value={}),
                mock.patch.object(probe, "bad_candidate_match", return_value=None),
                mock.patch.object(probe, "mark_manual_source_waiting_local", return_value={"record": {"review_id": entry["review_id"]}}),
                mock.patch.object(probe, "slskd_existing_download", side_effect=lookup),
                mock.patch.object(probe, "slskd_enqueue_candidate", side_effect=enqueue),
                mock.patch.object(probe, "auto_grab_transfer_from_enqueue", return_value={"transfer": transfer}),
                mock.patch.object(probe, "auto_grab_audit", return_value=None),
                mock.patch.object(probe, "log", return_value=None),
            ):
                outcome = probe._run_auto_grab_with_ephemeral_candidates(
                    SimpleNamespace(auto_grab_live=True, auto_grab_dry_run=False, auto_grab_max=1),
                    {"items": {entry["review_id"]: entry}},
                )
        finally:
            probe.INKDROP_STATE_DB = old_db
        return outcome["rows"][0], calls

    with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-decision-active-") as temp:
        db = Path(temp) / "state.sqlite3"
        seed_queue(db)
        with inkdrop_state.connect(db) as con:
            con.execute("update queue_items set state='queued',current_source=null where id='queue:1'")
        old_db = probe.INKDROP_STATE_DB
        try:
            probe.INKDROP_STATE_DB = db
            owner = probe.reserve_slskd_candidate(entry_for(), candidate_for("owner"), "fixture owner")
        finally:
            probe.INKDROP_STATE_DB = old_db
        require(owner.get("created"), owner)
        row, calls = invoke(db, entry_for(), candidate_for("blocked"))
        require((row.get("candidate_reservation") or {}).get("decision") == "blocked_active_owner", row)
        require(calls == {"lookup": 0, "enqueue": 0}, calls)

    with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-decision-sibling-") as temp:
        db = Path(temp) / "state.sqlite3"
        seed_queue(db)
        with inkdrop_state.connect(db) as con:
            con.execute("update queue_items set state='queued',current_source=null where id='queue:1'")
            con.execute("insert into wanted_items(id,series_id,issue_id,status,created_at,updated_at,raw_json) values('wanted:sibling','series:1','issue:1','wanted',1,1,'{}')")
            con.execute(
                "insert into queue_items(id,wanted_id,series_id,issue_id,state,active,created_at,updated_at,raw_json) values('queue:sibling','wanted:sibling','series:1','issue:1','queued',1,1,1,?)",
                (json.dumps({"series": "Series", "issue": "1", "queue_identity": "series:1", "unit_type": "issue", "issue_number": "1"}),),
            )
        old_db = probe.INKDROP_STATE_DB
        try:
            probe.INKDROP_STATE_DB = db
            owner = probe.reserve_slskd_candidate(entry_for(), candidate_for("old-owner"), "fixture owner")
        finally:
            probe.INKDROP_STATE_DB = old_db
        with inkdrop_state.connect(db) as con:
            con.executemany(
                "insert into download_tasks(id,queue_id,wanted_id,series_id,issue_id,source,title,status,state,lifecycle_phase,started_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(f"terminal:{number}", "queue:1", "wanted:1", "series:1", "issue:1", "slskd", f"terminal {number}", "failed", "failed", "failed_candidate", number, number, "{}") for number in range(1001)],
            )
        row, calls = invoke(db, entry_for("queue:sibling"), candidate_for("sibling"))
        require((row.get("candidate_reservation") or {}).get("decision") == "blocked_active_owner", row)
        require((row.get("candidate_reservation") or {}).get("reason") == "sibling_exact_unit_active", row)
        require(calls == {"lookup": 0, "enqueue": 0}, calls)

    with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-decision-complete-") as temp:
        db = Path(temp) / "state.sqlite3"
        seed_queue(db)
        with inkdrop_state.connect(db) as con:
            con.execute("update queue_items set state='queued',current_source=null where id='queue:1'")
        old_db = probe.INKDROP_STATE_DB
        try:
            probe.INKDROP_STATE_DB = db
            completed = probe.reserve_slskd_candidate(entry_for(), candidate_for("complete"), "fixture completion")
        finally:
            probe.INKDROP_STATE_DB = old_db
        with inkdrop_state.connect(db) as con:
            con.execute("update download_tasks set status='verified',state='verified',lifecycle_phase='verified' where id=?", (completed["download_task_id"],))
        row, calls = invoke(db, entry_for(), candidate_for("complete"))
        require((row.get("candidate_reservation") or {}).get("decision") == "blocked_completion", row)
        require(calls == {"lookup": 0, "enqueue": 0}, calls)

    with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-decision-expired-") as temp:
        db = Path(temp) / "state.sqlite3"
        seed_queue(db)
        with inkdrop_state.connect(db) as con:
            con.execute("update queue_items set state='queued',current_source=null where id='queue:1'")
        old_db = probe.INKDROP_STATE_DB
        try:
            probe.INKDROP_STATE_DB = db
            expired = probe.reserve_slskd_candidate(entry_for(), candidate_for("expired"), "fixture expiry")
        finally:
            probe.INKDROP_STATE_DB = old_db
        with inkdrop_state.connect(db) as con:
            raw = json.loads(con.execute("select raw_json from download_tasks where id=?", (expired["download_task_id"],)).fetchone()[0])
            raw.update({"reservation_deadline": 0, "slot_request_deadline": 0})
            con.execute("update download_tasks set raw_json=? where id=?", (json.dumps(raw), expired["download_task_id"]))
        row, calls = invoke(db, entry_for(), candidate_for("expired"))
        require((row.get("candidate_reservation") or {}).get("decision") == "retryable_rollback", row)
        require(calls == {"lookup": 0, "enqueue": 0}, calls)
        with inkdrop_state.connect(db) as con:
            con.execute("update queue_items set retry_after=0,retry_after_iso=null where id='queue:1'")
        retry_row, retry_calls = invoke(db, entry_for(), candidate_for("expired"))
        require((retry_row.get("candidate_reservation") or {}).get("decision") == "authorize_enqueue", retry_row)
        require(retry_calls == {"lookup": 1, "enqueue": 1}, retry_calls)
        with inkdrop_state.connect_read(db) as con:
            task_counts = con.execute("select count(*),sum(case when state in ('queued','downloading','import_ready','importing') then 1 else 0 end) from download_tasks where queue_id='queue:1'").fetchone()
        require(tuple(task_counts) == (2, 1), f"retry did not leave exactly one fresh active task: {tuple(task_counts)}")

    with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-decision-unbound-") as temp:
        db = Path(temp) / "state.sqlite3"
        seed_queue(db)
        row, calls = invoke(db, entry_for(autopilot=False), candidate_for("unbound"))
        require((row.get("candidate_reservation") or {}).get("decision") == "invalid_binding", row)
        require(calls == {"lookup": 0, "enqueue": 0}, calls)


def main():
    threshold_smoke()
    configured_completed_transfer_root_smoke()
    historical_missing_stage_recovery_smoke()
    persisted_stall_policy_smoke()
    durable_stall_cleanup_scope_smoke()
    durable_terminal_smoke()
    durable_verified_retry_fence_smoke()
    completed_work_precedes_replacement_search_smoke()
    durable_slot_request_lifecycle_smoke()
    durable_manga_volume_binding_smoke()
    auto_grab_slot_request_bridge_smoke()
    staged_file_path_durability_smoke()
    stale_staged_match_memory_revalidation_smoke()
    late_event_fence_smoke()
    verified_fence_smoke()
    direct_failover_smoke(True)
    direct_failover_smoke(False)
    direct_failover_smoke(True, delete_failure=True)
    verified_before_handoff_smoke()
    terminal_false_duplicate_attempt_reconciliation_smoke()
    missing_record_and_lookup_smoke()
    replay_smoke()
    authoritative_reservation_privacy_and_legacy_smoke()
    queue_only_legacy_wait_reconciliation_smoke()
    production_candidate_handoff_outcomes_smoke()
    authoritative_transfer_identity_reconciliation_smoke()
    automatic_handoff_negative_decision_smoke()
    print(json.dumps({"ok": True, "suite": "inkdrop-slskd-failover"}, sort_keys=True))


if __name__ == "__main__":
    main()
