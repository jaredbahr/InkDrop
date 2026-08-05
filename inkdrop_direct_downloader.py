"""Safe direct-download staging helper for InkDrop source providers.

The helper writes files only when a caller supplies an HTTP client and an
explicit staging path. It does not talk to the database or import into Kavita.
"""

from __future__ import annotations

import json
import errno
import hashlib
import os
import time
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import inkdrop_sources
import inkdrop_source_providers as providers


CONTRACT_VERSION = 1
DEFAULT_CHUNK_SIZE = 1024 * 1024
DEFAULT_MAX_REDIRECTS = 5
HTML_SAMPLE_BYTES = 8192

HTML_MARKERS = (
    b"<!doctype html",
    b"<html",
    b"<head",
    b"<body",
    b"captcha",
    b"cloudflare",
    b"just a moment",
    b"access denied",
    b"login",
    b"enable cookies",
)


FAILURE_CLASS_INFRASTRUCTURE = "infrastructure"
FAILURE_CLASS_SOURCE_CONTENT = "source_content"
FAILURE_CLASS_POLICY = "policy"


def _blocked(reason, *, failure_class=FAILURE_CLASS_INFRASTRUCTURE, **extra):
    out = {
        "direct_download_contract_version": CONTRACT_VERSION,
        "ok": False,
        "status": "blocked",
        "reason": str(reason or "direct_download_blocked"),
        "failure_reason": str(reason or "direct_download_blocked"),
        "failure_class": failure_class,
    }
    out.update({key: value for key, value in extra.items() if value not in (None, "", [], {})})
    return out


def _source_content_blocked(reason, **extra):
    return _blocked(reason, failure_class=FAILURE_CLASS_SOURCE_CONTENT, **extra)


def _policy_blocked(reason, **extra):
    return _blocked(reason, failure_class=FAILURE_CLASS_POLICY, **extra)


def _headers_map(headers):
    headers = headers if isinstance(headers, dict) else {}
    return {str(key): value for key, value in headers.items()}


def _header_value(headers, key):
    lowered = {str(name).lower(): value for name, value in _headers_map(headers).items()}
    return lowered.get(str(key).lower())


def _intish(value, default=None):
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except Exception:
        return default


def _sha256_text(value):
    text = str(value or "").strip()
    if not text:
        return ""
    return "sha256:" + providers.url_hash(text)


def _safe_http_url(value):
    parsed = urlparse(str(value or "").strip())
    return str(parsed.scheme or "").lower() in {"http", "https"} and bool(parsed.netloc)


def _normalized_hosts(values):
    out = []
    seen = set()
    for value in values or []:
        text = str(value or "").strip().lower()
        if "://" in text:
            text = str(urlparse(text).hostname or "").strip().lower()
        text = text.strip("[]")
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _host_allowed(url, allowed_hosts):
    allowed = set(_normalized_hosts(allowed_hosts))
    host = str(urlparse(str(url or "")).hostname or "").strip().lower()
    return bool(host and (not allowed or host in allowed))


def _effective_host_cap(global_hosts, route_hosts):
    global_cap = set(_normalized_hosts(global_hosts))
    route_cap = set(_normalized_hosts(route_hosts))
    if not route_cap:
        return sorted(global_cap)
    if not global_cap:
        return sorted(route_cap)
    effective = global_cap & route_cap
    return sorted(effective) if effective else ["__inkdrop_no_allowed_host__"]


def _safe_response_headers(headers):
    headers = _headers_map(headers)
    allowed = {
        "accept-ranges",
        "content-disposition",
        "content-length",
        "content-range",
        "content-type",
        "etag",
        "last-modified",
    }
    return {key: value for key, value in headers.items() if str(key).lower() in allowed}


def _content_type(headers, fallback=""):
    return providers.content_type_base(_header_value(headers, "content-type") or fallback)


def _allowed_content_types(values):
    out = set()
    for value in values or []:
        content_type = providers.content_type_base(value)
        if content_type:
            out.add(content_type)
    return out


def _content_type_allowed(content_type, allowed_content_types, *, allow_generic_content_type=True):
    if not content_type:
        return True
    if content_type in providers.HTML_CONTENT_TYPES:
        return False
    allowed = _allowed_content_types(allowed_content_types)
    if not allowed:
        return True
    if content_type in allowed:
        return True
    if allow_generic_content_type and content_type in {"application/octet-stream", "binary/octet-stream"}:
        return True
    return False


def _looks_html(sample):
    if not sample:
        return False
    lowered = bytes(sample[:HTML_SAMPLE_BYTES]).lower().lstrip()
    return any(marker in lowered for marker in HTML_MARKERS)


def _archive_validation_reason(path, extension):
    path = Path(path)
    extension = providers.normalize_extension(extension)
    if extension in {".cbz", ".epub", ".zip"}:
        if zipfile.is_zipfile(path):
            return ""
        return "archive_validation_failed"
    try:
        with path.open("rb") as handle:
            head = handle.read(512)
    except Exception:
        return "archive_validation_failed"
    if extension == ".pdf":
        return "" if head.startswith(b"%PDF-") else "archive_validation_failed"
    if extension in {".cbr", ".rar"}:
        if head.startswith(b"Rar!\x1a\x07\x00") or head.startswith(b"Rar!\x1a\x07\x01\x00"):
            return ""
        return "archive_validation_failed"
    if extension in {".mobi", ".azw3"}:
        return "" if b"BOOKMOBI" in head else "archive_validation_failed"
    return ""


def _safe_jsonish(value):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            key_text = str(key)
            lower = key_text.lower()
            if isinstance(item, str) and item.startswith(("http://", "https://")) and (
                "url" in lower or "link" in lower
            ):
                out[f"{key_text}_hash"] = _sha256_text(item)
                continue
            out[key_text] = _safe_jsonish(item)
        return out
    if isinstance(value, list):
        return [_safe_jsonish(item) for item in value]
    if isinstance(value, tuple):
        return [_safe_jsonish(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _response_from_http_get(http_get, request):
    try:
        return http_get(request)
    except TypeError:
        return http_get(request["url"], headers=request.get("headers"), timeout=request.get("timeout_seconds"))


def _response_parts(response):
    headers = {}
    status_code = None
    final_url = ""
    redirect_count = 0
    body = response
    if isinstance(response, tuple):
        if response:
            body = response[0]
        if len(response) > 1 and isinstance(response[1], dict):
            headers = response[1]
        if len(response) > 2:
            status_code = _intish(response[2])
    elif isinstance(response, dict):
        headers = response.get("headers") if isinstance(response.get("headers"), dict) else {}
        status_code = _intish(response.get("status_code", response.get("status")))
        final_url = str(response.get("final_url") or response.get("url") or "").strip()
        redirect_count = _intish(response.get("redirect_count"), 0) or 0
        if "body" in response:
            body = response.get("body")
        elif "content" in response:
            body = response.get("content")
        elif "bytes" in response:
            body = response.get("bytes")
        elif "text" in response:
            body = response.get("text")
        else:
            body = b""
    return {
        "headers": _headers_map(headers),
        "status_code": status_code,
        "final_url": final_url,
        "redirect_count": redirect_count,
        "body": body,
    }


def _iter_chunks(body, chunk_size=DEFAULT_CHUNK_SIZE):
    if body is None:
        return
    if isinstance(body, str):
        data = body.encode("utf-8")
        for index in range(0, len(data), chunk_size):
            yield data[index:index + chunk_size]
        return
    if isinstance(body, bytes):
        for index in range(0, len(body), chunk_size):
            yield body[index:index + chunk_size]
        return
    if hasattr(body, "iter_content"):
        for chunk in body.iter_content(chunk_size=chunk_size):
            if chunk:
                yield bytes(chunk)
        return
    if hasattr(body, "read"):
        while True:
            chunk = body.read(chunk_size)
            if not chunk:
                break
            yield bytes(chunk)
        return
    if hasattr(body, "__iter__"):
        for chunk in body:
            if chunk:
                yield bytes(chunk)


def _confined_paths(download_root, incomplete_root=None, *, final_filename=None, local_path=None, partial_path=None):
    """Resolve the final and partial paths, each confined to its own trusted root.

    ``download_root`` confines the final file; ``incomplete_root`` confines the
    in-progress ``.part`` file. They are validated independently -- a caller
    with a separately configured scratch volume for ``incomplete_root`` must
    not be forced to share ``download_root``'s confinement boundary. When
    ``incomplete_root`` is omitted it defaults to ``download_root``, which
    reproduces the original single-root behavior for callers that only know
    about one staging root.
    """
    if not str(download_root or "").strip():
        return None, None, None, None, "missing_staging_root"
    root = Path(download_root).expanduser().resolve()
    incomplete = (
        Path(incomplete_root).expanduser().resolve()
        if str(incomplete_root or "").strip()
        else root
    )
    if local_path:
        target = Path(str(local_path)).expanduser()
        if not target.is_absolute():
            target = root / target
    else:
        if not str(final_filename or "").strip():
            return root, None, incomplete, None, "missing_final_filename"
        target = root / str(final_filename)
    target = target.resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return root, target, incomplete, None, "target_outside_staging_root"
    if target == root:
        return root, target, incomplete, None, "target_is_staging_root"
    if partial_path:
        partial = Path(str(partial_path)).expanduser()
        if not partial.is_absolute():
            partial = incomplete / partial
    else:
        partial = incomplete / (target.name + ".part")
    partial = partial.resolve()
    try:
        partial.relative_to(incomplete)
    except ValueError:
        return root, target, incomplete, partial, "partial_outside_staging_root"
    if partial == target:
        return root, target, incomplete, partial, "partial_matches_final_path"
    return root, target, incomplete, partial, ""


def _close_body(body):
    close = getattr(body, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _is_response_too_large(exc):
    return str(getattr(exc, "reason", "") or "") == "response_too_large"


def _cleanup_partial(partial_path, root):
    try:
        partial = Path(partial_path).resolve()
        partial.relative_to(Path(root).resolve())
        if partial.exists() and partial.is_file():
            partial.unlink()
    except Exception:
        pass


def _publish_staged_file(partial, target, download_root, content_hash_hex):
    """Atomically publish a validated ``.part`` file to its final name.

    ``incomplete_root`` and ``download_root`` can be configured on separate
    filesystems (that is the whole point of independently configuring them),
    so the fast-path ``replace()`` can raise ``EXDEV`` -- there is no atomic
    rename across devices in POSIX. Fall back to copying the already-validated
    bytes into a same-filesystem temp file next to the final name, verifying
    the copy against the hash computed while streaming the download, then
    atomically replacing the final name from that local temp file. The source
    partial is removed only after the copy is verified and published, so a
    crash mid-copy leaves the original partial recoverable instead of
    silently losing a validated download.
    """
    try:
        partial.replace(target)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    tmp_target = target.with_name(f"{target.name}.publish-{os.getpid()}-{int(time.monotonic() * 1000)}.tmp")
    try:
        digest = hashlib.sha256()
        size = 0
        with partial.open("rb") as src, tmp_target.open("wb") as dst:
            while True:
                chunk = src.read(DEFAULT_CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        if digest.hexdigest() != content_hash_hex:
            raise ValueError("cross_device_copy_hash_mismatch")
        if size != partial.stat().st_size:
            raise ValueError("cross_device_copy_size_mismatch")
        tmp_target.replace(target)
    except Exception:
        _cleanup_partial(tmp_target, download_root)
        raise
    try:
        partial.unlink()
    except Exception:
        pass


def _metadata_path(final_path):
    final_path = Path(final_path)
    return final_path.with_name(final_path.name + ".source.json")


def _write_metadata(final_path, metadata):
    path = _metadata_path(final_path)
    path.write_text(json.dumps(_safe_jsonish(metadata), sort_keys=True, indent=2, ensure_ascii=True), encoding="utf-8")
    return path


def download_direct_file(
    *,
    url,
    provider_id,
    download_task_id,
    staging_root=None,
    download_root=None,
    incomplete_root=None,
    final_filename=None,
    local_path=None,
    partial_path=None,
    allowed_extensions=None,
    allowed_content_types=None,
    max_bytes=None,
    headers=None,
    timeout_seconds=60,
    http_get=None,
    source_metadata=None,
    overwrite=False,
    allow_generic_content_type=True,
    max_redirects=DEFAULT_MAX_REDIRECTS,
    allowed_hosts=None,
    expected_size_bytes=None,
    chunk_size=DEFAULT_CHUNK_SIZE,
):
    started = time.monotonic()
    url = str(url or "").strip()
    provider_id = inkdrop_sources.provider_key(provider_id)
    if not url:
        return _blocked("missing_download_url", provider_id=provider_id, download_task_id=download_task_id)
    if not _safe_http_url(url):
        return _policy_blocked("download_url_not_safe_http", provider_id=provider_id, download_task_id=download_task_id)
    allowed_hosts = _normalized_hosts(allowed_hosts)
    if allowed_hosts and not _host_allowed(url, allowed_hosts):
        return _policy_blocked("download_host_not_allowed", provider_id=provider_id, download_task_id=download_task_id)
    if not http_get:
        return _blocked("http_client_required", provider_id=provider_id, download_task_id=download_task_id)
    # download_root/incomplete_root are the trusted confinement boundaries; a
    # caller that only knows about one shared root (the pre-#281 contract)
    # still works because both fall back to staging_root.
    resolved_download_root = download_root or staging_root
    resolved_incomplete_root = incomplete_root or resolved_download_root
    root, target, incomplete_root_path, partial, path_reason = _confined_paths(
        resolved_download_root,
        resolved_incomplete_root,
        final_filename=final_filename,
        local_path=local_path,
        partial_path=partial_path,
    )
    if path_reason:
        return _blocked(
            path_reason,
            provider_id=provider_id,
            download_task_id=download_task_id,
            staging_root=str(root) if root else "",
            incomplete_root=str(incomplete_root_path) if incomplete_root_path else "",
            path=str(target) if target else "",
        )

    allowed_exts = providers.normalized_extensions(allowed_extensions or [])
    if not allowed_exts:
        return _blocked("missing_allowed_extensions", provider_id=provider_id, download_task_id=download_task_id, path=str(target))
    extension = providers.normalize_extension(str(target))
    if extension not in allowed_exts:
        return _policy_blocked(
            "extension_not_allowed",
            provider_id=provider_id,
            download_task_id=download_task_id,
            extension=extension,
            allowed_extensions=allowed_exts,
            path=str(target),
        )
    if target.exists() and not overwrite:
        return _blocked("target_exists", provider_id=provider_id, download_task_id=download_task_id, path=str(target))

    byte_limit = providers.DEFAULT_MAX_SIZE_BYTES if max_bytes is None else int(max_bytes)
    request = {
        "request_id": "direct_download",
        "method": "GET",
        "url": url,
        "headers": dict(headers or {}),
        "timeout_seconds": timeout_seconds,
        "provider_id": provider_id,
        "download_task_id": download_task_id,
        "allowed_hosts": list(allowed_hosts),
        "max_redirects": max(0, int(max_redirects or 0)),
        # Ask stream-capable clients (inkdrop_source_worker_http) to hand over
        # the connection instead of buffering the payload: chunks then go
        # straight to the .part file below. Clients without stream support
        # ignore both keys and keep returning a buffered body.
        "stream_body": True,
        "max_bytes": byte_limit,
    }
    try:
        response = _response_parts(_response_from_http_get(http_get, request))
    except Exception as exc:
        return _blocked(
            "http_request_failed",
            provider_id=provider_id,
            download_task_id=download_task_id,
            error_reason=str(getattr(exc, "reason", "") or type(exc).__name__),
        )
    response_headers = response["headers"]
    status_code = response["status_code"]
    response_body = response["body"]

    def _reject(result):
        # Stream-mode bodies hold the live connection until fully read; every
        # early return before EOF must release it. Buffered bytes bodies have
        # no close and pass through untouched.
        _close_body(response_body)
        return result

    if status_code is not None and not (200 <= status_code < 300):
        return _reject(_blocked(
            "http_status_not_success",
            provider_id=provider_id,
            download_task_id=download_task_id,
            status_code=status_code,
        ))
    if int(response.get("redirect_count") or 0) > int(max_redirects or 0):
        return _reject(_policy_blocked(
            "redirect_limit_exceeded",
            provider_id=provider_id,
            download_task_id=download_task_id,
            redirect_count=response.get("redirect_count"),
        ))
    if response.get("final_url") and not _safe_http_url(response.get("final_url")):
        return _reject(_policy_blocked(
            "final_url_not_safe_http",
            provider_id=provider_id,
            download_task_id=download_task_id,
            final_url_hash=_sha256_text(response.get("final_url")),
        ))
    if response.get("final_url") and allowed_hosts and not _host_allowed(response.get("final_url"), allowed_hosts):
        return _reject(_policy_blocked(
            "final_host_not_allowed",
            provider_id=provider_id,
            download_task_id=download_task_id,
            final_url_hash=_sha256_text(response.get("final_url")),
        ))

    content_type = _content_type(response_headers)
    if not _content_type_allowed(
        content_type,
        allowed_content_types,
        allow_generic_content_type=allow_generic_content_type,
    ):
        return _reject(_source_content_blocked(
            "content_type_not_allowed",
            provider_id=provider_id,
            download_task_id=download_task_id,
            content_type=content_type,
            allowed_content_types=sorted(_allowed_content_types(allowed_content_types)),
        ))
    declared_size = _intish(_header_value(response_headers, "content-length"))
    if declared_size is not None and declared_size > byte_limit:
        return _reject(_policy_blocked(
            "content_length_too_large",
            provider_id=provider_id,
            download_task_id=download_task_id,
            size_bytes=declared_size,
            max_bytes=byte_limit,
        ))

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        partial.parent.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return _reject(_blocked("staging_directory_create_failed", provider_id=provider_id, download_task_id=download_task_id, error=f"{type(exc).__name__}: {exc}"))
    digest = hashlib.sha256()
    size = 0
    sampled = b""
    try:
        chunks = _iter_chunks(response_body, chunk_size=max(1, int(chunk_size or DEFAULT_CHUNK_SIZE)))
        pending = []
        for chunk in chunks or []:
            if not chunk:
                continue
            pending.append(chunk)
            sampled += chunk[: max(0, HTML_SAMPLE_BYTES - len(sampled))]
            if len(sampled) >= HTML_SAMPLE_BYTES:
                break
    except Exception as exc:
        if _is_response_too_large(exc):
            return _reject(_policy_blocked("size_too_large", provider_id=provider_id, download_task_id=download_task_id, max_bytes=byte_limit))
        return _reject(_blocked("http_response_read_failed", provider_id=provider_id, download_task_id=download_task_id, error_reason=type(exc).__name__))
    if _looks_html(sampled):
        return _reject(_source_content_blocked("html_or_login_response", provider_id=provider_id, download_task_id=download_task_id, content_type=content_type))

    try:
        with partial.open("wb") as handle:
            for chunk in pending:
                size += len(chunk)
                if size > byte_limit:
                    raise ValueError("size_too_large")
                digest.update(chunk)
                handle.write(chunk)
            for chunk in chunks or []:
                if not chunk:
                    continue
                size += len(chunk)
                if size > byte_limit:
                    raise ValueError("size_too_large")
                digest.update(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
    except ValueError as exc:
        _cleanup_partial(partial, incomplete_root_path)
        return _reject(_policy_blocked(str(exc), provider_id=provider_id, download_task_id=download_task_id, max_bytes=byte_limit))
    except Exception as exc:
        _cleanup_partial(partial, incomplete_root_path)
        if _is_response_too_large(exc):
            # The transport's own limit tripped mid-stream; classify it like
            # the local size check so it lands as policy, not infrastructure.
            return _reject(_policy_blocked("size_too_large", provider_id=provider_id, download_task_id=download_task_id, max_bytes=byte_limit))
        return _reject(_blocked(
            "download_write_failed",
            provider_id=provider_id,
            download_task_id=download_task_id,
            error=f"{type(exc).__name__}: {exc}",
        ))

    if size <= 0:
        _cleanup_partial(partial, incomplete_root_path)
        return _source_content_blocked("zero_size", provider_id=provider_id, download_task_id=download_task_id)
    if declared_size is not None and size != declared_size:
        _cleanup_partial(partial, incomplete_root_path)
        return _source_content_blocked(
            "content_length_mismatch",
            provider_id=provider_id,
            download_task_id=download_task_id,
            declared_size_bytes=declared_size,
            size_bytes=size,
        )
    expected_size = _intish(expected_size_bytes)
    if expected_size is not None and expected_size > 0 and size != expected_size:
        _cleanup_partial(partial, incomplete_root_path)
        return _source_content_blocked(
            "probe_size_mismatch",
            provider_id=provider_id,
            download_task_id=download_task_id,
            expected_size_bytes=expected_size,
            size_bytes=size,
        )

    try:
        archive_reason = _archive_validation_reason(partial, extension)
    except Exception as exc:
        _cleanup_partial(partial, incomplete_root_path)
        return _blocked("archive_validation_failed", provider_id=provider_id, download_task_id=download_task_id, error=f"{type(exc).__name__}: {exc}")
    if archive_reason:
        _cleanup_partial(partial, incomplete_root_path)
        return _source_content_blocked(
            archive_reason,
            provider_id=provider_id,
            download_task_id=download_task_id,
            extension=extension,
            path=str(target),
        )

    try:
        _publish_staged_file(partial, target, root, digest.hexdigest())
    except Exception as exc:
        _cleanup_partial(partial, incomplete_root_path)
        return _blocked("staged_file_replace_failed", provider_id=provider_id, download_task_id=download_task_id, error=f"{type(exc).__name__}: {exc}")
    elapsed = time.monotonic() - started
    final_url = response.get("final_url") or url
    content_hash = "sha256:" + digest.hexdigest()
    metadata = {
        "direct_download_contract_version": CONTRACT_VERSION,
        "provider_id": provider_id,
        "download_task_id": download_task_id,
        "status": "staged_file_ready",
        "path": str(target),
        "partial_path": str(partial),
        "size_bytes": size,
        "declared_size_bytes": declared_size,
        "content_hash": content_hash,
        "url_hash": _sha256_text(url),
        "final_url_hash": _sha256_text(final_url),
        "content_type": content_type,
        "status_code": status_code,
        "elapsed_seconds": round(elapsed, 3),
        "archive_validation": "passed",
        "response_headers": _safe_response_headers(response_headers),
        "source_metadata": source_metadata if isinstance(source_metadata, dict) else {},
    }
    try:
        sidecar = _write_metadata(target, metadata)
    except Exception as exc:
        _cleanup_partial(target, root)
        _cleanup_partial(_metadata_path(target), root)
        _cleanup_partial(_metadata_path(target).with_name(_metadata_path(target).name + ".part"), root)
        return _blocked("metadata_sidecar_write_failed", provider_id=provider_id, download_task_id=download_task_id, error=f"{type(exc).__name__}: {exc}")
    return {
        "direct_download_contract_version": CONTRACT_VERSION,
        "ok": True,
        "status": "staged_file_ready",
        "state": "import_ready",
        "provider_id": provider_id,
        "download_task_id": download_task_id,
        "path": str(target),
        "local_path": str(target),
        "partial_path": str(partial),
        "metadata_path": str(sidecar),
        "size_bytes": size,
        "declared_size_bytes": declared_size,
        "content_hash": content_hash,
        "url_hash": _sha256_text(url),
        "final_url_hash": _sha256_text(final_url),
        "content_type": content_type,
        "status_code": status_code,
        "elapsed_seconds": round(elapsed, 3),
        "archive_validation": "passed",
    }


def download_direct_task(
    download_task,
    *,
    http_get=None,
    allowed_extensions=None,
    allowed_content_types=None,
    allowed_hosts=None,
    max_bytes=None,
    staging_root=None,
    download_root=None,
    incomplete_root=None,
    headers=None,
    max_redirects=DEFAULT_MAX_REDIRECTS,
    **kwargs,
):
    task = download_task if isinstance(download_task, dict) else {}
    raw = task.get("raw_json")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except ValueError:
            raw = {}
    raw = raw if isinstance(raw, dict) else {}
    nested_raw = raw.get("raw") if isinstance(raw.get("raw"), dict) else {}
    seed = nested_raw.get("download_task_seed") if isinstance(nested_raw.get("download_task_seed"), dict) else {}
    seed_raw = seed.get("raw_json") if isinstance(seed.get("raw_json"), dict) else {}
    candidate = (
        raw.get("candidate")
        if isinstance(raw.get("candidate"), dict)
        else nested_raw.get("candidate") if isinstance(nested_raw.get("candidate"), dict)
        else seed_raw.get("candidate") if isinstance(seed_raw.get("candidate"), dict)
        else {}
    )
    download_url = (
        candidate.get("download_url")
        or raw.get("download_url")
        or nested_raw.get("download_url")
        or seed.get("download_url")
        or task.get("download_url")
    )
    local_path = task.get("local_path") or task.get("download_path")
    partial_path = task.get("partial_path") or raw.get("partial_path") or nested_raw.get("partial_path") or seed.get("partial_path")
    root = staging_root or task.get("save_path") or (Path(str(local_path)).parent if local_path else "")
    resolved_download_root = download_root or root
    # incomplete_root is a trusted confinement boundary, so it must come from
    # an explicit caller (the coordinator resolves it from the provider
    # registry row -- see inkdrop_source_providers.direct_download_incomplete_
    # root) or fall back to sharing download_root, same as the pre-#281
    # single-root contract. Deriving it from the task's own stored
    # partial_path would make the confinement check meaningless: a path is
    # always "inside" its own parent, so any stored path would trivially pass.
    resolved_incomplete_root = incomplete_root or resolved_download_root
    ext = providers.normalize_extension(local_path or candidate.get("extension") or download_url)
    derived_extensions = allowed_extensions or candidate.get("allowed_extensions") or ([ext] if ext else [])
    derived_content_types = allowed_content_types or [candidate.get("content_type") or providers.content_type_for_extension(ext)]
    direct_artifact = seed_raw.get("direct_artifact") if isinstance(seed_raw.get("direct_artifact"), dict) else {}
    if not direct_artifact:
        direct_artifact = raw.get("direct_artifact") if isinstance(raw.get("direct_artifact"), dict) else {}
    route_hosts = candidate.get("transport_allowed_hosts") or direct_artifact.get("transport_allowed_hosts") or []
    derived_hosts = _effective_host_cap(allowed_hosts, route_hosts)
    route_max_value = candidate.get("max_redirects")
    if route_max_value in (None, ""):
        route_max_value = direct_artifact.get("max_redirects")
    route_max_redirects = _intish(route_max_value, None)
    derived_max_redirects = max(0, int(max_redirects or 0))
    if route_max_redirects is not None:
        derived_max_redirects = min(derived_max_redirects, max(0, route_max_redirects))
    return download_direct_file(
        url=download_url,
        provider_id=task.get("provider_id") or task.get("provider") or task.get("source"),
        download_task_id=task.get("id") or task.get("external_id") or candidate.get("candidate_identity"),
        staging_root=root,
        download_root=resolved_download_root,
        incomplete_root=resolved_incomplete_root,
        local_path=local_path,
        partial_path=partial_path,
        allowed_extensions=derived_extensions,
        allowed_content_types=derived_content_types,
        max_bytes=max_bytes,
        allowed_hosts=derived_hosts,
        max_redirects=derived_max_redirects,
        expected_size_bytes=(candidate.get("size_bytes") or task.get("size_bytes")) if candidate.get("enforce_probe_size_match") else None,
        headers=headers,
        http_get=http_get,
        source_metadata={"download_task": task, "candidate": candidate},
        **kwargs,
    )
