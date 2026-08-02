#!/usr/bin/env python3
import contextlib, json, os, sqlite3, tempfile
from pathlib import Path

import inkdrop_download_client_config as config
import inkdrop_download_client_routing as routing
import inkdrop_secret_store
import inkdrop_source_worker_coordinator as coordinator
import inkdrop_state


def require(value, message):
    if not value: raise AssertionError(message)


def add(db, secrets, kind, identity, priority, **extra):
    schema = config.DEFAULT_TYPE_SCHEMAS[kind]
    secret_values = {key: f"secret-{identity}-{key}" for key in schema.get("required_secret_fields") or []}
    if schema.get("required_secret_fields_any"):
        key = schema["required_secret_fields_any"][0]; secret_values[key] = f"secret-{identity}-{key}"
    path = extra.pop("path", f"/remote/{identity}")
    payload = {"id": identity, "name": identity, "client_type": kind, "enabled": extra.pop("enabled", True),
        "priority": priority, "base_url": f"http://{identity}.example:8080", "username": "user", "category": "default",
        "categories": {"comics": identity + "-comics", "manga": identity + "-manga"},
        "download_path": path, "download_paths": {"comics": path, "manga": path + "/manga"},
        "path_mappings": [{"remote_path": path, "local_path": f"/local/{identity}"}],
        "settings": {"test_status": extra.pop("test_status", "passed")}, "secrets": secret_values,
        "provider_mappings": extra.pop("provider_mappings", [])}
    if kind == "transmission" and not secret_values: payload["username"] = ""
    return config.create_instance(db, payload, secret_root=secrets)


def main():
    with tempfile.TemporaryDirectory(prefix="inkdrop-routing-") as tmp:
        root = Path(tmp); db = root / "state.db"; secrets = root / "secrets"
        os.environ["INKDROP_DOWNLOAD_CLIENT_SECRET_DIR"] = str(secrets)
        with inkdrop_state.connect(db) as con:
            inkdrop_state.init_schema(con); inkdrop_state.init_schema(con)
            for table in ("source_attempts", "download_tasks"):
                require("download_client_instance_id" in {row[1] for row in con.execute(f"pragma table_info({table})")}, table + " instance column missing")
        add(db, secrets, "qbittorrent", "qbit-primary", 20, path="/remote/primary",
            provider_mappings=[{"provider_id": "mapped", "protocol": "torrent", "media_type": "manga"}])
        add(db, secrets, "qbittorrent", "qbit-secondary", 10, path="/remote/secondary")
        add(db, secrets, "qbittorrent", "qbit-failed", 1, test_status="failed")
        add(db, secrets, "qbittorrent", "qbit-disabled", 1, enabled=False)
        add(db, secrets, "sabnzbd", "sab-primary", 5)
        for n, kind in enumerate(("slskd", "transmission", "deluge", "nzbget", "utorrent", "rtorrent"), 30):
            add(db, secrets, kind, kind + "-primary", n)

        mapped = routing.select_instances(db, {"provider_id": "mapped", "protocol": "torrent", "download_client": "qbittorrent", "media_type": "manga"})
        require(mapped[0]["download_client_instance_id"] == "qbit-primary" and mapped[0]["routing_reason"].startswith("provider_"), "explicit mapping failed")
        require(mapped[1]["download_client_instance_id"] == "qbit-secondary", "mapped route omitted next compatible failover")
        legacy = routing.select_instances(db, {"provider_id": "other", "protocol": "torrent", "download_client": "qbittorrent"})
        ids = [row["download_client_instance_id"] for row in legacy]
        require(ids[:2] == ["qbit-secondary", "qbit-primary"], "legacy type priority failed")
        require(not {"qbit-failed", "qbit-disabled"} & set(ids), "unready instance selected")
        require(routing.select_instances(db, {"protocol": "usenet"})[0]["download_client_instance_id"] == "sab-primary", "compatible selection failed")

        reads = []; old_read = inkdrop_secret_store.read_secret
        inkdrop_secret_store.read_secret = lambda ref, **kw: reads.append(ref) or old_read(ref, **kw)
        try: chosen = routing.materialize_instance_settings(db, "qbit-secondary", "manga", secret_root=secrets)
        finally: inkdrop_secret_store.read_secret = old_read
        require(len(reads) == 1, "selection/materialization read unrelated secrets")
        require(chosen["settings"]["path_mappings"] == [{"remote_path": "/remote/secondary", "local_path": "/local/qbit-secondary"}], "path mappings unioned")
        require("secret-qbit" not in json.dumps(legacy) and "/remote/" not in json.dumps(legacy), "redacted selection leaked config")
        for kind in config.DEFAULT_TYPE_SCHEMAS:
            identity = "qbit-secondary" if kind == "qbittorrent" else "sab-primary" if kind == "sabnzbd" else kind + "-primary"
            require(routing.materialize_instance_settings(db, identity, secret_root=secrets)["client_type"] == kind, kind + " settings failed")

        require(routing.select_url_handoff_instances(db, {"protocol": "soulseek", "download_client": "slskd"}) == [],
            "SLSKD escaped into generic URL routing")
        slskd_runtime = routing.materialize_instance_settings(db, "slskd-primary", secret_root=secrets)
        before = db.read_bytes()
        blocked = routing.dispatch_instance(slskd_runtime, "slskd://user/file", "Fixture", "comics")
        require(blocked["status"] == "unsupported_mode" and blocked["reason"] == "slskd_requires_source_specific_handoff",
            "hostile generic SLSKD dispatch was not safely fenced")
        require(db.read_bytes() == before, "blocked generic SLSKD dispatch mutated state")

        import inkdrop_slskd_source_probe as slskd_probe
        slskd_probe.INKDROP_STATE_DB = db
        selected_settings = slskd_probe.apply_slskd_provider_settings()
        require(selected_settings["download_client_instance_id"] == "slskd-primary" and
            slskd_probe.slskd_api_key() == "secret-slskd-primary-api_key", "SLSKD source path did not materialize its instance")
        generic_calls = []; source_calls = []
        old_generic, old_post = routing.dispatch_instance, slskd_probe.slskd_post
        routing.dispatch_instance = lambda *_a, **_k: generic_calls.append(True)
        slskd_probe.slskd_post = lambda path, payload, timeout=30: source_calls.append((path, payload)) or {"id": "transfer-1"}
        try:
            slskd_probe.slskd_enqueue_candidate({"username": "reader", "filename": "Comics/Fixture.cbz", "size": 42}, dry_run=False)
        finally:
            routing.dispatch_instance, slskd_probe.slskd_post = old_generic, old_post
        require(source_calls == [("/transfers/downloads/reader", [{"filename": "Comics/Fixture.cbz", "size": 42}])],
            "SLSKD source-specific handoff was not used")
        require(not generic_calls and "slskd" not in coordinator.HANDOFF_DOWNLOAD_CLIENTS,
            "production SLSKD handoff invoked generic dispatcher")

        calls = []; old_dispatch = routing.dispatch_instance
        def dispatch(instance, *_args, **kwargs):
            calls.append((instance["instance_id"], kwargs.get("unique_tag")))
            return ({"ok": False, "added": False, "reason": "down"} if len(calls) == 1 else
                {"ok": True, "added": True, "client_external_id": "hash", "download_client": "qbittorrent"})
        routing.dispatch_instance = dispatch
        task = {"id": "task-1", "_runtime_db_path": str(db), "provider_id": "other", "protocol": "torrent",
            "download_client": "qbittorrent", "title": "Fixture", "raw_json": {"download_url": "https://source/file.torrent"}}
        try: result = coordinator._default_download_client_adder(task)
        finally: routing.dispatch_instance = old_dispatch
        require(result["download_client_instance_id"] == "qbit-primary" and len(calls) == 2, "bounded failover failed")
        require(calls[0][1] == calls[1][1], "failover changed stable handoff key")
        calls.clear()
        routing.dispatch_instance = lambda instance, *_a, **_k: calls.append(instance["instance_id"]) or {"ok": False, "added": True, "client_external_id": "ambiguous"}
        try: coordinator._default_download_client_adder(task)
        finally: routing.dispatch_instance = old_dispatch
        require(calls == ["qbit-secondary"], "ambiguous add duplicated handoff")

        with contextlib.closing(sqlite3.connect(db)) as con:
            con.execute("insert into series(id,title) values('series-1','Fixture')")
            con.execute("insert into queue_items(id,series_id,state,active) values('queue-1','series-1','queued',1)"); con.commit()
        attempt = {"source": "prowlarr", "protocol": "torrent", "download_client": "qbittorrent",
            "download_client_instance_id": "qbit-secondary", "external_id": "hash", "status": "download_started", "title": "Fixture"}
        inkdrop_state.record_queue_source_attempt(db, "queue-1", attempt)
        inkdrop_state.record_queue_source_attempt(db, "queue-1", {**attempt, "download_client_instance_id": "qbit-primary"})
        with contextlib.closing(sqlite3.connect(db)) as con:
            require({row[0] for row in con.execute("select download_client_instance_id from source_attempts")} == {"qbit-secondary", "qbit-primary"}, "attempt instance identity collapsed")
            require({row[0] for row in con.execute("select download_client_instance_id from download_tasks")} == {"qbit-secondary", "qbit-primary"}, "task instance identity collapsed")
        recorded = slskd_probe.record_slskd_queue_attempt({"queue_key": "queue-1", "series": "Fixture", "issue": "1"},
            {"username": "reader", "filename": "Comics/Fixture.cbz", "size": 42}, "started_waiting",
            transfer={"id": "slskd-transfer-1", "state": "Queued"})
        require(recorded.get("ok"), "SLSKD source-specific attempt did not record")
        with contextlib.closing(sqlite3.connect(db)) as con:
            require(con.execute("select download_client_instance_id from download_tasks where lower(download_client)='slskd'").fetchone()[0] == "slskd-primary",
                "SLSKD source-specific task lost instance ownership")

        empty = root / "empty.db"
        with inkdrop_state.connect(empty) as con: inkdrop_state.init_schema(con)
        require(routing.select_instances(empty, {"protocol": "torrent"}) == [], "empty store changed behavior")
        import inkdrop_acquire
        called = []; old_qbit = inkdrop_acquire.qbit_add
        inkdrop_acquire.qbit_add = lambda *_a, **_k: called.append(True) or {"ok": True, "added": True, "client_external_id": "legacy"}
        try: coordinator._default_download_client_adder({**task, "_runtime_db_path": str(empty)})
        finally: inkdrop_acquire.qbit_add = old_qbit
        require(called, "legacy provider/env/file fallback bypassed")
        print("PASS download-client instance routing smoke")


if __name__ == "__main__": main()
