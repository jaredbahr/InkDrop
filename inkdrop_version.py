#!/usr/bin/env python3
"""Canonical, build-injected InkDrop product and version metadata."""

from __future__ import annotations

import json
import hashlib
import hmac
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


PRODUCT_NAME = "InkDrop"
DEFAULT_VERSION = "dev"
DEFAULT_COMMIT_SHA = "unknown"
DEFAULT_BUILD_DATE = "unknown"
DEFAULT_RELEASE_CHANNEL = "dev"
CANDIDATE_MANIFEST_SCHEMA = "inkdrop.qa_candidate.v1"
CANDIDATE_MANIFEST_SCHEMA_VERSION = 1
UPDATE_MANIFEST_SCHEMA_VERSION = 1
UPDATE_STATUS_SCHEMA = "inkdrop.update_status.v1"
UPDATE_STATUS_SCHEMA_VERSION = 1
UPDATE_IMAGE_REPOSITORY = "ghcr.io/jaredbahr/inkdrop"
UPDATE_IMAGE_REPOSITORIES = {
    "qa": "ghcr.io/jaredbahr/inkdrop-qa",
    "alpha": "ghcr.io/jaredbahr/inkdrop-qa",
    "beta": "ghcr.io/jaredbahr/inkdrop-beta",
    "stable": UPDATE_IMAGE_REPOSITORY,
}
UPDATE_MANIFEST_MAX_BYTES = 64 * 1024
UPDATE_CACHE_SECONDS = 3600
UPDATE_MANIFEST_MAX_AGE_SECONDS = 31 * 24 * 60 * 60
UPDATE_MANIFEST_FUTURE_SECONDS = 5 * 60
UPDATE_CONNECT_TIMEOUT_SECONDS = 2.0
UPDATE_TOTAL_TIMEOUT_SECONDS = 5.0
UPDATE_CACHE_MAX_CONTEXTS = 8
DEFAULT_UPDATE_MANIFEST_URL = ""
UPDATE_STATES = {
    "up_to_date", "update_available", "newer_prerelease_available", "channel_mismatch",
    "unsupported_install", "update_check_unavailable", "invalid_manifest",
    "incompatible_update", "development_build",
}
UPDATE_MANIFEST_REQUIRED_KEYS = {
    "schema_version", "product", "version", "channel", "prerelease", "validated",
    "published_at", "commit_sha", "image_repository", "image_digest",
    "minimum_supported_version", "minimum_schema", "database_migration", "restart_required",
    "release_notes_summary", "release_url", "installation_notes", "rollback_notes", "manifest_identity",
}
_UPDATE_CACHE = {"entries": {}, "last_good": {}, "inflight": {}}
_UPDATE_CACHE_LOCK = threading.Lock()


def _value(environ, name, default):
    value = str(environ.get(name) or "").strip()
    return value or default


def _commit_sha(value):
    value = str(value or "").strip().lower()
    return value if re.fullmatch(r"[0-9a-f]{7,64}", value) else DEFAULT_COMMIT_SHA


def _build_date(value):
    value = str(value or "").strip()
    if not value or value.lower() == "unknown":
        return DEFAULT_BUILD_DATE
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return DEFAULT_BUILD_DATE
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _positive_integer(value):
    try:
        parsed = int(str(value or "").strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def candidate_manifest_path(environ=None):
    env = os.environ if environ is None else environ
    explicit = str(env.get("INKDROP_CANDIDATE_MANIFEST_PATH") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    state_dir = Path(str(env.get("INKDROP_STATE_DIR") or "/state")).expanduser()
    return state_dir / "release" / "current.json"


def load_candidate_manifest(environ=None):
    path = candidate_manifest_path(environ)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, "missing"
    except (OSError, ValueError, TypeError):
        return {}, "invalid"
    if not isinstance(payload, dict):
        return {}, "invalid"
    if payload.get("schema") != CANDIDATE_MANIFEST_SCHEMA:
        return payload, "invalid"
    if int(payload.get("manifest_schema_version") or 0) != CANDIDATE_MANIFEST_SCHEMA_VERSION:
        return payload, "invalid"
    return payload, "loaded"


def build_metadata(environ=None):
    env = os.environ if environ is None else environ
    version = _value(env, "INKDROP_VERSION", DEFAULT_VERSION)
    commit_sha = _commit_sha(_value(env, "INKDROP_COMMIT_SHA", DEFAULT_COMMIT_SHA))
    build_date = _build_date(_value(env, "INKDROP_BUILD_DATE", DEFAULT_BUILD_DATE))
    release_channel = _value(env, "INKDROP_RELEASE_CHANNEL", DEFAULT_RELEASE_CHANNEL).lower()
    image_revision = _value(env, "INKDROP_IMAGE_REVISION", commit_sha)
    image_digest = _value(env, "INKDROP_IMAGE_DIGEST", "")
    image_repository = _value(env, "INKDROP_IMAGE_REPOSITORY", "")
    qa_build_number = _positive_integer(env.get("INKDROP_QA_BUILD_NUMBER"))
    state_schema_version = _positive_integer(env.get("INKDROP_STATE_SCHEMA_VERSION"))
    candidate_manifest, manifest_status = load_candidate_manifest(env)
    manifest_commit = _commit_sha(candidate_manifest.get("full_commit_sha") or candidate_manifest.get("commit_sha"))
    manifest_mismatches = []
    if manifest_status == "loaded":
        comparisons = {
            "commit_sha": (manifest_commit, commit_sha),
            "version": (_value(candidate_manifest, "version", ""), version),
            "build_date": (_build_date(candidate_manifest.get("build_date")), build_date),
            "release_channel": (_value(candidate_manifest, "release_channel", "").lower(), release_channel),
            "image_digest": (_value(candidate_manifest, "image_digest", "").lower(), image_digest.lower()),
            "qa_build_number": (_positive_integer(candidate_manifest.get("qa_build_number")), qa_build_number),
        }
        if image_repository:
            comparisons["image_repository"] = (
                _value(candidate_manifest, "image_repository", "").lower(),
                image_repository.lower(),
            )
        if state_schema_version:
            comparisons["state_schema_version"] = (
                _positive_integer(candidate_manifest.get("state_schema_version")),
                state_schema_version,
            )
        manifest_mismatches = [key for key, (expected, actual) in comparisons.items() if expected != actual]
        if manifest_mismatches:
            manifest_status = "stale" if "commit_sha" in manifest_mismatches else "mismatch"
    manifest_matches = manifest_status == "loaded" and not manifest_mismatches
    development = release_channel == "dev" or version.lower() in {"dev", "development"}
    short_sha = commit_sha[:7] if commit_sha != DEFAULT_COMMIT_SHA else ""
    if development:
        display_version = f"dev+{short_sha}" if short_sha else "dev"
    else:
        display_version = version
    metadata = {
        "product": PRODUCT_NAME,
        "product_name": PRODUCT_NAME,
        "version": version,
        "commit_sha": commit_sha,
        "short_commit_sha": short_sha,
        "short_sha": short_sha,
        "build_date": build_date,
        "release_channel": release_channel,
        "channel": release_channel,
        "development": development,
        "display_version": display_version,
        "qa_build_number": qa_build_number,
        "image_revision": image_revision if image_revision != DEFAULT_COMMIT_SHA else "",
        "image_digest": image_digest,
        "image_repository": image_repository,
        "candidate_manifest_status": "matched" if manifest_matches else manifest_status,
        "oci": {
            "org.opencontainers.image.title": PRODUCT_NAME,
            "org.opencontainers.image.version": version,
            "org.opencontainers.image.revision": image_revision if image_revision != DEFAULT_COMMIT_SHA else "",
            "org.opencontainers.image.created": build_date if build_date != DEFAULT_BUILD_DATE else "",
            "io.inkdrop.qa.build-number": str(qa_build_number or ""),
        },
    }
    if manifest_matches:
        metadata["candidate_manifest"] = {
            key: candidate_manifest.get(key)
            for key in (
                "schema",
                "manifest_schema_version",
                "branch",
                "full_commit_sha",
                "short_sha",
                "version",
                "release_channel",
                "build_date",
                "image_repository",
                "image_tag",
                "image_digest",
                "workflow_run_id",
                "qa_build_number",
                "state_schema_version",
            )
            if candidate_manifest.get(key) not in (None, "")
        }
        metadata["candidate_branch"] = str(candidate_manifest.get("branch") or "")
        metadata["candidate_workflow_run_id"] = str(candidate_manifest.get("workflow_run_id") or "")
        metadata["state_schema_version"] = candidate_manifest.get("state_schema_version")
    elif manifest_mismatches:
        metadata["candidate_manifest_mismatches"] = sorted(manifest_mismatches)
    return metadata


def server_version(environ=None):
    metadata = build_metadata(environ)
    return f"{PRODUCT_NAME}/{metadata['display_version']}"


def _update_timestamp(value):
    raw = str(value or "").strip()
    parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    if parsed.tzinfo is None:
        raise ValueError("published_at must include a timezone")
    return parsed.astimezone(timezone.utc)


def normalize_update_timestamp(value):
    """Return the canonical second-precision UTC timestamp used in manifests."""
    return _update_timestamp(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def _update_semver(value):
    # Leading zeros are allowed in the numeric parts, which strict semver
    # forbids. InkDrop's release counter lives in the patch position and is
    # zero-padded on purpose -- 0.1.01-beta, 0.1.02-beta -- so rejecting "01"
    # would rule out every release the project actually cuts. Build metadata
    # is still refused, since the update manifest has nowhere to put it.
    raw = str(value or "").strip()
    if not re.fullmatch(r"\d+(?:\.\d+){2}(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?", raw):
        raise ValueError("version must be a semantic version without build metadata")
    return raw


def _update_digest(value):
    raw = str(value or "").strip().lower()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", raw):
        raise ValueError("image_digest must be an immutable sha256 digest")
    return raw


def _bounded_manifest_text(value, label, minimum=1, maximum=2000):
    raw = str(value or "").strip()
    if len(raw) < minimum or len(raw.encode("utf-8")) > maximum or any(ord(char) < 32 and char not in "\n\t" for char in raw):
        raise ValueError(f"{label} is missing or outside its bound")
    assignment = r"(?i)\b(?:token|access[_-]?token|refresh[_-]?token|id[_-]?token|session[_-]?token|auth[_-]?token|api[_-]?key|client[_-]?secret|private[_-]?key|authorization|proxy[_-]?authorization|password|passwd|secret|credential)\s*[:=]\s*[^\s]+"
    authorization = r"(?i)\b(?:proxy[_-]?)?authorization\s*:\s*(?:bearer|basic)\s+[^\s]+"
    bearer = r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"
    token_shape = r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})\b"
    if any(re.search(pattern, raw) for pattern in (assignment, authorization, bearer, token_shape)):
        raise ValueError(f"{label} contains secret-like material")
    return raw


def _bounded_cache_put(mapping, key, value, limit=UPDATE_CACHE_MAX_CONTEXTS):
    mapping.pop(key, None)
    mapping[key] = value
    while len(mapping) > max(1, int(limit or 8)):
        mapping.pop(next(iter(mapping)))


def update_manifest_identity(payload):
    unsigned = {key: value for key, value in payload.items() if key != "manifest_identity"}
    canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def validate_update_manifest(payload, now=None, max_age_seconds=UPDATE_MANIFEST_MAX_AGE_SECONDS):
    if not isinstance(payload, dict):
        raise ValueError("update manifest must be an object")
    keys = set(payload)
    if not UPDATE_MANIFEST_REQUIRED_KEYS.issubset(keys) or keys - (UPDATE_MANIFEST_REQUIRED_KEYS | {"maximum_schema"}):
        raise ValueError(f"update manifest fields differ: {sorted(keys ^ UPDATE_MANIFEST_REQUIRED_KEYS)}")
    if payload.get("schema_version") != UPDATE_MANIFEST_SCHEMA_VERSION:
        raise ValueError("unsupported update manifest schema")
    if payload.get("product") != PRODUCT_NAME:
        raise ValueError("update manifest product mismatch")
    if payload.get("validated") is not True:
        raise ValueError("update manifest is not validated")
    version = _update_semver(payload.get("version"))
    prerelease = payload.get("prerelease")
    if not isinstance(prerelease, bool) or prerelease != ("-" in version):
        raise ValueError("prerelease flag does not match version")
    channel = str(payload.get("channel") or "").strip().lower()
    if channel not in {"qa", "alpha", "beta", "stable"}:
        raise ValueError("unsupported update channel")
    published = _update_timestamp(payload.get("published_at"))
    current = datetime.fromtimestamp(float(time.time() if now is None else now), tz=timezone.utc)
    age = (current - published).total_seconds()
    if age < -UPDATE_MANIFEST_FUTURE_SECONDS:
        raise ValueError("update manifest is from the future")
    if age > max(3600, min(90 * 24 * 60 * 60, int(max_age_seconds))):
        raise ValueError("update manifest is stale")
    commit = str(payload.get("commit_sha") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("commit_sha must contain exactly 40 hexadecimal characters")
    repository = str(payload.get("image_repository") or "").strip().lower()
    if repository != UPDATE_IMAGE_REPOSITORIES[channel]:
        raise ValueError("image_repository does not match the approved InkDrop channel repository")
    digest = _update_digest(payload.get("image_digest"))
    minimum_version = _update_semver(payload.get("minimum_supported_version"))
    try:
        minimum_schema = int(payload.get("minimum_schema"))
        maximum_schema = int(payload["maximum_schema"]) if payload.get("maximum_schema") is not None else None
    except (TypeError, ValueError) as exc:
        raise ValueError("schema compatibility bounds must be integers") from exc
    if minimum_schema < 1 or (maximum_schema is not None and maximum_schema < minimum_schema):
        raise ValueError("schema compatibility bounds are invalid")
    if not isinstance(payload.get("database_migration"), bool) or not isinstance(payload.get("restart_required"), bool):
        raise ValueError("migration and restart flags must be booleans")
    if payload["database_migration"] and not payload["restart_required"]:
        raise ValueError("database migrations require a service restart")
    release_url = str(payload.get("release_url") or "").strip()
    if release_url != f"https://github.com/jaredbahr/inkdrop-dev/releases/tag/v{version}":
        raise ValueError("release_url must identify the exact InkDrop release")
    normalized = {
        "schema_version": UPDATE_MANIFEST_SCHEMA_VERSION,
        "product": PRODUCT_NAME,
        "version": version,
        "channel": channel,
        "prerelease": prerelease,
        "validated": True,
        "published_at": normalize_update_timestamp(published),
        "commit_sha": commit,
        "image_repository": repository,
        "image_digest": digest,
        "minimum_supported_version": minimum_version,
        "minimum_schema": minimum_schema,
        "database_migration": payload["database_migration"],
        "restart_required": payload["restart_required"],
        "release_notes_summary": _bounded_manifest_text(payload.get("release_notes_summary"), "release_notes_summary", 20, 4000),
        "release_url": release_url,
        "installation_notes": _bounded_manifest_text(payload.get("installation_notes"), "installation_notes", 20, 4000),
        "rollback_notes": _bounded_manifest_text(payload.get("rollback_notes"), "rollback_notes", 20, 4000),
    }
    if maximum_schema is not None:
        normalized["maximum_schema"] = maximum_schema
    expected_identity = update_manifest_identity(normalized)
    supplied_identity = str(payload.get("manifest_identity") or "").strip().lower()
    if not hmac.compare_digest(supplied_identity, expected_identity):
        raise ValueError("update manifest identity mismatch")
    normalized["manifest_identity"] = expected_identity
    return normalized


def _load_update_bytes(raw, now=None, max_age_seconds=UPDATE_MANIFEST_MAX_AGE_SECONDS):
    if not raw or len(raw) > UPDATE_MANIFEST_MAX_BYTES:
        raise ValueError("update manifest is empty or exceeds 64 KiB")
    return validate_update_manifest(json.loads(raw.decode("utf-8")), now=now, max_age_seconds=max_age_seconds)


def load_update_manifest(path, now=None, max_age_seconds=UPDATE_MANIFEST_MAX_AGE_SECONDS):
    target = Path(path)
    try:
        if target.stat().st_size > UPDATE_MANIFEST_MAX_BYTES:
            raise ValueError("update manifest exceeds 64 KiB")
        with target.open("rb") as handle:
            return _load_update_bytes(handle.read(UPDATE_MANIFEST_MAX_BYTES + 1), now, max_age_seconds)
    except FileNotFoundError:
        return None


def _approved_update_url(value):
    raw = str(value or "").strip()
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("update URL must be secret-free HTTPS")
    host = (parsed.hostname or "").lower()
    if host != "github.com" or not re.fullmatch(r"/jaredbahr/inkdrop-dev/releases/(?:latest/download|download/v[0-9A-Za-z.-]+)/inkdrop-update-manifest\.json", parsed.path):
        raise ValueError("update URL is outside the approved InkDrop release path")
    return raw


def _fetch_update_manifest(url, connect_timeout, total_timeout):
    started = time.monotonic()
    deadline = started + total_timeout
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "InkDrop-update-awareness/1"})
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("update check exceeded its total timeout")
        with urllib.request.urlopen(request, timeout=min(connect_timeout, remaining)) as response:
            final = urllib.parse.urlsplit(response.geturl())
            if final.scheme != "https" or not ((final.hostname or "").lower() == "github.com" or (final.hostname or "").lower().endswith(".githubusercontent.com" or ".github.com")):
                raise OSError("update response redirect was not approved")
            length = response.headers.get("Content-Length")
            if length and int(length) > UPDATE_MANIFEST_MAX_BYTES:
                raise ValueError("remote update manifest exceeds 64 KiB")
            chunks, size = [], 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("update check exceeded its total timeout")
                socket = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
                if socket is not None:
                    socket.settimeout(min(connect_timeout, remaining))
                chunk = response.read(min(8192, UPDATE_MANIFEST_MAX_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > UPDATE_MANIFEST_MAX_BYTES:
                    raise ValueError("remote update manifest exceeds 64 KiB")
            return b"".join(chunks)
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise TimeoutError("update check timed out") from exc
        raise OSError("update check unavailable") from exc


def _fetch_with_deadline(fetcher, url, connect_timeout, total_timeout):
    """Bound even injected or unexpectedly blocking fetchers by wall clock."""
    results = queue.Queue(maxsize=1)
    deadline = time.monotonic() + total_timeout

    def invoke():
        try:
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                raise TimeoutError("update check exceeded its total timeout")
            results.put((True, fetcher(url, min(connect_timeout, remaining), remaining)))
        except Exception as exc:
            results.put((False, exc))

    threading.Thread(target=invoke, name="inkdrop-update-fetch", daemon=True).start()
    try:
        succeeded, value = results.get(timeout=max(0.0, deadline - time.monotonic()))
    except queue.Empty as exc:
        raise TimeoutError("update check exceeded its total timeout") from exc
    if succeeded:
        return value
    raise value


def _compare_update_versions(left, right):
    def comparable(value):
        core, _, pre = _update_semver(value).partition("-")
        tokens = [] if not pre else [(0, int(part)) if part.isdigit() else (1, part.lower()) for part in pre.split(".")]
        return tuple(int(item) for item in core.split(".")), not bool(pre), tokens
    return (comparable(left) > comparable(right)) - (comparable(left) < comparable(right))


def _bounded_number(env, name, default, minimum, maximum, floating=False):
    try:
        value = float(env.get(name) or default) if floating else int(env.get(name) or default)
        return max(minimum, min(maximum, value))
    except (TypeError, ValueError):
        return default


def _status_copy(state):
    return {
        "up_to_date": ("InkDrop is up to date", "The installed immutable image is current for the selected channel."),
        "update_available": ("An update is available", "Use your external container manager to update web and worker together."),
        "newer_prerelease_available": ("A newer prerelease is available", "Review the prerelease notes before updating both services."),
        "channel_mismatch": ("Update channel does not match", "The checked release is outside the selected update channel."),
        "unsupported_install": ("This install cannot be updated safely", "Current web and worker identity must be verified before update guidance is actionable."),
        "update_check_unavailable": ("Update check unavailable", "No validated cached release is available; InkDrop continues running normally."),
        "invalid_manifest": ("Update metadata is invalid", "The remote or cached manifest failed validation and was rejected."),
        "incompatible_update": ("Available update is incompatible", "Version, database schema, or migration requirements are not compatible with this install."),
        "development_build": ("Development build", "Automatic update comparison is disabled for development builds."),
    }[state]


def update_status(environ=None, now=None, fetcher=None, allow_remote=True):
    env = dict(os.environ if environ is None else environ)
    current_time = float(time.time() if now is None else now)
    ttl = int(_bounded_number(env, "INKDROP_UPDATE_CACHE_SECONDS", UPDATE_CACHE_SECONDS, 60, 86400))
    max_age = int(_bounded_number(env, "INKDROP_UPDATE_MANIFEST_MAX_AGE_SECONDS", UPDATE_MANIFEST_MAX_AGE_SECONDS, 3600, 90 * 86400))
    connect_timeout = float(_bounded_number(env, "INKDROP_UPDATE_CONNECT_TIMEOUT_SECONDS", UPDATE_CONNECT_TIMEOUT_SECONDS, .25, 10, True))
    total_timeout = float(_bounded_number(env, "INKDROP_UPDATE_TOTAL_TIMEOUT_SECONDS", UPDATE_TOTAL_TIMEOUT_SECONDS, 1, 20, True))
    configured_url = str(env.get("INKDROP_UPDATE_MANIFEST_URL") or DEFAULT_UPDATE_MANIFEST_URL).strip()
    remote_enabled = bool(allow_remote) and bool(configured_url) and str(env.get("INKDROP_UPDATE_REMOTE_ENABLED") or "1").strip().lower() not in {"0", "false", "no", "off"}
    remote_configuration_invalid = False
    try:
        remote_url = _approved_update_url(configured_url) if remote_enabled else ""
    except ValueError:
        remote_url, remote_configuration_invalid = "", True
    path = Path(str(env.get("INKDROP_UPDATE_MANIFEST_PATH") or (candidate_manifest_path(env).parent / "latest-update.json")))
    selected_channel = str(env.get("INKDROP_UPDATE_CHANNEL") or env.get("INKDROP_RELEASE_CHANNEL") or "stable").strip().lower()
    context_key = (str(path.resolve()), configured_url, selected_channel, max_age)
    key = (*context_key, remote_enabled)
    source_result = None
    flight = None
    flight_owner = False
    with _UPDATE_CACHE_LOCK:
        entries = _UPDATE_CACHE.setdefault("entries", {})
        last_good_by_context = _UPDATE_CACHE.setdefault("last_good", {})
        inflight = _UPDATE_CACHE.setdefault("inflight", {})
        entry = entries.get(key) if isinstance(entries.get(key), dict) else {}
        age = max(0, current_time - float(entry.get("loaded_at") or 0))
        if isinstance(entry.get("result"), dict) and age < ttl:
            cached = dict(entry["result"])
            cached["cache_age_seconds"] = int(age)
            source_result = cached
        elif remote_enabled:
            flight = inflight.get(key)
            if flight is None:
                if len(inflight) >= UPDATE_CACHE_MAX_CONTEXTS:
                    source_result = {
                        "manifest": None,
                        "source": "none",
                        "failure": "unavailable",
                        "invalid": False,
                        "cache_age_seconds": 0,
                    }
                else:
                    flight = threading.Event()
                    inflight[key] = flight
                    flight_owner = True

    if source_result is None and remote_enabled and not flight_owner:
        flight.wait(timeout=total_timeout + 0.25)
        with _UPDATE_CACHE_LOCK:
            entry = (_UPDATE_CACHE.get("entries") or {}).get(key) or {}
            age = max(0, current_time - float(entry.get("loaded_at") or 0))
            if isinstance(entry.get("result"), dict):
                source_result = dict(entry["result"])
                source_result["cache_age_seconds"] = int(age)
        if source_result is None:
            source_result = {
                "manifest": None,
                "source": "none",
                "failure": "timeout",
                "invalid": False,
                "cache_age_seconds": 0,
            }

    if source_result is None:
        local_good = None
        local_invalid = False
        try:
            local_good = load_update_manifest(path, current_time, max_age)
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            local_invalid = True
        with _UPDATE_CACHE_LOCK:
            cached_good = (_UPDATE_CACHE.get("last_good") or {}).get(context_key)
        if cached_good is not None:
            try:
                cached_good = validate_update_manifest(cached_good, now=current_time, max_age_seconds=max_age)
            except (ValueError, TypeError):
                cached_good = None
                with _UPDATE_CACHE_LOCK:
                    (_UPDATE_CACHE.setdefault("last_good", {})).pop(context_key, None)
        manifest, source, failure = local_good, "local_file" if local_good else "none", ""
        if not remote_enabled and manifest is None and cached_good is not None:
            manifest, source = cached_good, "last_known_good"
        invalid = remote_configuration_invalid or (local_invalid and not remote_enabled)
        if remote_enabled:
            try:
                raw = _fetch_with_deadline(fetcher or _fetch_update_manifest, remote_url, connect_timeout, total_timeout)
                manifest = _load_update_bytes(raw, current_time, max_age)
                source = "remote"
            except TimeoutError:
                manifest = cached_good or local_good
                if manifest is not None:
                    try:
                        manifest = validate_update_manifest(manifest, now=current_time, max_age_seconds=max_age)
                    except (ValueError, TypeError):
                        manifest = None
                source = "last_known_good" if manifest else "none"
                failure = "timeout"
            except OSError:
                manifest = cached_good or local_good
                if manifest is not None:
                    try:
                        manifest = validate_update_manifest(manifest, now=current_time, max_age_seconds=max_age)
                    except (ValueError, TypeError):
                        manifest = None
                source = "last_known_good" if manifest else "none"
                failure = "unavailable"
            except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
                manifest, source, failure, invalid = None, "remote", "invalid", True
            except Exception:
                manifest = cached_good or local_good
                source = "last_known_good" if manifest else "none"
                failure = "unavailable"
        source_result = {"manifest": manifest, "source": source, "failure": failure, "invalid": invalid, "cache_age_seconds": 0}
        with _UPDATE_CACHE_LOCK:
            entries = _UPDATE_CACHE.setdefault("entries", {})
            last_good_by_context = _UPDATE_CACHE.setdefault("last_good", {})
            if source == "remote" and manifest is not None:
                _bounded_cache_put(last_good_by_context, context_key, manifest)
                entries.pop((*context_key, False), None)
            elif not remote_enabled and local_good:
                _bounded_cache_put(last_good_by_context, context_key, local_good)
            _bounded_cache_put(entries, key, {"loaded_at": current_time, "result": dict(source_result)})
            if flight_owner:
                (_UPDATE_CACHE.setdefault("inflight", {})).pop(key, None)
                flight.set()

    current = build_metadata(env)
    current_version = str(current.get("version") or "dev")
    current_digest = str(current.get("image_digest") or "").strip().lower()
    current_schema = current.get("state_schema_version")
    current_repository = str((current.get("candidate_manifest") or {}).get("image_repository") or "").strip().lower()
    expected_current_repository = UPDATE_IMAGE_REPOSITORIES.get(str(current.get("release_channel") or "").lower())
    worker_raw = str(env.get("INKDROP_WORKER_IMAGE_DIGEST") or "").strip().lower()
    worker_valid = not worker_raw or bool(re.fullmatch(r"sha256:[0-9a-f]{64}", worker_raw))
    deployment_consistent = None if not worker_raw or not current_digest else worker_raw == current_digest
    manifest = source_result.get("manifest")
    if current.get("development") or current_version == "dev":
        state = "development_build"
    elif current.get("candidate_manifest_status") != "matched" or not re.fullmatch(r"sha256:[0-9a-f]{64}", current_digest) or current_schema is None or not worker_valid or deployment_consistent is False or not current_repository or current_repository != expected_current_repository:
        state = "unsupported_install"
    elif source_result.get("invalid"):
        state = "invalid_manifest"
    elif not manifest:
        state = "update_check_unavailable"
    elif selected_channel not in {"qa", "alpha", "beta", "stable"} or manifest["channel"] != selected_channel or (selected_channel == "stable" and manifest["prerelease"]):
        state = "channel_mismatch"
    elif _compare_update_versions(current_version, manifest["minimum_supported_version"]) < 0 or int(current_schema) < manifest["minimum_schema"] or (manifest.get("maximum_schema") is not None and int(current_schema) > manifest["maximum_schema"]):
        state = "incompatible_update"
    elif manifest["image_digest"] == current_digest or _compare_update_versions(manifest["version"], current_version) < 0:
        state = "up_to_date"
    elif manifest["prerelease"] and _compare_update_versions(manifest["version"], current_version) > 0:
        state = "newer_prerelease_available"
    else:
        state = "update_available"
    if state not in UPDATE_STATES:
        raise RuntimeError("noncanonical update state")
    label, detail = _status_copy(state)
    actionable = state in {"update_available", "newer_prerelease_available"}
    image_ref = f"{manifest['image_repository']}@{manifest['image_digest']}" if manifest and actionable else ""
    commands = {
        "pull": f"docker pull {image_ref}",
        "compose": f"INKDROP_IMAGE='{image_ref}' docker compose up -d --no-deps --force-recreate inkdrop inkdrop-worker",
    } if image_ref else {}
    return {
        "ok": True, "schema": UPDATE_STATUS_SCHEMA, "status_schema_version": UPDATE_STATUS_SCHEMA_VERSION,
        "state": state, "label": label, "detail": detail, "update_available": True if actionable else False if state == "up_to_date" else None,
        "selected_channel": selected_channel,
        "current": {"version": current_version, "channel": current.get("release_channel") or "dev", "commit_sha": current.get("commit_sha") or "unknown", "image_repository": current_repository, "image_digest": current_digest, "state_schema_version": current_schema, "candidate_manifest_status": current.get("candidate_manifest_status") or "unknown"},
        "latest": manifest,
        "deployment": {"web_image_digest": current_digest, "worker_image_digest": worker_raw if worker_valid else "", "consistent": deployment_consistent, "worker_identity_reported": bool(worker_raw), "worker_identity_valid": worker_valid},
        "cache": {"state": "fresh" if not source_result.get("failure") else source_result["failure"], "age_seconds": source_result.get("cache_age_seconds", 0), "ttl_seconds": ttl, "source": source_result.get("source"), "remote_fetch": remote_enabled, "last_known_good": source_result.get("source") == "last_known_good", "connect_timeout_seconds": connect_timeout, "total_timeout_seconds": total_timeout},
        "external_updater": {"required": True, "install_available": False, "supported_managers": ["Dockhand", "Docker Compose", "Portainer"], "image_ref": image_ref, "commands": commands},
    }


def clear_update_cache():
    with _UPDATE_CACHE_LOCK:
        _UPDATE_CACHE.update({"entries": {}, "last_good": {}, "inflight": {}})
