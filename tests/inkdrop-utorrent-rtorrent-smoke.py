#!/usr/bin/env python3
"""Network-free transport contracts for the uTorrent and rTorrent adapters."""

import json
import xmlrpc.client

from core import inkdrop_acquire
from core import inkdrop_client_status
from core import inkdrop_download_clients as clients
from core import inkdrop_source_worker_coordinator as coordinator
from core import inkdrop_transfer


TORRENT = b"d4:infod4:name7:Fixture12:piece lengthi16384e6:lengthi12345eee"
INFO_HASH = clients.torrent_info_hash(TORRENT)


def require(condition, message):
    if not condition:
        raise AssertionError(message)


class Response:
    def __init__(self, content=b"", status=200):
        self.content = content
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


class TorrentFetch:
    def __init__(self, content=TORRENT):
        self.content = content
        self.calls = 0

    def get(self, _url, **kwargs):
        self.calls += 1
        require(kwargs.get("allow_redirects") is False, "metainfo fetch allowed redirects")
        return Response(self.content)


def utorrent_row(info_hash=INFO_HASH, path="/remote/comics", progress=250):
    row = [info_hash.upper(), 1, "Fixture 001", 1000, progress, 250, 0, 0, 0, 50, 15, "inkdrop"]
    row.extend([0] * (27 - len(row)))
    row[18] = 750
    row[26] = path
    return row


class UTorrentHttp:
    def __init__(self, rows=None, token_status=200):
        self.rows = list(rows or [])
        self.token_status = token_status
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        require(kwargs.get("allow_redirects") is False, "uTorrent GET allowed redirects")
        if url.endswith("token.html"):
            return Response(b"<html><div id='token'>fixture-token</div></html>", self.token_status)
        require(kwargs.get("params", {}).get("token") == "fixture-token", "uTorrent call omitted token")
        if kwargs.get("params", {}).get("action") == "add-url":
            self.rows.append(utorrent_row())
            return Response(b"{}")
        return Response(json.dumps({"torrents": self.rows}).encode())

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        require(kwargs.get("allow_redirects") is False, "uTorrent POST allowed redirects")
        require(kwargs.get("params", {}).get("token") == "fixture-token", "uTorrent upload omitted token")
        require(kwargs.get("params", {}).get("action") == "add-file", "unexpected uTorrent upload action")
        self.rows.append(utorrent_row())
        return Response(b"{}")


def rpc_response(value):
    return Response(xmlrpc.client.dumps((value,), methodresponse=True, allow_none=True).encode())


class RTorrentHttp:
    def __init__(self, rows=None, fault_multicall2=False):
        self.rows = list(rows or [])
        self.fault_multicall2 = fault_multicall2
        self.calls = []

    def post(self, _url, data=None, **kwargs):
        require(kwargs.get("allow_redirects") is False, "rTorrent POST allowed redirects")
        params, method = xmlrpc.client.loads(data)
        self.calls.append((method, params, kwargs))
        if method == "system.client_version":
            return rpc_response("0.9.8")
        if method == "d.multicall2" and self.fault_multicall2:
            fault = xmlrpc.client.Fault(1, "unsupported")
            return Response(xmlrpc.client.dumps(fault).encode())
        if method in {"d.multicall2", "d.multicall"}:
            return rpc_response(self.rows)
        if method == "load.raw_start":
            blob = bytes(params[1].data)
            self.rows.append([clients.torrent_info_hash(blob).upper(), "Fixture 001", 1, 0, 1000, 250, 50, 0, 750,
                "/remote/comics", "inkdrop-task-fixture"])
            return rpc_response(0)
        if method == "load.start":
            self.rows.append([INFO_HASH.upper(), "Fixture 001", 1, 0, 1000, 0, 0, 0, 1000,
                "/remote/comics", "inkdrop-task-fixture"])
            return rpc_response(0)
        if method in {"d.stop", "d.start", "d.erase"}:
            return rpc_response(0)
        raise AssertionError(f"unexpected XML-RPC method {method}")


def utorrent_settings():
    return {"enabled": True, "base_url": "https://private-utorrent.example:9443", "username": "private-user",
        "password": "private-password", "download_path": "/remote/comics",
        "path_mappings": [{"remote_path": "/remote", "local_path": "/local/private"}], "verify_tls": False}


def rtorrent_settings():
    return {"enabled": True, "base_url": "https://private-rtorrent.example/RPC2", "username": "private-user",
        "password": "private-password", "download_path": "/remote/comics",
        "path_mappings": [{"remote_path": "/remote", "local_path": "/local/private"}], "verify_tls": False}


def smoke_configuration_and_privacy():
    schema = clients.download_client_schemas()
    for name in ("utorrent", "rtorrent"):
        require(schema[name]["implementation_status"] == "implemented", f"{name} remains planned")
        require(schema[name]["settings"]["password"]["write_only"], f"{name} secret is not write-only")
    for bad in ("scgi://host/RPC2", "ftp://host/RPC2", "https://user:pass@host/RPC2", "https://host/RPC2?q=secret"):
        try:
            clients.validate_rtorrent_settings({"base_url": bad})
            raise AssertionError(f"unsafe endpoint accepted: {bad}")
        except ValueError:
            pass
    require(clients.validate_utorrent_settings({"enabled": False})["host"] == "", "disabled client demanded endpoint")
    u_result = clients.utorrent_test_connection(utorrent_settings(), http=UTorrentHttp())
    r_result = clients.rtorrent_test_connection(rtorrent_settings(), http=RTorrentHttp(fault_multicall2=True))
    for result in (u_result, r_result):
        serialized = json.dumps(result)
        require(result["ok"], f"connection fixture failed: {result}")
        for private in ("private-password", "private-user", "/remote/comics", "/local/private", "private-utorrent", "private-rtorrent"):
            require(private not in serialized, f"test connection leaked {private}")
    auth = clients.utorrent_test_connection(utorrent_settings(), http=UTorrentHttp(token_status=401))
    require(auth["error_type"] == "authentication", f"uTorrent auth misclassified: {auth}")


def smoke_utorrent_contract():
    existing_http = UTorrentHttp([utorrent_row()])
    existing = clients.utorrent_add("https://source.example/fixture.torrent", "Fixture", utorrent_settings(),
        unique_tag="inkdrop-task-fixture", http=existing_http, fetch_http=TorrentFetch())
    require(existing["existing"] and not existing["added"], "uTorrent duplicate was re-added")
    require(not any(method == "post" for method, _url, _kwargs in existing_http.calls), "uTorrent duplicate uploaded")
    new_http = UTorrentHttp()
    added = clients.utorrent_add("https://source.example/fixture.torrent", "Fixture", utorrent_settings(),
        unique_tag="inkdrop-task-fixture", http=new_http, fetch_http=TorrentFetch())
    require(added["added"] and added["client_external_id"] == INFO_HASH, "uTorrent add identity failed")
    require(added["local_path"] == "/local/private/comics", "uTorrent path mapping failed")
    require(sum(method == "post" for method, _url, _kwargs in new_http.calls) == 1, "uTorrent add was not exactly once")
    dry_http = UTorrentHttp()
    dry = clients.utorrent_add("https://source.example/fixture.torrent", "Fixture", utorrent_settings(), dry_run=True,
        http=dry_http, fetch_http=TorrentFetch())
    require(dry["dry_run"] and not dry_http.calls, "uTorrent dry-run contacted client")
    control_http = UTorrentHttp([utorrent_row()])
    removed = clients.utorrent_control(utorrent_settings(), INFO_HASH, "remove", http=control_http)
    require(removed["delete_data"] is False, "uTorrent remove allowed data deletion")
    require(clients.utorrent_control(utorrent_settings(), INFO_HASH, "remove-data", http=control_http)["unsupported"],
        "uTorrent destructive control accepted")
    magnet_http = UTorrentHttp()
    magnet = clients.utorrent_add(f"magnet:?xt=urn:btih:{INFO_HASH}", "Fixture", utorrent_settings(), http=magnet_http)
    require(magnet["added"] and any(call[2].get("params", {}).get("action") == "add-url" for call in magnet_http.calls),
        "uTorrent magnet handoff failed")


def smoke_rtorrent_contract():
    existing_row = [INFO_HASH.upper(), "Fixture 001", 1, 0, 1000, 250, 50, 0, 750, "/remote/comics", "old"]
    existing_http = RTorrentHttp([existing_row])
    existing = clients.rtorrent_add("https://source.example/fixture.torrent", "Fixture", rtorrent_settings(),
        unique_tag="inkdrop-task-fixture", http=existing_http, fetch_http=TorrentFetch())
    require(existing["existing"] and not existing["added"], "rTorrent duplicate was re-added")
    require(not any(method.startswith("load.") for method, _params, _kwargs in existing_http.calls), "rTorrent duplicate uploaded")
    new_http = RTorrentHttp()
    added = clients.rtorrent_add("https://source.example/fixture.torrent", "Fixture", rtorrent_settings(),
        unique_tag="inkdrop-task-fixture", http=new_http, fetch_http=TorrentFetch())
    require(added["added"] and added["client_external_id"] == INFO_HASH, "rTorrent add identity failed")
    require(added["local_path"] == "/local/private/comics", "rTorrent path mapping failed")
    require(sum(method == "load.raw_start" for method, _params, _kwargs in new_http.calls) == 1, "rTorrent add was not exactly once")
    dry_http = RTorrentHttp()
    dry = clients.rtorrent_add("https://source.example/fixture.torrent", "Fixture", rtorrent_settings(), dry_run=True,
        http=dry_http, fetch_http=TorrentFetch())
    require(dry["dry_run"] and not dry_http.calls, "rTorrent dry-run contacted client")
    removed = clients.rtorrent_control(rtorrent_settings(), INFO_HASH, "remove", http=RTorrentHttp())
    require(removed["delete_data"] is False, "rTorrent remove allowed data deletion")
    bounded = clients.RTorrentXmlRpc(rtorrent_settings(), http=RTorrentHttp([existing_row] * 600)).torrents()
    require(len(bounded) == 500, "rTorrent multicall was not bounded")
    magnet_http = RTorrentHttp()
    magnet = clients.rtorrent_add(f"magnet:?xt=urn:btih:{INFO_HASH}", "Fixture", rtorrent_settings(), http=magnet_http)
    require(magnet["added"] and any(call[0] == "load.start" for call in magnet_http.calls), "rTorrent magnet handoff failed")


def smoke_registry_and_dispatch():
    for client in ("utorrent", "rtorrent"):
        require(inkdrop_client_status.canonical_client_id(client) == client, f"{client} status ID was not registered")
        normalized = inkdrop_transfer.normalize_transfer_status({"download_client": client},
            {"status": "active", "size_bytes": 1000, "downloaded_bytes": 250, "download_speed": 50})
        require(normalized["transfer_state"] == "downloading" and normalized["percent_complete"] == 25,
            f"{client} status registry failed: {normalized}")
    captured = []
    old_u, old_r = inkdrop_acquire.utorrent_add, inkdrop_acquire.rtorrent_add
    def fake_add(_url, _title, dry_run=False, unique_tag=None):
        captured.append(unique_tag)
        return {"ok": True, "added": True, "client_external_id": INFO_HASH}
    try:
        inkdrop_acquire.utorrent_add = fake_add
        inkdrop_acquire.rtorrent_add = fake_add
        for client in ("utorrent", "rtorrent"):
            result = coordinator._default_download_client_adder({"id": "task-123", "download_client": client,
                "raw_json": {"download_url": "https://source.example/fixture.torrent"}, "title": "Fixture"})
            require(result["ok"] and result["download_client"].lower() == client, f"{client} dispatch failed: {result}")
    finally:
        inkdrop_acquire.utorrent_add, inkdrop_acquire.rtorrent_add = old_u, old_r
    require(captured == ["inkdrop-task-task-123", "inkdrop-task-task-123"], "dispatch did not preserve stable handoff identity")


def smoke_handoff_projection():
    task = {"source": "prowlarr", "download_client": "ignored", "external_id": "candidate-url",
        "title": "Fixture", "raw_json": {"download_url_hash": "locator-hash"}}
    for client, label in (("utorrent", "uTorrent"), ("rtorrent", "rTorrent")):
        attempt = coordinator._handoff_attempt_from_task(task,
            {"ok": True, "added": True, "download_client": client, "client_external_id": INFO_HASH}, now=123.0)
        require(attempt["torrent_hash"] == INFO_HASH, f"{client} torrent hash was not mirrored")
        require(attempt["external_id"] == attempt["client_external_id"] == INFO_HASH,
            f"{client} canonical external identity diverged")
        require(attempt["download_client"] == client and attempt["reason"].startswith(label + " "),
            f"{client} preferred display label missing: {attempt}")
    legacy = {
        "qbittorrent": "qBittorrent", "transmission": "Transmission", "deluge": "Deluge",
        "sabnzbd": "SABnzbd", "nzbget": "NZBGet",
    }
    for client, label in legacy.items():
        result = {"ok": True, "added": True, "download_client": client, "client_external_id": "legacy-id"}
        attempt = coordinator._handoff_attempt_from_task(task, result, now=123.0)
        require(attempt["reason"].startswith(label + " "), f"legacy {client} label changed")
        expected_hash = "legacy-id" if client in {"qbittorrent", "transmission", "deluge"} else None
        require(attempt["torrent_hash"] == expected_hash, f"legacy {client} torrent hash behavior changed")


def main():
    smoke_configuration_and_privacy()
    smoke_utorrent_contract()
    smoke_rtorrent_contract()
    smoke_registry_and_dispatch()
    smoke_handoff_projection()
    print("PASS uTorrent/rTorrent adapter smoke")


if __name__ == "__main__":
    main()
