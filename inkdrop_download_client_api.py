#!/usr/bin/env python3
"""Instance-scoped download-client API service without runtime routing changes."""

from __future__ import annotations

import concurrent.futures
import re
import threading
import time
import uuid

import requests

import inkdrop_download_client_config as config_store
import inkdrop_download_client_routing
import inkdrop_download_clients
import inkdrop_operator_contracts
import inkdrop_qbittorrent_auth
import inkdrop_state


MAX_BODY_BYTES = 65536
TEST_TIMEOUT_SECONDS = 10.0
TEST_ALL_DEADLINE_SECONDS = 30.0
TEST_ALL_CONCURRENCY = 4
TEST_RUN_TTL_SECONDS = 300.0
TEST_RUN_LIMIT = 32
STATUS_TTL_SECONDS = 15.0
SECRET_PATTERN = re.compile(r"password|passphrase|api_?key|token|secret|cookie|reference|secret_ref", re.I)

_LOCK = threading.Lock()
_TEST_RUNS = {}
_STATUS_CACHE = {}


class SafeSession:
    """Requests session enforcing bounded timeouts and disabled redirects."""

    def __init__(self):
        self._session = requests.Session()
        # Persistent per-session headers, so auth helpers can arm this the same
        # way they arm a real requests.Session.
        self.headers = {}

    def _call(self, method, url, **kwargs):
        timeout = kwargs.get("timeout") or TEST_TIMEOUT_SECONDS
        if isinstance(timeout, tuple):
            timeout = tuple(min(float(part), TEST_TIMEOUT_SECONDS) for part in timeout)
        else:
            timeout = min(float(timeout), TEST_TIMEOUT_SECONDS)
        kwargs["timeout"] = timeout
        kwargs["allow_redirects"] = False
        if self.headers:
            kwargs["headers"] = {**self.headers, **(kwargs.get("headers") or {})}
        response = self._session.request(method, url, **kwargs)
        status = int(getattr(response, "status_code", 200) or 200)
        if 300 <= status < 400:
            # Tests do not follow redirects, so say where it wanted to go --
            # otherwise a proxy that adds a trailing slash or upgrades to HTTPS
            # reads as a generic connectivity failure.
            target = str((getattr(response, "headers", None) or {}).get("Location") or "").strip()
            raise RuntimeError(
                f"the URL redirected (HTTP {status}"
                + (f" to {target[:200]}" if target else "")
                + "). Configure the client with the address it redirects to."
            )
        return response

    def get(self, url, **kwargs):
        return self._call("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._call("POST", url, **kwargs)


def implemented_registry():
    schemas = inkdrop_download_clients.download_client_schemas()
    existing = {
        str(row.get("client_id") or row.get("client_type") or "").lower(): dict(row)
        for row in (inkdrop_operator_contracts.download_client_registry().get("clients") or [])
    }
    labels = {
        "qbittorrent": ("qBittorrent", ["torrent"]), "sabnzbd": ("SABnzbd", ["usenet"]),
        "slskd": ("SLSKD", ["soulseek"]), "transmission": ("Transmission", ["torrent"]),
        "deluge": ("Deluge", ["torrent"]), "nzbget": ("NZBGet", ["usenet"]),
        "utorrent": ("uTorrent", ["torrent"]), "rtorrent": ("rTorrent", ["torrent"]),
    }
    rows = []
    for client_type, (label, protocols) in labels.items():
        schema, base = dict(schemas.get(client_type) or {}), existing.get(client_type) or {}
        rows.append({
            **base, "client_id": client_type, "client_type": client_type,
            "display_name": base.get("display_name") or schema.get("display_name") or label,
            "implemented": True, "addable": True, "testable": True, "test": True,
            "supported_protocols": base.get("supported_protocols") or ([schema.get("protocol")] if schema.get("protocol") else protocols),
            "configuration_schema": "inkdrop.download_client_instances.v1", "disabled_reason": None,
            "fields": schema.get("settings") or {},
        })
    ids = [row["client_id"] for row in rows]
    return {"schema": "inkdrop.download_client_instances.registry.v1", "clients": rows, "implemented": ids, "addable": ids, "testable": ids}


def schema_resolver(client_type):
    return config_store.DEFAULT_TYPE_SCHEMAS.get(str(client_type or "").lower())


def storage_payload(payload):
    raw = dict(payload or {})
    if len(raw) > 64:
        raise ValueError("download-client payload exceeds 64 fields")
    client_type = str(raw.get("client_type") or "").lower()
    schema = schema_resolver(client_type) or {}
    had_secrets, had_settings = "secrets" in raw, "settings" in raw
    secrets = dict(raw.get("secrets") or {}) if isinstance(raw.get("secrets"), dict) else raw.get("secrets")
    settings = dict(raw.get("settings") or {}) if isinstance(raw.get("settings"), dict) else raw.get("settings")
    if secrets is None:
        secrets = {}
    if settings is None:
        settings = {}
    secret_fields = set(schema.get("secret_fields") or [])
    first_class = {"id", "name", "client_type", "enabled", "priority", "base_url", "username", "category", "download_path", "categories", "download_paths", "path_mappings", "provider_mappings", "secrets", "clear_secret_fields", "revision", "expected_revision"}
    for key in list(raw):
        if key in first_class:
            continue
        value = raw.pop(key)
        if key in secret_fields:
            if not isinstance(secrets, dict):
                raise ValueError("secrets must be an object")
            secrets[key] = value
        else:
            if not isinstance(settings, dict):
                raise ValueError("settings must be an object")
            settings[key] = value
    if had_secrets or secrets:
        raw["secrets"] = secrets
    else:
        raw.pop("secrets", None)
    if had_settings or settings:
        raw["settings"] = settings
    else:
        raw.pop("settings", None)
    return raw


def list_payload(db_path):
    payload = {"ok": True, "registry": implemented_registry(), **config_store.list_instances(db_path)}
    instances = payload.get("instances") or []
    if any(str(row.get("client_type") or "").lower() == "slskd" for row in instances):
        # SLSKD is unique in this system: an instance row here doesn't just add a
        # second connection, it can fully take over from the single SLSKD provider
        # card once it satisfies slskd_source_instance()'s readiness check (enabled,
        # base_url, api_key). Surface exactly which instance (if any) the routing
        # layer actually picked, so the UI can show which config is live instead of
        # guessing from "enabled" -- an instance can be enabled and still not be the
        # one InkDrop is actually using.
        routed = inkdrop_download_client_routing.slskd_source_instance(db_path)
        routed_id = (routed or {}).get("download_client_instance_id")
        for row in instances:
            if str(row.get("client_type") or "").lower() == "slskd":
                row["is_active_slskd_source"] = row.get("id") == routed_id
    legacy_slskd = inkdrop_state.provider_config(db_path, "slskd")
    if legacy_slskd:
        # A brand-new SLSKD instance draft otherwise starts blank -- if a user
        # finishes and enables it, thinking they're just adding a second
        # connection, it silently takes over with whatever they typed instead of
        # the tuned download_root/incomplete_root/probe settings already live on
        # the provider card. Pre-filling from that card's current config means
        # activating the new surface can't silently change where downloads land.
        legacy_settings = legacy_slskd.get("settings") or {}
        payload["legacy_slskd_defaults"] = {
            "base_url": legacy_slskd.get("base_url") or "",
            "download_path": legacy_settings.get("download_root") or "",
            "settings": {
                key: legacy_settings[key] for key in
                ("wait_seconds", "max_queries", "auto_grab_max", "probe_budget_seconds", "cooldown_hours", "max_active_per_user")
                if key in legacy_settings
            },
        }
    return payload


def get_payload(db_path, instance_id):
    item = config_store.get_instance(db_path, instance_id)
    if not item:
        raise LookupError("download-client instance not found")
    return {"ok": True, "instance": item}


def create(db_path, payload, **kwargs):
    payload = storage_payload(payload)
    if str((payload or {}).get("client_type") or "").lower() not in implemented_registry()["addable"]:
        raise ValueError("download-client type is not addable")
    result = inkdrop_state.create_download_client_instance(db_path, payload, schema_resolver=schema_resolver, **kwargs)
    invalidate(result["id"])
    return {"ok": True, "instance": result}


def update(db_path, instance_id, payload, revision, **kwargs):
    current = config_store.get_instance(db_path, instance_id)
    if not current:
        raise LookupError("download-client instance not found")
    payload = storage_payload({"client_type": current["client_type"], **dict(payload or {})})
    if (payload or {}).get("client_type") and str(payload["client_type"]).lower() not in implemented_registry()["addable"]:
        raise ValueError("download-client type is not addable")
    result = inkdrop_state.update_download_client_instance(db_path, instance_id, payload, expected_revision=revision, schema_resolver=schema_resolver, **kwargs)
    invalidate(instance_id)
    return {"ok": True, "instance": result}


def delete(db_path, instance_id, revision, **kwargs):
    result = inkdrop_state.delete_download_client_instance(db_path, instance_id, expected_revision=revision, **kwargs)
    invalidate(instance_id)
    return {"ok": True, "instance": result}


def _sanitize(value, depth=0):
    if depth > 8:
        return None
    if isinstance(value, dict):
        return {str(key)[:128]: ("<redacted>" if SECRET_PATTERN.search(str(key)) else _sanitize(item, depth + 1)) for key, item in list(value.items())[:128]}
    if isinstance(value, list):
        return [_sanitize(item, depth + 1) for item in value[:128]]
    if isinstance(value, str):
        return value[:1024]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:256]


def _qbit_test(settings, http):
    base = inkdrop_qbittorrent_auth.normalize_base_url(settings["base_url"])
    auth = inkdrop_qbittorrent_auth.authenticate_settings(http, settings, timeout=TEST_TIMEOUT_SECONDS)
    response = http.get(base + "/api/v2/app/version", verify=settings.get("verify_tls", True))
    if inkdrop_qbittorrent_auth.has_api_key(settings) and response.status_code in {401, 403}:
        # The key never touches the login endpoint, so this is the first place
        # a bad or too-old key can show up. Say which, rather than "connectivity".
        raise inkdrop_qbittorrent_auth.QbitAuthError(
            "qBittorrent rejected the API key. Check the key was copied whole, and that this "
            f"server runs {inkdrop_qbittorrent_auth.MIN_API_KEY_VERSION} or newer -- API keys "
            "do not exist in earlier versions, which reject them the same way.",
            kind="api_key",
            status=response.status_code,
        )
    response.raise_for_status()
    return {
        "ok": True,
        "client": "qbittorrent",
        "auth_method": auth["method"],
        "version": str(response.text or "").strip()[:128],
    }


def _sab_test(settings, http):
    response = http.get(settings["base_url"].rstrip("/") + "/api", params={"mode": "version", "output": "json", "apikey": settings.get("api_key", "")}, verify=settings.get("verify_tls", True))
    response.raise_for_status()
    data = response.json() if hasattr(response, "json") else {}
    return {"ok": True, "client": "sabnzbd", "version": str((data or {}).get("version") or "")[:128]}


def _slskd_test(settings, http):
    response = http.get(settings["base_url"].rstrip("/") + "/server", headers={"X-API-Key": settings.get("api_key", "")}, verify=settings.get("verify_tls", True))
    response.raise_for_status()
    return {"ok": True, "client": "slskd", "reachable": True}


TESTERS = {
    "qbittorrent": _qbit_test, "sabnzbd": _sab_test, "slskd": _slskd_test,
    "transmission": lambda settings, http: inkdrop_download_clients.transmission_test_connection(settings, http=http),
    "deluge": lambda settings, http: inkdrop_download_clients.deluge_test_connection(settings, http=http),
    "nzbget": lambda settings, http: inkdrop_download_clients.nzbget_test_connection(settings, http=http),
    "utorrent": lambda settings, http: inkdrop_download_clients.utorrent_test_connection(settings, http=http),
    "rtorrent": lambda settings, http: inkdrop_download_clients.rtorrent_test_connection(settings, http=http),
}


def _run_test(settings, *, http=None):
    client_type, started = str(settings.get("client_type") or "").lower(), time.monotonic()
    tester = TESTERS.get(client_type)
    if tester is None:
        return {"ok": False, "client_type": client_type, "error_type": "unsupported", "reason": "adapter is not implemented"}
    try:
        result = dict(tester(settings, http or SafeSession()) or {})
    except inkdrop_qbittorrent_auth.QbitAuthError as exc:
        # Keep the cause. A banned IP, a rejected Host header, and a wrong
        # password all need different things from the user, and flattening them
        # into "authentication failed" sends people to re-type a good password.
        result = {"ok": False, "error_type": "authentication", "auth_failure": exc.kind, "reason": exc.reason}
    except PermissionError as exc:
        result = {"ok": False, "error_type": "authentication", "reason": str(exc) or "authentication failed"}
    except ValueError as exc:
        result = {"ok": False, "error_type": "configuration", "reason": str(exc)}
    except RuntimeError as exc:
        # Our own adapters raise RuntimeError with an explanation already
        # written for the user; the bare class name below would discard it.
        result = {"ok": False, "error_type": "connectivity", "reason": str(exc)[:512] or "RuntimeError"}
    except Exception as exc:
        result = {"ok": False, "error_type": "connectivity", "reason": type(exc).__name__}
    result.update({"client_type": client_type, "duration_ms": round((time.monotonic() - started) * 1000, 1)})
    return _sanitize(result)


def test_draft(payload, *, http=None):
    raw = storage_payload(payload)
    if str(raw.get("client_type") or "").lower() not in implemented_registry()["testable"]:
        raise ValueError("download-client type is not testable")
    raw.setdefault("name", f"Draft {str(raw.get('client_type') or 'download client')}")
    candidate = config_store._normalize_candidate({**raw, "enabled": True}, schema_resolver=schema_resolver)
    supplied = raw.get("secrets") if isinstance(raw.get("secrets"), dict) else {}
    config_store._validate_ready(candidate, {key: bool(str(value or "").strip()) for key, value in supplied.items()})
    settings = {key: value for key, value in candidate.items() if key != "schema"}
    settings.update(candidate.get("settings") or {})
    settings.update(supplied)
    return {"ok": True, "persisted": False, "result": _run_test(settings, http=http)}


def test_saved(db_path, instance_id, *, secret_root=None, http=None):
    item = config_store.get_instance(db_path, instance_id)
    if not item:
        raise LookupError("download-client instance not found")
    result = ({"ok": True, "skipped": True, "reason": "disabled", "client_type": item.get("client_type")} if not item.get("enabled") else _run_test(config_store.adapter_settings(db_path, instance_id, secret_root=secret_root), http=http))
    status_row = {"instance_id": item["id"], "client_type": item["client_type"], "tested_at": time.time(), "result": result}
    with _LOCK:
        _STATUS_CACHE[item["id"]] = status_row
    return {"ok": True, "status": status_row}


def status(db_path, instance_id, *, force=False, **kwargs):
    with _LOCK:
        cached = dict(_STATUS_CACHE.get(instance_id) or {})
    if not force and cached and time.time() - float(cached.get("tested_at") or 0) <= STATUS_TTL_SECONDS:
        return {"ok": True, "cached": True, "status": cached}
    payload = test_saved(db_path, instance_id, **kwargs)
    payload["cached"] = False
    return payload


def invalidate(instance_id=None):
    with _LOCK:
        _STATUS_CACHE.pop(str(instance_id), None) if instance_id else _STATUS_CACHE.clear()


def _prune_runs(now=None):
    current = time.time() if now is None else float(now)
    for key in [key for key, value in _TEST_RUNS.items() if current - float(value.get("created_at") or 0) > TEST_RUN_TTL_SECONDS]:
        _TEST_RUNS.pop(key, None)
    ordered = sorted(_TEST_RUNS, key=lambda key: float(_TEST_RUNS[key].get("created_at") or 0))
    for key in ordered[:-TEST_RUN_LIMIT]:
        _TEST_RUNS.pop(key, None)


def start_test_all(db_path, *, secret_root=None, http_factory=None):
    run_id = "dctr_" + uuid.uuid4().hex
    with _LOCK:
        _prune_runs()
        _TEST_RUNS[run_id] = {"run_id": run_id, "state": "queued", "created_at": time.time(), "completed_at": None, "results": []}

    def worker():
        instances = config_store.list_instances(db_path).get("instances") or []
        eligible = [row for row in instances if row.get("enabled")]
        results = [{"instance_id": row["id"], "skipped": True, "reason": "disabled"} for row in instances if not row.get("enabled")]
        with _LOCK:
            _TEST_RUNS[run_id]["state"] = "running"
        deadline = time.monotonic() + TEST_ALL_DEADLINE_SECONDS
        with concurrent.futures.ThreadPoolExecutor(max_workers=TEST_ALL_CONCURRENCY) as pool:
            futures = {pool.submit(test_saved, db_path, row["id"], secret_root=secret_root, http=(http_factory() if http_factory else None)): row["id"] for row in eligible}
            for future, instance_id in futures.items():
                try:
                    results.append(future.result(timeout=max(0.01, deadline - time.monotonic())).get("status") or {})
                except Exception as exc:
                    results.append({"instance_id": instance_id, "ok": False, "error_type": type(exc).__name__})
        with _LOCK:
            if run_id in _TEST_RUNS:
                _TEST_RUNS[run_id].update({"state": "completed", "completed_at": time.time(), "results": _sanitize(results)})

    threading.Thread(target=worker, name=f"inkdrop-{run_id}", daemon=True).start()
    return {"ok": True, "accepted": True, "run_id": run_id, "state": "queued"}


def test_run(run_id):
    with _LOCK:
        _prune_runs()
        run = _TEST_RUNS.get(str(run_id))
        return {"ok": True, "run": _sanitize(dict(run))} if run else None
