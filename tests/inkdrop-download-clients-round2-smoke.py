#!/usr/bin/env python3
import json

import inkdrop_download_clients as clients
import inkdrop_source_worker_coordinator as coordinator
import inkdrop_transfer


def require(condition, message):
    if not condition:
        raise AssertionError(message)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None, text="", content=b""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"result": "success", "arguments": {}}
        self.headers = headers or {}
        self.text = text
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400 and self.status_code != 409:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        return self._payload


class FakeTransmissionHttp:
    def __init__(self, torrents=None, add_torrent=None, auth_status=None):
        self.torrents = list(torrents or [])
        self.add_torrent = add_torrent or {
            "id": 7,
            "hashString": "abc123",
            "name": "Smoke Comic 001",
            "labels": ["inkdrop", "inkdrop-task-task-123"],
            "downloadDir": "/remote/comics",
            "status": 4,
            "percentDone": 0.25,
            "totalSize": 1000,
            "leftUntilDone": 750,
        }
        self.calls = []
        self.session_seen = False
        self.auth_status = auth_status

    def post(self, url, json=None, headers=None, auth=None, timeout=None, verify=None):
        self.calls.append({"url": url, "json": json, "headers": headers or {}, "auth": auth, "verify": verify})
        if self.auth_status:
            return FakeResponse(status_code=self.auth_status, payload={"result": "auth failed"})
        if not self.session_seen:
            self.session_seen = True
            return FakeResponse(status_code=409, headers={"X-Transmission-Session-Id": "session-1"})
        method = json.get("method")
        if method == "session-get":
            return FakeResponse(payload={"result": "success", "arguments": {"version": "4.0.6", "rpc-version": 17, "rpc-version-min": 1}})
        if method == "torrent-get":
            return FakeResponse(payload={"result": "success", "arguments": {"torrents": self.torrents}})
        if method == "torrent-add":
            self.torrents.append(self.add_torrent)
            return FakeResponse(payload={"result": "success", "arguments": {"torrent-added": self.add_torrent}})
        if method in {"torrent-stop", "torrent-start", "torrent-remove"}:
            return FakeResponse(payload={"result": "success", "arguments": {}})
        return FakeResponse(payload={"result": "unknown method", "arguments": {}})


class FakeTorrentFetch:
    def __init__(self, content):
        self.content = content
        self.calls = []

    def get(self, url, timeout=None, verify=None):
        self.calls.append({"url": url, "timeout": timeout, "verify": verify})
        return FakeResponse(content=self.content)


class FakeDelugeHttp:
    def __init__(self, torrents=None, auth_ok=True, version="2.1.1"):
        self.torrents = dict(torrents or {})
        self.auth_ok = auth_ok
        self.version = version
        self.calls = []

    def post(self, url, json=None, timeout=None, verify=None):
        self.calls.append({"url": url, "json": json, "verify": verify})
        method = json.get("method")
        if method == "auth.login":
            return FakeResponse(payload={"result": bool(self.auth_ok), "error": None})
        if not self.auth_ok:
            return FakeResponse(payload={"result": None, "error": {"message": "Authentication failed"}})
        if method == "web.connected":
            return FakeResponse(payload={"result": True, "error": None})
        if method == "daemon.info":
            return FakeResponse(payload={"result": self.version, "error": None})
        if method == "core.get_torrents_status":
            wanted = (json.get("params") or [{}])[0] or {}
            result = dict(self.torrents) if not wanted else {key: value for key, value in self.torrents.items() if key in wanted}
            return FakeResponse(payload={"result": result, "error": None})
        if method == "core.add_torrent_file":
            params = json.get("params") or []
            blob = __import__("base64").b64decode(params[1]) if len(params) > 1 else torrent_fixture()
            info_hash = clients.torrent_info_hash(blob)
            self.torrents[info_hash] = {
                "hash": info_hash,
                "name": "Deluge Comic 001.cbz",
                "state": "Downloading",
                "progress": 10,
                "total_size": 1000,
                "total_done": 100,
                "save_path": "/remote/comics",
            }
            return FakeResponse(payload={"result": info_hash, "error": None})
        if method == "core.add_torrent_magnet":
            info_hash = "abcdefabcdefabcdefabcdefabcdefabcdefabcd"
            self.torrents[info_hash] = {
                "hash": info_hash,
                "name": "Magnet Comic 001.cbz",
                "state": "Queued",
                "progress": 0,
                "total_size": 0,
                "total_done": 0,
                "save_path": "/remote/comics",
            }
            return FakeResponse(payload={"result": info_hash, "error": None})
        if method in {"core.pause_torrent", "core.resume_torrent", "core.remove_torrent"}:
            return FakeResponse(payload={"result": True, "error": None})
        return FakeResponse(payload={"result": None, "error": {"message": f"unknown {method}"}})


class FakeNzbgetHttp:
    def __init__(self, queue=None, history=None, auth_ok=True, version="24.3"):
        self.queue = list(queue or [])
        self.history = list(history or [])
        self.auth_ok = auth_ok
        self.version = version
        self.calls = []
        self.next_id = 100

    def post(self, url, json=None, auth=None, timeout=None, verify=None):
        self.calls.append({"url": url, "json": json, "auth": auth, "verify": verify})
        if not self.auth_ok:
            return FakeResponse(status_code=401, payload={"error": {"message": "authentication failed"}})
        method = json.get("method")
        params = json.get("params") or []
        if method == "version":
            return FakeResponse(payload={"result": self.version, "error": None})
        if method == "status":
            return FakeResponse(payload={"result": {"DownloadPaused": False}, "error": None})
        if method == "listgroups":
            return FakeResponse(payload={"result": self.queue, "error": None})
        if method == "history":
            return FakeResponse(payload={"result": self.history, "error": None})
        if method == "append":
            self.next_id += 1
            filename, content, category, _priority, _top, paused, dupe_key = params[:7]
            self.queue.append(
                {
                    "NZBID": self.next_id,
                    "NZBName": filename.rsplit(".", 1)[0],
                    "NZBFilename": filename,
                    "URL": content,
                    "Category": category,
                    "Status": "paused" if paused else "queued",
                    "Paused": bool(paused),
                    "DupeKey": dupe_key,
                    "FileSizeMB": 10,
                    "RemainingSizeMB": 10,
                    "DestDir": "/remote/comics/NZBGet Comic 001",
                }
            )
            return FakeResponse(payload={"result": self.next_id, "error": None})
        if method == "editqueue":
            return FakeResponse(payload={"result": True, "error": None})
        return FakeResponse(payload={"result": None, "error": {"message": f"unknown {method}"}})


def transmission_settings():
    return {
        "enabled": True,
        "base_url": "http://transmission.example",
        "username": "inkdrop",
        "password": "secret-password",
        "category": "comics",
        "label": "inkdrop",
        "download_path": "/remote/comics",
        "path_mappings": [{"remote_path": "/remote", "local_path": "/mnt/downloads"}],
        "verify_tls": False,
    }


def deluge_settings():
    return {
        "enabled": True,
        "base_url": "http://deluge.example",
        "password": "deluge-secret",
        "category": "comics",
        "label": "inkdrop",
        "download_path": "/remote/comics",
        "path_mappings": [{"remote_path": "/remote", "local_path": "/mnt/downloads"}],
        "verify_tls": False,
    }


def nzbget_settings():
    return {
        "enabled": True,
        "base_url": "http://nzbget.example",
        "username": "inkdrop",
        "password": "nzbget-secret",
        "category": "comics",
        "label": "inkdrop",
        "download_path": "/remote/comics",
        "path_mappings": [{"remote_path": "/remote", "local_path": "/mnt/downloads"}],
        "verify_tls": False,
    }


def torrent_fixture():
    return b"d4:infod4:name13:Deluge Fixture12:piece lengthi16384e6:lengthi12345eee"


def smoke_schema_and_redaction():
    schema = clients.download_client_schemas()
    for key in ("transmission", "deluge", "nzbget", "utorrent", "rtorrent"):
        require(key in schema, f"{key} schema missing")
        require(schema[key]["settings"]["password"]["write_only"], f"{key} password must be write-only")
    redacted = clients.redact({"password": "secret-password", "base_url": "http://example"})
    require(redacted["password"] == "<redacted>", "password leaked from redaction")
    require("secret-password" not in json.dumps(schema), "schema must not contain runtime secret")


def smoke_transmission_test_connection():
    fake = FakeTransmissionHttp()
    result = clients.transmission_test_connection(transmission_settings(), http=fake)
    require(result["ok"], f"test connection failed: {result}")
    require(result["version"] == "4.0.6", "version was not discovered")
    require(result["capabilities"]["labels"] is True, "label capability missing")
    require(result["settings"]["password"] == "<redacted>", "connection result leaked password")
    auth = clients.transmission_test_connection({**transmission_settings(), "password": "wrong"}, http=FakeTransmissionHttp(auth_status=401))
    require(auth["error_type"] == "authentication", f"auth error misclassified: {auth}")
    config = clients.transmission_test_connection({"enabled": True}, http=FakeTransmissionHttp())
    require(config["error_type"] == "configuration", f"config error misclassified: {config}")


def smoke_deluge_test_connection():
    result = clients.deluge_test_connection(deluge_settings(), http=FakeDelugeHttp())
    require(result["ok"], f"deluge test connection failed: {result}")
    require(result["version"] == "2.1.1", "deluge version was not discovered")
    require(result["capabilities"]["torrent_info_hash_identity_required"] is True, "deluge identity capability missing")
    require(result["settings"]["password"] == "<redacted>", "deluge connection leaked password")
    auth = clients.deluge_test_connection(deluge_settings(), http=FakeDelugeHttp(auth_ok=False))
    require(auth["error_type"] == "authentication", f"deluge auth error misclassified: {auth}")
    config = clients.deluge_test_connection({"enabled": True}, http=FakeDelugeHttp())
    require(config["error_type"] == "configuration", f"deluge config error misclassified: {config}")


def smoke_nzbget_test_connection():
    result = clients.nzbget_test_connection(nzbget_settings(), http=FakeNzbgetHttp())
    require(result["ok"], f"nzbget test connection failed: {result}")
    require(result["version"] == "24.3", "nzbget version was not discovered")
    require(result["capabilities"]["append"] is True and result["capabilities"]["editqueue"] is True, "nzbget capabilities missing")
    require(result["settings"]["password"] == "<redacted>", "nzbget connection leaked password")
    require(result["settings"]["api_key"] == "<redacted>", "nzbget api key alias leaked")
    auth = clients.nzbget_test_connection(nzbget_settings(), http=FakeNzbgetHttp(auth_ok=False))
    require(auth["error_type"] == "authentication", f"nzbget auth error misclassified: {auth}")
    config = clients.nzbget_test_connection({"enabled": True}, http=FakeNzbgetHttp())
    require(config["error_type"] == "configuration", f"nzbget config error misclassified: {config}")


def smoke_transmission_existing_job_idempotency():
    existing = {
        "id": 5,
        "hashString": "hash-existing",
        "name": "Smoke Comic 001",
        "labels": ["inkdrop", "inkdrop-task-task-123"],
        "downloadDir": "/remote/comics",
        "status": 4,
        "percentDone": 0.5,
        "totalSize": 2000,
        "leftUntilDone": 1000,
    }
    fake = FakeTransmissionHttp(torrents=[existing])
    result = clients.transmission_add(
        "http://prowlarr/download/1",
        "Smoke Comic 001",
        transmission_settings(),
        unique_tag="inkdrop-task-task-123",
        http=fake,
    )
    require(result["ok"] and result["existing"] and not result["added"], f"existing result wrong: {result}")
    require(not any(call["json"]["method"] == "torrent-add" for call in fake.calls), "existing handoff added duplicate torrent")


def smoke_transmission_new_handoff_and_mapping():
    torrent = {
        "id": 7,
        "hashString": "hash-new",
        "name": "Smoke Comic 001.cbz",
        "labels": ["inkdrop", "inkdrop-task-task-456"],
        "downloadDir": "/remote/comics",
        "status": 6,
        "percentDone": 1,
        "totalSize": 3000,
        "leftUntilDone": 0,
        "rateDownload": 0,
        "rateUpload": 40,
        "eta": -1,
        "addedDate": 100,
        "doneDate": 200,
    }
    fake = FakeTransmissionHttp(add_torrent=torrent)
    result = clients.transmission_add(
        "http://prowlarr/download/2",
        "Smoke Comic 001",
        transmission_settings(),
        unique_tag="inkdrop-task-task-456",
        http=fake,
    )
    add_calls = [call for call in fake.calls if call["json"]["method"] == "torrent-add"]
    require(result["ok"] and result["added"], f"new result wrong: {result}")
    require(len(add_calls) == 1, "new handoff should submit once")
    require("inkdrop-task-task-456" in add_calls[0]["json"]["arguments"]["labels"], "handoff label missing")
    require(result["local_path"] == "/mnt/downloads/comics/Smoke Comic 001.cbz", f"path mapping failed: {result}")
    require(result["download_client_snapshot"]["seeding"] is True, "seeding state not preserved")


def smoke_deluge_existing_job_idempotency():
    blob = torrent_fixture()
    info_hash = clients.torrent_info_hash(blob)
    existing = {
        "hash": info_hash,
        "name": "Deluge Comic 001.cbz",
        "state": "Downloading",
        "progress": 44.5,
        "total_size": 2000,
        "total_done": 890,
        "download_payload_rate": 123,
        "upload_payload_rate": 4,
        "eta": 55,
        "save_path": "/remote/comics",
    }
    fake = FakeDelugeHttp(torrents={info_hash: existing})
    result = clients.deluge_add(
        "http://prowlarr/download/deluge-existing",
        "Deluge Comic 001",
        deluge_settings(),
        unique_tag="inkdrop-task-deluge-123",
        http=fake,
        fetch_http=FakeTorrentFetch(blob),
    )
    require(result["ok"] and result["existing"] and not result["added"], f"deluge existing result wrong: {result}")
    require(not any(call["json"]["method"] == "core.add_torrent_file" for call in fake.calls), "deluge existing handoff added duplicate torrent")


def smoke_deluge_new_handoff_and_mapping():
    blob = torrent_fixture()
    info_hash = clients.torrent_info_hash(blob)
    fake = FakeDelugeHttp()
    result = clients.deluge_add(
        "http://prowlarr/download/deluge-new",
        "Deluge Comic 001",
        deluge_settings(),
        unique_tag="inkdrop-task-deluge-456",
        http=fake,
        fetch_http=FakeTorrentFetch(blob),
    )
    add_calls = [call for call in fake.calls if call["json"]["method"] == "core.add_torrent_file"]
    require(result["ok"] and result["added"], f"deluge new result wrong: {result}")
    require(len(add_calls) == 1, "deluge new handoff should submit once")
    require(add_calls[0]["json"]["params"][2]["download_location"] == "/remote/comics", "deluge download path missing")
    require(result["client_external_id"] == info_hash, "deluge did not preserve torrent identity")


def smoke_deluge_magnet_handoff_identity():
    info_hash = "abcdefabcdefabcdefabcdefabcdefabcdefabcd"
    fake = FakeDelugeHttp()
    result = clients.deluge_add(
        f"magnet:?xt=urn:btih:{info_hash}&dn=Magnet+Comic",
        "Magnet Comic 001",
        deluge_settings(),
        unique_tag="inkdrop-task-deluge-magnet",
        http=fake,
    )
    require(result["ok"] and result["torrent_hash"] == info_hash, f"deluge magnet identity failed: {result}")


def smoke_nzbget_existing_job_idempotency():
    handoff = clients.nzbget_handoff_key("NZBGet Comic 001", "http://prowlarr/download/nzb", unique_tag="inkdrop-task-nzbget-123")
    existing = {
        "NZBID": 44,
        "NZBName": "NZBGet Comic 001",
        "Status": "downloading",
        "DupeKey": handoff,
        "FileSizeMB": 20,
        "RemainingSizeMB": 5,
        "DestDir": "/remote/comics/NZBGet Comic 001",
    }
    fake = FakeNzbgetHttp(queue=[existing])
    result = clients.nzbget_add(
        "http://prowlarr/download/nzb",
        "NZBGet Comic 001",
        nzbget_settings(),
        unique_tag="inkdrop-task-nzbget-123",
        http=fake,
    )
    require(result["ok"] and result["existing"] and not result["added"], f"nzbget existing result wrong: {result}")
    require(not any(call["json"]["method"] == "append" for call in fake.calls), "nzbget existing handoff appended duplicate")


def smoke_nzbget_new_handoff_and_mapping():
    fake = FakeNzbgetHttp()
    result = clients.nzbget_add(
        "http://prowlarr/download/nzb-new",
        "NZBGet Comic 001",
        nzbget_settings(),
        unique_tag="inkdrop-task-nzbget-456",
        http=fake,
    )
    append_calls = [call for call in fake.calls if call["json"]["method"] == "append"]
    require(result["ok"] and result["added"], f"nzbget new result wrong: {result}")
    require(len(append_calls) == 1, "nzbget new handoff should append once")
    params = append_calls[0]["json"]["params"]
    require(params[1] == "http://prowlarr/download/nzb-new", "nzbget append content should carry URL")
    require(params[2] == "comics", "nzbget append category missing")
    require(params[6] == "inkdrop-task-nzbget-456", "nzbget duplicate key missing")
    require(params[8] == "SCORE", "nzbget duplicate mode should be SCORE")


def smoke_nzbget_history_reconciliation():
    handoff = clients.nzbget_handoff_key("NZBGet Done 001", "http://prowlarr/download/done", unique_tag="inkdrop-task-nzbget-done")
    history = {
        "NZBID": 55,
        "Name": "NZBGet Done 001",
        "NZBName": "NZBGet Done 001",
        "Status": "SUCCESS/ALL",
        "DupeKey": handoff,
        "FileSizeMB": 30,
        "DownloadedSizeMB": 30,
        "DestDir": "/remote/comics/NZBGet Done 001",
        "HistoryTime": 200,
    }
    fake = FakeNzbgetHttp(history=[history])
    result = clients.nzbget_add(
        "http://prowlarr/download/done",
        "NZBGet Done 001",
        nzbget_settings(),
        unique_tag="inkdrop-task-nzbget-done",
        http=fake,
    )
    require(result["ok"] and result["existing"] and result["local_path"] == "/mnt/downloads/comics/NZBGet Done 001", f"nzbget history reconcile failed: {result}")
    require(not any(call["json"]["method"] == "append" for call in fake.calls), "nzbget history reconcile appended duplicate")


def smoke_progress_and_failure_normalization():
    stalled = clients.transmission_status(
        {
            "id": 8,
            "hashString": "hash-stalled",
            "name": "Stalled.cbz",
            "downloadDir": "/remote/comics",
            "status": 4,
            "percentDone": 0.2,
            "totalSize": 1000,
            "leftUntilDone": 800,
            "isStalled": True,
        },
        transmission_settings(),
        now=300,
    )
    require(stalled["transfer_state"] == "stalled", f"stalled state not normalized: {stalled}")
    failed = clients.transmission_status(
        {
            "id": 9,
            "hashString": "hash-failed",
            "status": 4,
            "error": 3,
            "errorString": "tracker error",
        },
        transmission_settings(),
    )
    require(failed["transfer_state"] == "failed" and failed["client_error"] == "tracker error", f"failure not normalized: {failed}")
    generic = inkdrop_transfer.normalize_transfer_status({"download_client": "transmission"}, {"status": "seeding", "percent_complete": 100})
    require(generic["transfer_state"] == "seeding", f"generic seeding telemetry regressed: {generic}")
    deluge_done = clients.deluge_status(
        {
            "hash": "hash-deluge",
            "name": "Done.cbz",
            "state": "Seeding",
            "progress": 100,
            "total_size": 1000,
            "total_done": 1000,
            "download_payload_rate": 0,
            "upload_payload_rate": 20,
            "save_path": "/remote/comics",
            "is_seed": True,
        },
        deluge_settings(),
        now=500,
    )
    require(deluge_done["transfer_state"] == "seeding", f"deluge seeding not normalized: {deluge_done}")
    require(deluge_done["completed_output_path"] == "/mnt/downloads/comics/Done.cbz", f"deluge path mapping failed: {deluge_done}")
    deluge_failed = clients.deluge_status({"hash": "bad", "state": "Error", "message": "missing files"}, deluge_settings())
    require(deluge_failed["transfer_state"] == "failed" and deluge_failed["client_error"] == "missing files", f"deluge failure not normalized: {deluge_failed}")
    generic_deluge = inkdrop_transfer.normalize_transfer_status({"download_client": "deluge"}, {"status": "paused", "progress": 12})
    require(generic_deluge["transfer_state"] == "paused", f"generic deluge paused telemetry regressed: {generic_deluge}")
    nzb_done = clients.nzbget_status(
        {
            "_inkdrop_section": "history",
            "NZBID": 99,
            "NZBName": "Done",
            "Status": "SUCCESS/ALL",
            "FileSizeMB": 10,
            "DownloadedSizeMB": 10,
            "DestDir": "/remote/comics/Done",
            "HistoryTime": 100,
        },
        nzbget_settings(),
        now=500,
    )
    require(nzb_done["transfer_state"] == "completed", f"nzbget completed not normalized: {nzb_done}")
    require(nzb_done["completed_output_path"] == "/mnt/downloads/comics/Done", f"nzbget path mapping failed: {nzb_done}")
    nzb_failed = clients.nzbget_status({"_inkdrop_section": "history", "NZBID": 98, "Status": "FAILURE/UNPACK"}, nzbget_settings())
    require(nzb_failed["transfer_state"] == "failed" and nzb_failed["client_error"] == "FAILURE/UNPACK", f"nzbget failure not normalized: {nzb_failed}")
    nzb_removed = clients.nzbget_status({"_inkdrop_section": "history", "NZBID": 97, "Status": "DELETED/MANUAL"}, nzbget_settings())
    require(nzb_removed["transfer_state"] == "removed", f"nzbget removed not normalized: {nzb_removed}")
    generic_nzb = inkdrop_transfer.normalize_transfer_status({"download_client": "nzbget"}, {"status": "SUCCESS/ALL", "FileSizeMB": 5, "DownloadedSizeMB": 5})
    require(generic_nzb["transfer_state"] == "completed", f"generic nzbget completed telemetry regressed: {generic_nzb}")


def smoke_transmission_controls_are_safe():
    fake = FakeTransmissionHttp()
    result = clients.transmission_control(transmission_settings(), "hash-new", "remove", http=fake)
    require(result["ok"] and result["delete_data"] is False, f"remove should not delete data by default: {result}")
    remove = [call for call in fake.calls if call["json"]["method"] == "torrent-remove"][-1]
    require(remove["json"]["arguments"]["delete-local-data"] is False, "delete-local-data must remain false")
    unsupported = clients.transmission_control(transmission_settings(), "hash-new", "force-delete", http=FakeTransmissionHttp())
    require(unsupported["unsupported"], "unsupported control should be explicit")


def smoke_deluge_controls_are_safe():
    fake = FakeDelugeHttp()
    result = clients.deluge_control(deluge_settings(), "hash-new", "remove", http=fake)
    require(result["ok"] and result["delete_data"] is False, f"deluge remove should not delete data by default: {result}")
    remove = [call for call in fake.calls if call["json"]["method"] == "core.remove_torrent"][-1]
    require(remove["json"]["params"] == [["hash-new"], False] or remove["json"]["params"] == ["hash-new", False], "deluge remove must keep data")
    unsupported = clients.deluge_control(deluge_settings(), "hash-new", "force-delete", http=FakeDelugeHttp())
    require(unsupported["unsupported"], "deluge unsupported control should be explicit")


def smoke_nzbget_controls_are_safe():
    fake = FakeNzbgetHttp()
    result = clients.nzbget_control(nzbget_settings(), 101, "remove", http=fake)
    require(result["ok"] and result["delete_data"] is False, f"nzbget remove should not delete data by default: {result}")
    edit = [call for call in fake.calls if call["json"]["method"] == "editqueue"][-1]
    require(edit["json"]["params"] == ["GroupDelete", "", [101]], f"nzbget remove should use GroupDelete: {edit}")
    unsupported = clients.nzbget_control(nzbget_settings(), 101, "force-delete", http=FakeNzbgetHttp())
    require(unsupported["unsupported"], "nzbget unsupported control should be explicit")


def smoke_worker_restart_identity_propagation():
    task = {
        "id": "task-abcdef1234567890",
        "download_client": "transmission",
        "protocol": "torrent",
        "title": "Worker Smoke 001",
        "raw_json": {"download_url": "http://prowlarr/download/3", "download_url_hash": "urlhash"},
    }
    seen = {}

    def fake_add(download_url, title, dry_run=False, unique_tag=None):
        seen["download_url"] = download_url
        seen["title"] = title
        seen["unique_tag"] = unique_tag
        return {
            "ok": True,
            "added": False,
            "existing": True,
            "download_client": "Transmission",
            "protocol": "torrent",
            "client_external_id": "hash-worker",
            "torrent_hash": "hash-worker",
            "handoff_tag": unique_tag,
        }

    old = coordinator.inkdrop_acquire.transmission_add if hasattr(coordinator, "inkdrop_acquire") else None
    import inkdrop_acquire

    old = inkdrop_acquire.transmission_add
    try:
        inkdrop_acquire.transmission_add = fake_add
        result = coordinator._default_download_client_adder(task)
    finally:
        inkdrop_acquire.transmission_add = old
    require(result["ok"] and result["client_external_id"] == "hash-worker", f"worker result failed: {result}")
    require(seen["unique_tag"] == "inkdrop-task-task-abcdef1234567890", f"stable task identity missing: {seen}")
    attempt = coordinator._handoff_attempt_from_task(task, result, now=123)
    require(attempt["download_client"] == "transmission", f"attempt client mismatch: {attempt}")
    require(attempt["torrent_hash"] == "hash-worker", f"attempt did not preserve torrent identity: {attempt}")


def smoke_worker_restart_identity_propagation_deluge():
    task = {
        "id": "task-deluge1234567890",
        "download_client": "deluge",
        "protocol": "torrent",
        "title": "Worker Deluge 001",
        "raw_json": {"download_url": "http://prowlarr/download/4", "download_url_hash": "urlhash-deluge"},
    }
    seen = {}

    def fake_add(download_url, title, dry_run=False, unique_tag=None):
        seen["download_url"] = download_url
        seen["title"] = title
        seen["unique_tag"] = unique_tag
        return {
            "ok": True,
            "added": False,
            "existing": True,
            "download_client": "Deluge",
            "protocol": "torrent",
            "client_external_id": "hash-deluge-worker",
            "torrent_hash": "hash-deluge-worker",
            "handoff_tag": unique_tag,
        }

    import inkdrop_acquire

    old = inkdrop_acquire.deluge_add
    try:
        inkdrop_acquire.deluge_add = fake_add
        result = coordinator._default_download_client_adder(task)
    finally:
        inkdrop_acquire.deluge_add = old
    require(result["ok"] and result["client_external_id"] == "hash-deluge-worker", f"deluge worker result failed: {result}")
    require(seen["unique_tag"] == "inkdrop-task-task-deluge1234567890", f"deluge stable task identity missing: {seen}")
    attempt = coordinator._handoff_attempt_from_task(task, result, now=124)
    require(attempt["download_client"] == "deluge", f"deluge attempt client mismatch: {attempt}")
    require(attempt["torrent_hash"] == "hash-deluge-worker", f"deluge attempt did not preserve torrent identity: {attempt}")


def smoke_worker_restart_identity_propagation_nzbget():
    task = {
        "id": "task-nzbget1234567890",
        "download_client": "nzbget",
        "protocol": "usenet",
        "title": "Worker NZBGet 001",
        "raw_json": {"download_url": "http://prowlarr/download/5", "download_url_hash": "urlhash-nzbget"},
    }
    seen = {}

    def fake_add(download_url, title, dry_run=False, unique_tag=None):
        seen["download_url"] = download_url
        seen["title"] = title
        seen["unique_tag"] = unique_tag
        return {
            "ok": True,
            "added": False,
            "existing": True,
            "download_client": "NZBGet",
            "protocol": "usenet",
            "client_external_id": "77",
            "nzo_id": "77",
            "handoff_key": unique_tag,
        }

    import inkdrop_acquire

    old = inkdrop_acquire.nzbget_add
    try:
        inkdrop_acquire.nzbget_add = fake_add
        result = coordinator._default_download_client_adder(task)
    finally:
        inkdrop_acquire.nzbget_add = old
    require(result["ok"] and result["client_external_id"] == "77", f"nzbget worker result failed: {result}")
    require(seen["unique_tag"] == "inkdrop-task-task-nzbget1234567890", f"nzbget stable task identity missing: {seen}")
    attempt = coordinator._handoff_attempt_from_task(task, result, now=125)
    require(attempt["download_client"] == "nzbget", f"nzbget attempt client mismatch: {attempt}")
    require(attempt["nzo_id"] == "77", f"nzbget attempt did not preserve nzb identity: {attempt}")


def main():
    smoke_schema_and_redaction()
    smoke_transmission_test_connection()
    smoke_deluge_test_connection()
    smoke_nzbget_test_connection()
    smoke_transmission_existing_job_idempotency()
    smoke_transmission_new_handoff_and_mapping()
    smoke_deluge_existing_job_idempotency()
    smoke_deluge_new_handoff_and_mapping()
    smoke_deluge_magnet_handoff_identity()
    smoke_nzbget_existing_job_idempotency()
    smoke_nzbget_new_handoff_and_mapping()
    smoke_nzbget_history_reconciliation()
    smoke_progress_and_failure_normalization()
    smoke_transmission_controls_are_safe()
    smoke_deluge_controls_are_safe()
    smoke_nzbget_controls_are_safe()
    smoke_worker_restart_identity_propagation()
    smoke_worker_restart_identity_propagation_deluge()
    smoke_worker_restart_identity_propagation_nzbget()
    print(json.dumps({"ok": True, "download_clients_round2_smoke": "passed"}, indent=2))


if __name__ == "__main__":
    main()
