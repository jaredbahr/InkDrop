#!/usr/bin/env python3
"""Prowlarr locators remain replayable without persisting the API key."""

import json
import tempfile
from pathlib import Path

import inkdrop_credential_evidence_cleanup
import inkdrop_acquire
import inkdrop_source_providers
import inkdrop_state


SECRET = "PROWLARR-KEY-LEAK-PROBE"
INNER_SECRET = "TRACKER-PASSKEY-LEAK-PROBE"
URL = (
    f"http://prowlarr:9696/api/v1/indexer/7/download?apikey={SECRET}"
    f"&link=https%3A%2F%2Ftracker.example%2Fdownload%3Fpasskey%3D{INNER_SECRET}&file=Safe.nzb"
)
SAFE_URL = f"http://prowlarr:9696/api/v1/indexer/7/download?apikey={SECRET}&link=opaque-token&file=Safe.nzb"


def require(value, message):
    if not value:
        raise AssertionError(message)


result = {
    "title": "Safe Series 001",
    "protocol": "usenet",
    "downloadUrl": URL,
    "api_key": SECRET,
    "headers": {"X-Api-Key": SECRET},
}
registry = {
    "provider_id": "prowlarr_test",
    "provider_type": "indexer",
    "base_url": "http://prowlarr:9696",
}
candidate = inkdrop_source_providers.prowlarr_candidate_from_result(result, registry, {"series": "Safe Series"})
serialized = json.dumps(candidate, sort_keys=True)
require(SECRET not in serialized and INNER_SECRET not in serialized, serialized)
require("tracker.example" in candidate["download_url"] and "apikey=" not in candidate["download_url"].lower(), candidate)
require(candidate["download_url_hash"] == inkdrop_source_providers.url_hash(candidate["download_url"]), candidate)
require(candidate["prowlarr_nested_credentials_redacted"] is True, candidate)
require(not inkdrop_source_providers.authorized_prowlarr_download_url(candidate, registry), candidate)
blocked = inkdrop_source_providers.indexer_candidate_verdict(candidate, {
    **registry,
    "registry_state": "ready",
    "source_mode": "auto",
    "auto_search_allowed": True,
    "auto_download_allowed": True,
})
require("credential_bearing_locator_requires_refresh" in blocked["block_reasons"], blocked)

safe_result = dict(result, downloadUrl=SAFE_URL)
safe_candidate = inkdrop_source_providers.prowlarr_candidate_from_result(safe_result, registry, {"series": "Safe Series"})
require(inkdrop_source_providers.authorized_prowlarr_download_url(safe_candidate, registry) == safe_candidate["download_url"], safe_candidate)

original_settings_loader = inkdrop_acquire.load_prowlarr_settings
original_key_loader = inkdrop_acquire.load_prowlarr_key
try:
    inkdrop_acquire.load_prowlarr_settings = lambda _media_type: {"base_url": "http://prowlarr:9696"}
    inkdrop_acquire.load_prowlarr_key = lambda: SECRET
    runtime_url = inkdrop_acquire.prowlarr_download_url_for_client(safe_candidate["download_url"])
finally:
    inkdrop_acquire.load_prowlarr_settings = original_settings_loader
    inkdrop_acquire.load_prowlarr_key = original_key_loader
require(f"apikey={SECRET}" in runtime_url and "link=opaque-token" in runtime_url, runtime_url)
require(SECRET not in safe_candidate["download_url"], "runtime rehydration mutated durable candidate state")

attempt = inkdrop_source_providers.indexer_candidate_attempt_seed(safe_candidate, registry, status="sent")
task = inkdrop_state.download_task_from_attempt(
    "queue:1", "wanted:1", "series:1", "issue:1", attempt, "attempt:1"
)
require(task and SECRET not in task["raw_json"], task)
require("opaque-token" in task["raw_json"] and "apikey=" not in task["raw_json"].lower(), task["raw_json"])

with tempfile.TemporaryDirectory(prefix="inkdrop-credential-cleanup-") as tmp:
    db_path = Path(tmp) / "state.sqlite3"
    with inkdrop_state.connect(db_path) as con:
        inkdrop_state.init_schema(con)
        for index in range(5):
            con.execute(
                "insert into history_events(id,entity_type,entity_id,event_type,source,message,created_at,raw_json) values(?,?,?,?,?,?,?,?)",
                (f"benign:{index}", "source_attempt", f"benign:{index}", "source_attempt", "test", "benign", index, "{}"),
            )
        con.execute(
            "insert into history_events(id,entity_type,entity_id,event_type,source,message,created_at,raw_json) values(?,?,?,?,?,?,?,?)",
            ("malformed:1", "source_attempt", "malformed:1", "source_attempt", "test", "malformed", 0.5, '{"api_key":"unterminated"'),
        )
        con.execute(
            "insert into history_events(id,entity_type,entity_id,event_type,source,message,created_at,raw_json) values(?,?,?,?,?,?,?,?)",
            ("history:1", "source_attempt", "attempt:1", "source_attempt", "prowlarr", f"failed api_key={SECRET}", 1, json.dumps(result)),
        )
        for index, key in enumerate(("token", "rsskey", "auth", "key"), start=2):
            alternate = f"alternate-{key}-secret"
            con.execute(
                "insert into history_events(id,entity_type,entity_id,event_type,source,message,created_at,raw_json) values(?,?,?,?,?,?,?,?)",
                (f"history:{index}", "source_attempt", f"attempt:{index}", "source_attempt", "prowlarr", "alternate", index,
                 json.dumps({"url": f"http://prowlarr:9696/api/v1/indexer/7/download?{key}={alternate}&link=opaque&file=Safe.nzb"})),
            )
        con.execute(
            "insert into download_tasks(id,source,provider_id,protocol,download_client,title,status,state,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?,?)",
            ("task:1", "prowlarr", "prowlarr_test", "usenet", "sabnzbd", "Safe Series", "failed_download", "failed", 1, json.dumps(result)),
        )
        con.execute(
            "insert into provider_configs(id,provider_type,display_name,enabled,settings_json) values(?,?,?,?,?)",
            ("prowlarr_test", "indexer", "Prowlarr", 1, json.dumps({"api_key": SECRET, "secret_fields": ["api_key"]})),
        )
        con.commit()
    cleanups = [inkdrop_credential_evidence_cleanup.cleanup_persisted_credentials(db_path, batch_size=20) for _ in range(10)]
    require(all(item["examined"] <= 20 for item in cleanups), cleanups)
    require(sum(item["changed"] for item in cleanups) >= 2, cleanups)
    require(sum(item["malformed"] for item in cleanups) == 1, cleanups)
    with inkdrop_state.connect_read(db_path) as con:
        history = con.execute("select message,raw_json from history_events where id='history:1'").fetchone()
        alternate_history = [row["raw_json"] for row in con.execute("select raw_json from history_events where id like 'history:%'")]
        malformed = con.execute("select raw_json from history_events where id='malformed:1'").fetchone()["raw_json"]
        stored_task = con.execute("select raw_json from download_tasks where id='task:1'").fetchone()
        config = con.execute("select settings_json from provider_configs where id='prowlarr_test'").fetchone()
    require(SECRET not in history["message"] and SECRET not in history["raw_json"], dict(history))
    require(all("alternate-" not in raw for raw in alternate_history), alternate_history)
    require("unterminated" not in malformed, malformed)
    require(SECRET not in stored_task["raw_json"] and INNER_SECRET not in stored_task["raw_json"] and "tracker.example" in stored_task["raw_json"], dict(stored_task))
    require(SECRET in config["settings_json"], "authoritative provider secret was modified")
    require(inkdrop_credential_evidence_cleanup.cleanup_persisted_credentials(db_path, batch_size=20)["changed"] == 0,
            "cleanup was not idempotent")

print("inkdrop-prowlarr-durable-credential-redaction-smoke: ok")
