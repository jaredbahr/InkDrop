"""Conservative CLI wrapper for settings-backed InkDrop source batches."""

from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))


import argparse
import base64
import copy
import hashlib
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlparse

from core import inkdrop_source_worker_batch as batch
from core import inkdrop_source_worker_http as source_http
from core import inkdrop_sources
from core import inkdrop_runtime_config


CONTRACT_VERSION = 1


class ProviderStartDeadlineMissed(RuntimeError):
    pass

DEFAULT_SECRET_ENV_PREFIX = "INKDROP_SOURCE_SECRET_"
DEFAULT_PROWLARR_CONFIG = Path(os.environ.get("INKDROP_PROWLARR_CONFIG") or inkdrop_runtime_config.config_dir() / "prowlarr" / "config.xml")

URL_PATTERN = re.compile(r"(https?://[^\s<>'\"]+|magnet:\?[^\s<>'\"]+)")

SECRETISH_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
)

URLISH_KEY_PARTS = (
    "download_url",
    "final_url",
    "href",
    "magnet_url",
    "source_url",
    "url",
)


def _list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def split_csv_values(values):
    out = []
    for value in _list(values):
        for part in str(value or "").split(","):
            text = part.strip()
            if text:
                out.append(text)
    return out


def _int(value, default=None):
    try:
        if value in (None, ""):
            return default
        return int(value)
    except Exception:
        return default


def _float(value, default=None):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def _json_load(path, default):
    if not path:
        return default
    with Path(path).expanduser().open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    return data if data not in (None, "") else default


def _parse_name_value(raw, *, label):
    text = str(raw or "")
    if "=" not in text:
        raise ValueError(f"{label} must use NAME=VALUE syntax")
    name, value = text.split("=", 1)
    name = name.strip()
    if not name:
        raise ValueError(f"{label} is missing a name")
    return name, value


def _secret_field_key(value):
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def provider_secret_values_from_db(db_path):
    values = {}
    if not db_path:
        return values
    try:
        from core import inkdrop_state

        path = Path(db_path)
        if not path.exists():
            return values
        with inkdrop_state.connect_read(path) as con:
            if not inkdrop_state.table_exists(con, "provider_configs"):
                return values
            rows = con.execute("select id, settings_json from provider_configs").fetchall()
    except Exception:
        return values
    for row in rows:
        provider_id = str(row["id"] if hasattr(row, "keys") else row[0] or "").strip()
        provider_key = inkdrop_sources.provider_key(provider_id)
        if not provider_key:
            continue
        try:
            settings = json.loads((row["settings_json"] if hasattr(row, "keys") else row[1]) or "{}")
        except ValueError:
            settings = {}
        if not isinstance(settings, dict):
            continue
        secret_fields = settings.get("secret_fields") if isinstance(settings.get("secret_fields"), list) else []
        for field in secret_fields:
            field_name = str(field or "").strip()
            field_key = _secret_field_key(field_name)
            if not field_key:
                continue
            value = settings.get(field_name)
            if value in (None, ""):
                continue
            names = {
                f"{provider_key}_{field_key}",
                f"{_secret_field_key(provider_id)}_{field_key}",
            }
            if field_key == "api_key":
                names.add(f"{provider_key}_apikey")
            for name in names:
                if name:
                    values[name] = str(value)
    return values


def _prowlarr_api_key_from_config(*, environ=None):
    """Return Prowlarr's existing local API key without persisting it elsewhere."""

    environ = environ if isinstance(environ, dict) else os.environ
    config_path = Path(environ.get("INKDROP_PROWLARR_CONFIG") or DEFAULT_PROWLARR_CONFIG)
    try:
        config_key = ET.parse(config_path).getroot().findtext("ApiKey")
    except Exception:
        return ""
    return str(config_key or "").strip()


def provider_secret_values(db_path, *, environ=None, env_secret_prefix=DEFAULT_SECRET_ENV_PREFIX):
    """Resolve provider secrets through the same bounded runtime contract.

    Manual Search and the source-worker CLI both use this resolver. Values are
    kept in memory only; callers must continue to pass them to the redacting
    source HTTP client rather than serializing them into plans or diagnostics.
    """

    environ = environ if isinstance(environ, dict) else os.environ
    values = provider_secret_values_from_db(db_path)
    prefix = str(env_secret_prefix or "")
    if prefix:
        for key, value in environ.items():
            if not str(key).startswith(prefix):
                continue
            name = str(key)[len(prefix) :].strip().lower()
            if name and value not in (None, ""):
                values[name] = str(value)
    if not values.get("prowlarr_api_key"):
        config_key = _prowlarr_api_key_from_config(environ=environ)
        if config_key:
            values["prowlarr_api_key"] = config_key
    return values


def secret_values_from_args(args, *, environ=None):
    environ = environ if isinstance(environ, dict) else os.environ
    prefix = str(getattr(args, "env_secret_prefix", None) or DEFAULT_SECRET_ENV_PREFIX)
    values = provider_secret_values(
        getattr(args, "db_path", None),
        environ=environ,
        env_secret_prefix=prefix,
    )
    for item in split_csv_values(getattr(args, "secret_env", None)):
        name, env_key = _parse_name_value(item, label="--secret-env")
        value = environ.get(env_key)
        if value not in (None, ""):
            values[name] = str(value)
            values[name.lower()] = str(value)
    for item in split_csv_values(getattr(args, "secret", None)):
        name, value = _parse_name_value(item, label="--secret")
        values[name] = value
        values[name.lower()] = value
    return values


def build_parser():
    parser = argparse.ArgumentParser(
        description="Plan or run settings-backed InkDrop source-worker batches.",
    )
    parser.add_argument("db_path", help="InkDrop state database path.")
    parser.add_argument("--queue-id", action="append", default=[], help="Queue id to include. Repeat or comma-separate.")
    parser.add_argument("--provider-id", action="append", default=[], help="Provider/source id to include. Repeat or comma-separate.")
    parser.add_argument("--state", action="append", default=[], help="Queue state to include. Repeat or comma-separate.")
    parser.add_argument("--queue-limit", type=int, default=50)
    parser.add_argument("--job-limit", type=int, default=20)
    parser.add_argument("--attempt-cooldown-seconds", type=float, default=0)
    parser.add_argument("--provider-timeout-window-seconds", type=float, default=0)
    parser.add_argument("--provider-timeout-threshold", type=int, default=0)
    parser.add_argument("--provider-timeout-cooldown-seconds", type=float, default=0)
    parser.add_argument("--provider-fetch-failure-window-seconds", type=float, default=0)
    parser.add_argument("--provider-fetch-failure-threshold", type=int, default=0)
    parser.add_argument("--provider-fetch-failure-cooldown-seconds", type=float, default=0)
    parser.add_argument(
        "--comic-pack-child-lane-limit",
        type=int,
        help="Max trusted comic Prowlarr child lanes to try per queue row in one batch pass.",
    )
    parser.add_argument("--run-limit", type=int)
    parser.add_argument("--eligible-limit", type=int)
    parser.add_argument(
        "--max-run-seconds",
        type=float,
        help="Stop selecting new source-worker queue rows when the run budget is too low.",
    )
    parser.add_argument("--include-waiting", dest="due_only", action="store_false", help="Include rows whose retry_after is still in the future.")
    parser.set_defaults(due_only=True)
    parser.add_argument("--include-blocked", action="store_true", help="Include blocked/planned jobs in diagnostics.")
    parser.add_argument("--exclude-operator", dest="include_operator", action="store_false", help="Hide operator-required jobs.")
    parser.set_defaults(include_operator=True)

    parser.add_argument("--execute", action="store_true", help="Run eligible provider jobs. Without this, the command is plan-only.")
    parser.add_argument("--write", action="store_true", help="Persist attempts/tasks. Requires --execute.")
    parser.add_argument("--stage-direct", action="store_true", help="Stage queued source file tasks after recording.")
    parser.add_argument("--stage-managed-folders", action="store_true", help="Scan explicitly enabled managed-folder sources and stage import-ready files.")
    parser.add_argument("--handoff-download-clients", action="store_true", help="Send queued qBittorrent/SABnzbd tasks to the configured downloader after recording.")
    parser.add_argument("--staging-root", help="Source staging root. Required for --stage-direct --write.")

    parser.add_argument("--allow-network", action="store_true", help="Build a source HTTP client for adapter requests.")
    parser.add_argument("--allow-direct-network", action="store_true", help="Build a direct-download HTTP client for staging.")
    parser.add_argument("--allowed-host", action="append", default=[], help="Allowed source HTTP host. Repeat or comma-separate.")
    parser.add_argument("--direct-allowed-host", action="append", default=[], help="Allowed direct-download host. Defaults to --allowed-host.")
    parser.add_argument("--timeout-seconds", type=float, default=15.0)
    parser.add_argument("--max-bytes", type=int, default=5 * 1024 * 1024)
    parser.add_argument("--direct-timeout-seconds", type=float)
    parser.add_argument(
        "--direct-max-bytes",
        type=int,
        help="Cap for staged direct downloads in bytes. Defaults to the 2 GiB download policy; --max-bytes only bounds API/feed responses.",
    )
    parser.add_argument("--secret", action="append", default=[], help="Secret resolver value as NAME=VALUE. Values are never printed.")
    parser.add_argument("--secret-env", action="append", default=[], help="Secret resolver value as NAME=ENV_VAR.")
    parser.add_argument("--env-secret-prefix", default=DEFAULT_SECRET_ENV_PREFIX)

    parser.add_argument("--candidate-headers-json", help="JSON file of candidate headers by provider.")
    parser.add_argument("--operator-payloads-json", help="JSON file of operator payloads by provider.")
    parser.add_argument("--source-memory-db", help="Source Memory DB path. Defaults to db_path.")
    parser.add_argument("--source-memory-cooldown-seconds", type=float)
    parser.add_argument("--record-lock-retry-attempts", type=int)
    parser.add_argument("--record-lock-retry-initial-delay", type=float)

    parser.add_argument("--full-output", action="store_true", help="Print redacted full batch output instead of a compact summary.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON.")
    parser.add_argument("--now", type=float, help="Override current timestamp. Intended for smoke tests.")
    return parser


def validate_args(args):
    errors = []
    allowed_hosts = split_csv_values(getattr(args, "allowed_host", None))
    direct_allowed_hosts = split_csv_values(getattr(args, "direct_allowed_host", None)) or allowed_hosts
    if args.write and not args.execute:
        errors.append("--write requires --execute")
    if args.allow_network and not allowed_hosts:
        errors.append("--allow-network requires at least one --allowed-host")
    if args.allow_direct_network and not direct_allowed_hosts:
        errors.append("--allow-direct-network requires --direct-allowed-host or --allowed-host")
    if args.stage_direct and args.write and not args.staging_root:
        errors.append("--stage-direct --write requires --staging-root")
    if args.stage_direct and args.write and not args.allow_direct_network:
        errors.append("--stage-direct --write requires --allow-direct-network")
    return errors


# run_source_worker_batch() already de-dupes identical HTTP requests within
# one call via an in-memory cache (inkdrop_source_worker_batch._cached_
# source_http_get), but that cache is created fresh per call -- and this CLI
# gets invoked once per SERIES (core/inkdrop_series_autopilot.py's
# run_source_worker_rss()), one fresh subprocess each time. A monitored
# library checked series-by-series refetches the one shared RSS feed URL
# once per series in quick succession; GetComics answers that with a 429,
# which (via provider_fetch_failure_circuit_breakers in
# inkdrop_source_worker_scheduler.py, threshold=2 failures/30min) opens a
# 30-minute "provider_wait" cooldown for every series' RSS queue rows, not
# just the one that got rate-limited. Same shape of bug as the one already
# fixed in inkdrop_rss_discovery.py's fetch_feed() -- same fix, scoped to
# this separate execution path: persist a short-lived, on-disk copy of the
# feed response so the next series' subprocess reuses it instead of
# refetching. Scoped narrowly to the RSS feed host only (RSS_FEED_HOSTS
# below) -- every other provider's request through this client is
# untouched, byte-for-byte the same as before.
RSS_FEED_HOSTS = ("getcomics.org", "www.getcomics.org")
RSS_FEED_CACHE_FILE = inkdrop_runtime_config.state_dir() / "rss-source-worker-feed-cache.json"
RSS_FEED_CACHE_TTL_SECONDS = 300


def _rss_feed_cache_key(request):
    request = request if isinstance(request, dict) else {}
    method = str(request.get("method") or "GET").strip().upper()
    if method not in {"GET", "HEAD"}:
        return None
    url = str(request.get("url") or "").strip()
    if not url:
        return None
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return None
    if host.lower() not in RSS_FEED_HOSTS:
        return None
    return json.dumps(
        {"method": method, "url": url, "params": request.get("params") or {}},
        sort_keys=True,
        default=str,
    )


def _json_safe(value):
    """Recursively swap bytes for a JSON-safe marker (source_http_get's
    response body is bytes whenever the content-type isn't recognized as
    text/json -- see inkdrop_source_worker_http._decode_payload's "body"
    fallback -- and plain json.dumps() raises TypeError on that, which a
    bare except then swallows silently: the cache write appears to succeed
    but never actually lands on disk, every call refetches, and the whole
    point of this cache quietly does nothing.
    """
    if isinstance(value, (bytes, bytearray)):
        return {"__b64__": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _json_unsafe(value):
    if isinstance(value, dict):
        if set(value.keys()) == {"__b64__"}:
            return base64.b64decode(value["__b64__"])
        return {key: _json_unsafe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_unsafe(item) for item in value]
    return value


def _load_rss_feed_cache():
    try:
        return _json_unsafe(json.loads(RSS_FEED_CACHE_FILE.read_text(encoding="utf-8")))
    except Exception:
        return {}


def _save_rss_feed_cache(cache):
    try:
        RSS_FEED_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = RSS_FEED_CACHE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_json_safe(cache)), encoding="utf-8")
        os.replace(tmp, RSS_FEED_CACHE_FILE)
    except Exception:
        pass


def _rss_feed_caching_client(client):
    def _wrapped(request):
        key = _rss_feed_cache_key(request)
        if key is None:
            return client(request)
        cache = _load_rss_feed_cache()
        entry = cache.get(key)
        now = time.time()
        if isinstance(entry, dict) and (now - float(entry.get("cached_at") or 0)) < RSS_FEED_CACHE_TTL_SECONDS:
            return copy.deepcopy(entry["response"])
        response = client(request)
        cache[key] = {"cached_at": now, "response": response}
        _save_rss_feed_cache(cache)
        return response

    return _wrapped


def _build_source_http(args, *, secret_values, override=None):
    if override is not None:
        return override
    if not args.allow_network:
        return None
    client = source_http.make_source_http_get(
        secret_values=secret_values,
        allowed_hosts=split_csv_values(args.allowed_host),
        allow_request_hosts=True,
        timeout_seconds=args.timeout_seconds,
        max_bytes=args.max_bytes,
    )
    return _rss_feed_caching_client(client)


def _direct_transport_authority_hosts(db_path):
    """Transport hosts every enabled provider is allowed to download from.

    The pending direct-stage sweep processes EVERY provider's queued
    direct/page-pack tasks, not just the invoking lane's
    (inkdrop_source_worker_batch.run_source_worker_batch selects queue rows by
    download_client alone). A scheduled MangaDex lane whose direct cap named
    only uploads.mangadex.org therefore kept rejecting queued
    GetComics/Pixeldrain tasks with disallowed_host before any network attempt.

    The lane cap keeps covering the lane's own infrastructure; per-task
    confinement is unchanged because each task still sends its own
    transport_allowed_hosts as request-level hosts, which the client
    intersects against this cap (a request can narrow it, never widen it).
    Unioning the registry's per-provider transport allowlists here adds no
    host that is not already configured provider policy.
    """
    hosts = []
    seen = set()

    def _add(value):
        text = str(value or "").strip().lower()
        if text and text not in seen:
            seen.add(text)
            hosts.append(text)

    try:
        from core import inkdrop_source_registry
        from core import inkdrop_source_worker_adapters as source_adapters

        rows = inkdrop_source_registry.registry_from_db(db_path, include_disabled=False)
    except Exception:
        return []
    for row in rows or []:
        row = row if isinstance(row, dict) else {}
        try:
            for host in source_adapters._direct_transport_allowed_hosts(row) or []:
                _add(host)
        except Exception:
            continue
        provider_id = str(row.get("provider_id") or "").strip().lower()
        policy = row.get("policy") if isinstance(row.get("policy"), dict) else {}
        if provider_id == "mangadex":
            # MangaDex@Home page servers use per-node hostnames handed out at
            # runtime; the page-pack validator only ever admits this family
            # (inkdrop_source_providers._mangadex_page_allowed_hosts).
            _add("uploads.mangadex.org")
            _add("*.mangadex.network")
        if provider_id == "suwayomi":
            endpoint = str(policy.get("suwayomi_page_base_url") or row.get("base_url") or "").strip()
            if endpoint:
                _add(urlparse(endpoint).hostname)
    return hosts


def _build_direct_http(args, *, secret_values, override=None):
    if override is not None:
        return override
    if not args.allow_direct_network:
        return None
    allowed_hosts = split_csv_values(args.direct_allowed_host) or split_csv_values(args.allowed_host)
    lane_hosts = set(allowed_hosts)
    allowed_hosts = allowed_hosts + [
        host for host in _direct_transport_authority_hosts(args.db_path) if host not in lane_hosts
    ]
    return source_http.make_source_http_get(
        secret_values=secret_values,
        allowed_hosts=allowed_hosts,
        allow_request_hosts=True,
        timeout_seconds=args.direct_timeout_seconds or args.timeout_seconds,
        max_bytes=args.direct_max_bytes or args.max_bytes,
        # Direct payload transfers stream to disk and are bounded by the
        # 2 GiB download policy (--direct-max-bytes narrows it); the buffered
        # max_bytes above only bounds non-streaming responses on this client.
        stream_max_bytes=args.direct_max_bytes,
        allow_stream_body=True,
    )


def _observed_source_http_get(http_get, provider_observer=None, call_tracker=None):
    if not callable(http_get) or not provider_observer:
        return http_get

    def observed(request):
        started_monotonic = time.monotonic()
        call_id = f"source-http:{time.monotonic_ns()}"
        allowed = provider_observer(
            {
                "phase": "permission",
                "call_id": call_id,
                "started_monotonic": started_monotonic,
                "healthy": None,
            }
        )
        if allowed is False:
            raise ProviderStartDeadlineMissed("provider_start_deadline_missed")
        provider_observer(
            {
                "phase": "start",
                "call_id": call_id,
                "started_monotonic": started_monotonic,
                "healthy": None,
                "failed": False,
                "error": "",
            }
        )
        if isinstance(call_tracker, list):
            call_tracker.append({"call_id": call_id, "started_monotonic": started_monotonic})
        try:
            response = http_get(request)
        except Exception as exc:
            provider_observer(
                {
                    "phase": "finish",
                    "call_id": call_id,
                    "started_monotonic": started_monotonic,
                    "healthy": False,
                    "failed": True,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            raise
        return response

    return observed


def _source_worker_run_semantic_health(run):
    if not isinstance(run, dict) or run.get("ok") is not True:
        return False
    result = run.get("result") if isinstance(run.get("result"), dict) else {}
    if result.get("ok") is False or result.get("errors"):
        return False
    job_results = [row for row in result.get("job_results") or [] if isinstance(row, dict)]
    if not job_results:
        return False
    unhealthy_statuses = {
        "", "pending", "provider_wait", "provider_unavailable", "error", "failed",
        "operator_required", "configuration_required", "skipped", "cooldown",
    }
    for job_result in job_results:
        fetch = job_result.get("fetch") if isinstance(job_result.get("fetch"), dict) else {}
        status = str(job_result.get("result_status") or "").strip().lower()
        if fetch.get("ok") is not True or status in unhealthy_statuses:
            return False
    return True


def _source_worker_call_healths(result, call_count):
    if not isinstance(result, dict) or result.get("ok") is not True or call_count <= 0:
        return [False] * max(0, int(call_count or 0))
    runs = [row for row in result.get("runs") or [] if isinstance(row, dict)]
    run_health = [_source_worker_run_semantic_health(run) for run in runs]
    if not run_health:
        return [False] * call_count
    if len(run_health) == 1:
        return [run_health[0]] * call_count
    # Calls are not explicitly owned by runs; equal list lengths can still be
    # coincidental when one run makes several requests and another uses cache.
    # Fail closed across mixed runs so health never crosses provider calls.
    return [all(run_health)] * call_count


def _finish_source_http_observations(provider_observer, call_tracker, result=None, error=None):
    if not provider_observer:
        return
    calls = list(call_tracker or [])
    healths = [False] * len(calls) if error else _source_worker_call_healths(result, len(calls))
    for call, healthy in zip(calls, healths):
        provider_observer(
            {
                "phase": "finish",
                "call_id": call.get("call_id"),
                "started_monotonic": call.get("started_monotonic"),
                "healthy": healthy,
                "failed": not healthy,
                "error": str(error or ("" if healthy else "provider response could not be validated")),
            }
        )


def _hash_text(value):
    text = str(value or "")
    if not text:
        return ""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _redact_urls_in_text(value):
    text = str(value or "")
    return URL_PATTERN.sub(lambda match: _hash_text(match.group(0)), text)


def _redact_jsonish(value, *, key_name=""):
    lower_key = str(key_name or "").strip().lower()
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            out[str(key)] = _redact_jsonish(item, key_name=str(key))
        return out
    if isinstance(value, list):
        return [_redact_jsonish(item, key_name=key_name) for item in value]
    if isinstance(value, tuple):
        return [_redact_jsonish(item, key_name=key_name) for item in value]
    if isinstance(value, str):
        if any(part in lower_key for part in SECRETISH_KEY_PARTS):
            return "<redacted>"
        if any(part in lower_key for part in URLISH_KEY_PARTS) and value.startswith(("http://", "https://", "magnet:")):
            return _hash_text(value)
        return _redact_urls_in_text(value)
    return value


def source_memory_db_path(args):
    explicit = str(getattr(args, "source_memory_db", "") or "").strip()
    if explicit:
        return explicit
    return str(getattr(args, "db_path", "") or "").strip()


def compact_output(result, args, *, secret_count=0):
    schedule = result.get("schedule") if isinstance(result.get("schedule"), dict) else {}
    runs = []
    for run in result.get("runs") or []:
        run_result = run.get("result") if isinstance(run.get("result"), dict) else {}
        direct_stage = run_result.get("direct_stage") if isinstance(run_result.get("direct_stage"), dict) else {}
        download_client_handoff = (
            run_result.get("download_client_handoff")
            if isinstance(run_result.get("download_client_handoff"), dict)
            else {}
        )
        runs.append(
            {
                "queue_id": run.get("queue_id"),
                "provider_ids": list(run.get("provider_ids") or []),
                "ok": bool(run.get("ok")),
                "reason": run.get("reason") or "",
                "job_result_summary": run_result.get("job_result_summary") or {},
                "recording": run_result.get("recording") or {},
                "direct_stage": {
                    "ok": direct_stage.get("ok"),
                    "stageable_download_clients": direct_stage.get("stageable_download_clients") or [],
                    "tasks_available": direct_stage.get("tasks_available"),
                    "tasks_staged": direct_stage.get("tasks_staged"),
                    "tasks_failed": direct_stage.get("tasks_failed"),
                    "reason": direct_stage.get("reason") or "",
                }
                if direct_stage
                else {},
                "download_client_handoff": {
                    "ok": download_client_handoff.get("ok"),
                    "handoff_download_clients": download_client_handoff.get("handoff_download_clients") or [],
                    "tasks_available": download_client_handoff.get("tasks_available"),
                    "tasks_handed_off": download_client_handoff.get("tasks_handed_off"),
                    "tasks_failed": download_client_handoff.get("tasks_failed"),
                    "reason": download_client_handoff.get("reason") or "",
                }
                if download_client_handoff
                else {},
            }
        )
    pending_direct_stage_raw = result.get("pending_direct_stage") if isinstance(result.get("pending_direct_stage"), dict) else {}
    pending_direct_stage = {
        "ok": pending_direct_stage_raw.get("ok"),
        "queue_ids": list(pending_direct_stage_raw.get("queue_ids") or []),
        "budget_skipped_queue_ids": list(pending_direct_stage_raw.get("budget_skipped_queue_ids") or []),
        "tasks_available": pending_direct_stage_raw.get("tasks_available"),
        "tasks_staged": pending_direct_stage_raw.get("tasks_staged"),
        "tasks_failed": pending_direct_stage_raw.get("tasks_failed"),
        "runs": [
            {
                "queue_id": run.get("queue_id"),
                "ok": bool(run.get("ok")),
                "reason": run.get("reason") or "",
                "direct_stage": {
                    "ok": ((run.get("result") or {}).get("ok") if isinstance(run.get("result"), dict) else None),
                    "tasks_available": ((run.get("result") or {}).get("tasks_available") if isinstance(run.get("result"), dict) else None),
                    "tasks_staged": ((run.get("result") or {}).get("tasks_staged") if isinstance(run.get("result"), dict) else None),
                    "tasks_failed": ((run.get("result") or {}).get("tasks_failed") if isinstance(run.get("result"), dict) else None),
                    "reason": ((run.get("result") or {}).get("reason") or "" if isinstance(run.get("result"), dict) else ""),
                },
            }
            for run in pending_direct_stage_raw.get("runs") or []
        ],
    }
    managed_folder_stage_raw = result.get("managed_folder_stage") if isinstance(result.get("managed_folder_stage"), dict) else {}
    managed_folder_stage = {
        "ok": managed_folder_stage_raw.get("ok"),
        "skipped": managed_folder_stage_raw.get("skipped"),
        "reason": managed_folder_stage_raw.get("reason") or "",
        "provider_id": managed_folder_stage_raw.get("provider_id") or "",
        "matched_count": managed_folder_stage_raw.get("matched_count"),
        "would_promote_import_ready_count": managed_folder_stage_raw.get("would_promote_import_ready_count"),
        "promoted_import_ready_count": managed_folder_stage_raw.get("promoted_import_ready_count"),
        "already_staged_import_ready_count": managed_folder_stage_raw.get("already_staged_import_ready_count"),
        "promotion_blocked_count": managed_folder_stage_raw.get("promotion_blocked_count"),
        "blocked_count": managed_folder_stage_raw.get("blocked_count"),
    }
    plans = []
    for plan in schedule.get("plans") or []:
        plans.append(
            {
                "queue_id": plan.get("queue_id"),
                "series": plan.get("series"),
                "issue_number": plan.get("issue_number"),
                "status": plan.get("status"),
                "blocker": plan.get("blocker"),
                "selected_provider_ids": list(plan.get("selected_provider_ids") or []),
                "provider_attempt_counts": plan.get("provider_attempt_counts") or {},
                "provider_attempt_plan": list(plan.get("provider_attempt_plan") or []),
                "all_job_status_counts": plan.get("all_job_status_counts") or {},
                "job_status_counts": plan.get("job_status_counts") or {},
                "source_worker_cooldown_count": int(plan.get("source_worker_cooldown_count") or 0),
                "provider_timeout_circuit_count": int(plan.get("provider_timeout_circuit_count") or 0),
                "provider_fetch_failure_circuit_count": int(plan.get("provider_fetch_failure_circuit_count") or 0),
                "source_worker_cooldowns": list(plan.get("source_worker_cooldowns") or []),
                "next_action": plan.get("next_action"),
            }
        )
    selected_plans = []
    for plan in result.get("selected_plans") or []:
        selected_plans.append(
            {
                "queue_id": plan.get("queue_id"),
                "series": plan.get("series"),
                "issue_number": plan.get("issue_number"),
                "selected_provider_ids": list(plan.get("selected_provider_ids") or []),
                "source_worker_slice": plan.get("source_worker_slice") or {},
                "source_worker_comic_pack_backlog_priority": int(
                    plan.get("source_worker_comic_pack_backlog_priority") or 0
                ),
                "runtime_estimate_seconds": batch._plan_runtime_estimate(
                    plan,
                    source_http_timeout_seconds=args.timeout_seconds,
                ),
            }
        )
    return {
        "source_worker_cli_contract_version": CONTRACT_VERSION,
        "ok": bool(result.get("ok")),
        "db_path": str(Path(args.db_path)),
        "mode": {
            "execute_jobs": bool(args.execute),
            "write_enabled": bool(args.write),
            "dry_run": not bool(args.write),
            "source_network_enabled": bool(args.allow_network),
            "direct_network_enabled": bool(args.allow_direct_network),
            "stage_direct": bool(args.stage_direct),
            "stage_managed_folders": bool(args.stage_managed_folders),
            "handoff_download_clients": bool(args.handoff_download_clients),
            "queue_limit": _int(result.get("queue_limit"), _int(args.queue_limit, 50)) or 50,
            "queue_scan_limit": _int(result.get("queue_scan_limit"), _int(args.queue_limit, 50)) or 50,
            "max_run_seconds": _float(args.max_run_seconds, 0.0) or 0,
            "source_http_timeout_seconds": _float(args.timeout_seconds, 0.0) or 0,
                "provider_timeout_window_seconds": _float(args.provider_timeout_window_seconds, 0.0) or 0,
                "provider_timeout_threshold": _int(args.provider_timeout_threshold, 0) or 0,
                "provider_timeout_cooldown_seconds": _float(args.provider_timeout_cooldown_seconds, 0.0) or 0,
                "provider_fetch_failure_window_seconds": _float(args.provider_fetch_failure_window_seconds, 0.0) or 0,
                "provider_fetch_failure_threshold": _int(args.provider_fetch_failure_threshold, 0) or 0,
                "provider_fetch_failure_cooldown_seconds": _float(args.provider_fetch_failure_cooldown_seconds, 0.0) or 0,
                "record_lock_retry_attempts": _int(args.record_lock_retry_attempts, 0) or 0,
            "record_lock_retry_initial_delay": _float(args.record_lock_retry_initial_delay, 0.0) or 0,
            "source_memory_enabled": bool(source_memory_db_path(args)),
            "source_memory_defaulted": not bool(str(getattr(args, "source_memory_db", "") or "").strip()),
        },
        "safety": {
            "mutates_database": bool(result.get("mutates_database")),
            "mutates_filesystem": bool(result.get("mutates_filesystem")),
            "mutates_download_client": bool(result.get("mutates_download_client")),
            "secret_values_loaded": int(secret_count),
            "allowed_hosts": split_csv_values(args.allowed_host),
            "direct_allowed_hosts": split_csv_values(args.direct_allowed_host) or split_csv_values(args.allowed_host),
        },
        "selected_queue_ids": list(result.get("selected_queue_ids") or []),
        "selected_plans": selected_plans,
        "budget_skipped_queue_ids": list(result.get("budget_skipped_queue_ids") or []),
        "provider_pass_failure_skipped_queue_ids": list(result.get("provider_pass_failure_skipped_queue_ids") or []),
        "provider_pass_failure_counts": result.get("provider_pass_failure_counts") or {},
        "provider_pass_failure_skips": [
            {
                "queue_id": row.get("queue_id"),
                "series": row.get("series"),
                "issue_number": row.get("issue_number"),
                "provider_pass_failure_budget": row.get("provider_pass_failure_budget") or {},
            }
            for row in result.get("provider_pass_failure_skips") or []
            if isinstance(row, dict)
        ],
        "runtime_budget": result.get("runtime_budget") or {},
        "source_http_cache": result.get("source_http_cache") or {},
        "summary": result.get("summary") or {},
        "pending_direct_stage": pending_direct_stage if pending_direct_stage_raw else {},
        "managed_folder_stage": managed_folder_stage if managed_folder_stage_raw else {},
        "schedule": {
            "queue_count": schedule.get("queue_count"),
            "queue_limit": result.get("queue_limit"),
            "queue_scan_limit": result.get("queue_scan_limit"),
            "summary": schedule.get("summary") or {},
            "plans": plans,
        },
        "runs": runs,
    }


def run_source_worker_cli(
    argv=None,
    *,
    source_http_get=None,
    direct_http_get=None,
    environ=None,
    provider_observer=None,
):
    parser = build_parser()
    args = parser.parse_args(argv)
    errors = validate_args(args)
    if errors:
        raise ValueError("; ".join(errors))
    secret_values = secret_values_from_args(args, environ=environ)
    provider_calls = []
    source_client = _observed_source_http_get(
        _build_source_http(args, secret_values=secret_values, override=source_http_get),
        provider_observer,
        provider_calls,
    )
    direct_client = _build_direct_http(args, secret_values=secret_values, override=direct_http_get)
    try:
        result = batch.run_source_worker_batch(
            args.db_path,
            source_http_get=source_client,
            direct_http_get=direct_client,
            candidate_headers_by_provider=_json_load(args.candidate_headers_json, {}),
            operator_payloads=_json_load(args.operator_payloads_json, {}),
            source_memory_db_path=source_memory_db_path(args),
            source_memory_cooldown_seconds=args.source_memory_cooldown_seconds,
            staging_root=args.staging_root,
            queue_ids=split_csv_values(args.queue_id),
            states=split_csv_values(args.state),
            due_only=args.due_only,
            include_operator=args.include_operator,
            include_blocked=args.include_blocked,
            provider_ids=split_csv_values(args.provider_id),
            queue_limit=args.queue_limit,
            job_limit=args.job_limit,
            attempt_cooldown_seconds=args.attempt_cooldown_seconds,
            provider_timeout_window_seconds=args.provider_timeout_window_seconds,
            provider_timeout_threshold=args.provider_timeout_threshold,
            provider_timeout_cooldown_seconds=args.provider_timeout_cooldown_seconds,
            provider_fetch_failure_window_seconds=args.provider_fetch_failure_window_seconds,
            provider_fetch_failure_threshold=args.provider_fetch_failure_threshold,
            provider_fetch_failure_cooldown_seconds=args.provider_fetch_failure_cooldown_seconds,
            comic_pack_child_lane_limit=args.comic_pack_child_lane_limit,
            run_limit=args.run_limit,
            eligible_limit=args.eligible_limit,
            execute_jobs=args.execute,
            dry_run=not args.write,
            stage_direct=args.stage_direct,
            stage_managed_folders=args.stage_managed_folders,
            handoff_download_clients=args.handoff_download_clients,
            record_lock_retry_attempts=args.record_lock_retry_attempts,
            record_lock_retry_initial_delay=args.record_lock_retry_initial_delay,
            max_run_seconds=args.max_run_seconds,
            source_http_timeout_seconds=args.timeout_seconds,
            now=args.now,
        )
    except Exception as exc:
        _finish_source_http_observations(provider_observer, provider_calls, error=exc)
        raise
    _finish_source_http_observations(provider_observer, provider_calls, result=result)
    if args.full_output:
        payload = _redact_jsonish(
            {
                "source_worker_cli_contract_version": CONTRACT_VERSION,
                "ok": bool(result.get("ok")),
                "mode": compact_output(result, args, secret_count=len(secret_values))["mode"],
                "safety": compact_output(result, args, secret_count=len(secret_values))["safety"],
                "batch": result,
            }
        )
    else:
        payload = compact_output(result, args, secret_count=len(secret_values))
    return payload


def main(argv=None):
    try:
        payload = run_source_worker_cli(argv)
    except ValueError as exc:
        print(json.dumps({"ok": False, "reason": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    indent = 2 if "--pretty" in (argv if argv is not None else sys.argv[1:]) else None
    print(json.dumps(payload, sort_keys=True, indent=indent, ensure_ascii=True))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
