#!/usr/bin/env python3

import json
import os

import inkdrop_container_scheduler as scheduler


job = scheduler.ScheduledJob("comicvine-scan", 21600, url="http://inkdrop:8796/api/comicvine/scan", critical=False)
assert scheduler.completion_schedule(job, 78, 32) == (0, 21600, "configuration_needed")


class Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return json.dumps({"ok": True, "result": {"status": "configuration_needed", "skipped": True}}).encode()


original = scheduler.urllib.request.urlopen
try:
    scheduler.urllib.request.urlopen = lambda *_args, **_kwargs: Response()
    assert scheduler.post_url(job.url, 5) == 78
finally:
    scheduler.urllib.request.urlopen = original

captured_timeout = []
original_post = scheduler.post_url
try:
    scheduler.post_url = lambda _url, timeout: captured_timeout.append(timeout) or 0
    assert scheduler.run_job(job) == 0
finally:
    scheduler.post_url = original_post
assert captured_timeout == [job.timeout_seconds]

original_open = scheduler.urllib.request.urlopen
try:
    scheduler.urllib.request.urlopen = lambda *_args, **_kwargs: (_ for _ in ()).throw(ConnectionRefusedError("web starting"))
    assert scheduler.post_url(job.url, 5) == 75
finally:
    scheduler.urllib.request.urlopen = original_open

scheduled = {item.name: item for item in scheduler.build_jobs()}
assert scheduled["queue-maintenance"].timeout_seconds == 180
assert scheduled["verified-import-projection"].command[-1] == "5"
assert scheduled["verified-import-projection"].critical is False
assert scheduled["queue-maintenance"].critical is True
assert scheduled["comicvine-scan"].url == ""
assert scheduled["comicvine-scan"].command[-2:] == ("inkdrop_internal_jobs.py", "comicvine-scan")
assert scheduled["manual-review-noop-resolve"].url == ""
assert scheduled["manual-review-noop-resolve"].command[-2:] == (
    "inkdrop_internal_jobs.py",
    "manual-review-noop-resolve",
)
manual_source_command = scheduled["manual-source-autoresolve"].command
assert manual_source_command[:4] == ("/usr/bin/flock", "-n", "-E", "75")
assert manual_source_command[4].endswith("inkdrop-manual-source-autoresolve.lock")
assert scheduler.completion_schedule(
    scheduled["manual-source-autoresolve"], 75, 0
)[2] == "deferred"
completed_import_command = scheduled["completed-import-comics"].command
assert completed_import_command[:4] == ("/usr/bin/flock", "-n", "-E", "75")
assert completed_import_command[4].endswith("inkdrop-comics-import.lock")
assert scheduler.completion_schedule(
    scheduled["completed-import-comics"], 75, 0
)[2] == "deferred"

original_maintenance_timeout = os.environ.get("INKDROP_SCHEDULER_QUEUE_MAINTENANCE_TIMEOUT_SECONDS")
try:
    os.environ["INKDROP_SCHEDULER_QUEUE_MAINTENANCE_TIMEOUT_SECONDS"] = "240"
    assert {item.name: item for item in scheduler.build_jobs()}["queue-maintenance"].timeout_seconds == 240
    os.environ["INKDROP_SCHEDULER_QUEUE_MAINTENANCE_TIMEOUT_SECONDS"] = "10"
    assert {item.name: item for item in scheduler.build_jobs()}["queue-maintenance"].timeout_seconds == 60
    os.environ["INKDROP_SCHEDULER_QUEUE_MAINTENANCE_TIMEOUT_SECONDS"] = "9999"
    assert {item.name: item for item in scheduler.build_jobs()}["queue-maintenance"].timeout_seconds == 1800
    os.environ["INKDROP_SCHEDULER_QUEUE_MAINTENANCE_TIMEOUT_SECONDS"] = "invalid"
    assert {item.name: item for item in scheduler.build_jobs()}["queue-maintenance"].timeout_seconds == 180
finally:
    if original_maintenance_timeout is None:
        os.environ.pop("INKDROP_SCHEDULER_QUEUE_MAINTENANCE_TIMEOUT_SECONDS", None)
    else:
        os.environ["INKDROP_SCHEDULER_QUEUE_MAINTENANCE_TIMEOUT_SECONDS"] = original_maintenance_timeout

projection_limit_name = "INKDROP_SCHEDULER_VERIFIED_IMPORT_PROJECTION_LIMIT"
original_projection_limit = os.environ.get(projection_limit_name)
try:
    for configured, expected in (("7", "7"), ("0", "5"), ("500", "100")):
        os.environ[projection_limit_name] = configured
        assert {item.name: item for item in scheduler.build_jobs()}[
            "verified-import-projection"
        ].command[-1] == expected
finally:
    if original_projection_limit is None:
        os.environ.pop(projection_limit_name, None)
    else:
        os.environ[projection_limit_name] = original_projection_limit

web = open("inkdrop_web.py", encoding="utf-8").read()
assert '"status": provider_status' in web
assert '"reason": "comicvine_provider_disabled"' in web
assert 'watch_log("comic_series_scan_skipped", summary)' in web
assert 'if provider_status != "configured":' in web
assert 'INKDROP_COMICVINE_SCAN_BATCH_SIZE") or 12' in web
assert 'eligible_watches.sort(key=lambda watch: float(watch.get("lastScan") or 0))' in web
assert 'eligible_watch_objects = {id(watch) for watch in eligible_watches}' in web
assert 'if id(watch) not in eligible_watch_objects:' in web

print("inkdrop ComicVine optional scheduler smoke: PASS")
