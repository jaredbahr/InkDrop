"""Safe HTTP client prep for settings-backed InkDrop source workers."""

from __future__ import annotations

import json
import hashlib
import re
import time
from urllib import error as urlerror
from urllib import parse, request as urlrequest


CONTRACT_VERSION = 2

SECRET_REF_PATTERN = re.compile(r"<secret_ref:([^>]+)>")

SECRET_HEADER_NAMES = {
    "authorization",
    "cookie",
    "proxy-authorization",
    "x-api-key",
    "x-auth-token",
}

DEFAULT_MAX_REDIRECTS = 5


class SourceHttpError(RuntimeError):
    def __init__(self, reason, message="", *, request=None):
        self.reason = str(reason or "source_http_error")
        self.safe_request = redacted_request(request or {})
        super().__init__(message or self.reason)


def _dict(value):
    return dict(value) if isinstance(value, dict) else {}


def _lower(value):
    return str(value or "").strip().lower()


def _bounded_timeout(value, default=15.0, maximum=60.0):
    try:
        timeout = float(value)
    except Exception:
        timeout = default
    return max(1.0, min(timeout, maximum))


def _bounded_bytes(value, default=5 * 1024 * 1024, maximum=50 * 1024 * 1024):
    try:
        size = int(value)
    except Exception:
        size = default
    return max(1024, min(size, maximum))


# Payload transfers (stream_body requests) are bounded by the direct-download
# policy ceiling, matching inkdrop_source_providers.DEFAULT_MAX_SIZE_BYTES --
# the same 2 GiB limit direct candidates are vetted against before a download
# task is ever created. The 5/50 MiB bounds above exist so parsed search and
# feed responses stay small; applying them to archive downloads silently
# failed every legitimately large book.
DEFAULT_STREAM_MAX_BYTES = 2 * 1024 * 1024 * 1024


def _bounded_stream_bytes(value, default=DEFAULT_STREAM_MAX_BYTES, maximum=DEFAULT_STREAM_MAX_BYTES):
    try:
        size = int(value)
    except Exception:
        size = default
    return max(1024, min(size, maximum))


class _LimitedStreamBody:
    """File-like response body that enforces the byte limit as the caller reads.

    Returned instead of a fully buffered payload when a request opts into
    stream_body, so the direct downloader can write chunks straight to its
    .part file: a large archive then costs one chunk of RAM instead of about
    twice the payload (chunk list plus the join in _read_limited).
    """

    def __init__(self, response, limit):
        self._response = response
        self._limit = int(limit)
        self._total = 0
        self._closed = False

    def read(self, size=65536):
        if self._closed:
            return b""
        try:
            size = int(size)
        except Exception:
            size = 65536
        if size <= 0:
            size = 65536
        chunk = self._response.read(size)
        if not chunk:
            self.close()
            return b""
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        self._total += len(chunk)
        if self._total > self._limit:
            self.close()
            raise SourceHttpError("response_too_large", f"source response exceeded {self._limit} bytes")
        return chunk

    def close(self):
        if self._closed:
            return
        self._closed = True
        close = getattr(self._response, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


def _header_items(headers):
    for key, value in _dict(headers).items():
        name = str(key or "").strip()
        if name:
            yield name, str(value or "")


def _secret_value(ref, *, secret_resolver=None, secret_values=None):
    ref = str(ref or "").strip()
    if not ref:
        return None
    if callable(secret_resolver):
        value = secret_resolver(ref)
        return str(value) if value not in (None, "") else None
    values = secret_values if isinstance(secret_values, dict) else {}
    value = values.get(ref)
    return str(value) if value not in (None, "") else None


def _replace_secret_refs(value, *, secret_resolver=None, secret_values=None, request=None):
    value = str(value or "")

    def repl(match):
        ref = match.group(1)
        secret = _secret_value(ref, secret_resolver=secret_resolver, secret_values=secret_values)
        if secret is None:
            raise SourceHttpError("unresolved_secret_ref", f"secret ref is not resolvable: {ref}", request=request)
        return secret

    return SECRET_REF_PATTERN.sub(repl, value)


def _contains_secret_ref(value):
    return bool(SECRET_REF_PATTERN.search(str(value or "")))


def redacted_headers(headers):
    out = {}
    for name, value in _header_items(headers):
        if _lower(name) in SECRET_HEADER_NAMES or _contains_secret_ref(value):
            out[name] = "<redacted>"
        else:
            out[name] = value
    return out


def redacted_request(request):
    request = _dict(request)
    params = _dict(request.get("params"))
    for key in _dict(request.get("secret_params")):
        params[str(key)] = "<redacted>"
    out = {
        "request_id": request.get("request_id"),
        "method": request.get("method") or "GET",
        "url": request.get("url"),
        "params": params,
        "headers": redacted_headers(request.get("headers")),
        "purpose": request.get("purpose"),
    }
    if any(key in request for key in ("json", "json_body", "body")):
        out["body"] = "<present>"
    return out


def resolve_request_secrets(request, *, secret_resolver=None, secret_values=None):
    request = _dict(request)
    resolved = dict(request)
    if _contains_secret_ref(request.get("url")):
        raise SourceHttpError("secret_ref_in_url", "secret refs are not allowed in source URLs", request=request)
    params = _dict(request.get("params"))
    for key, value in params.items():
        if _contains_secret_ref(value):
            raise SourceHttpError("secret_ref_in_query", f"secret refs are not allowed in query params: {key}", request=request)
    for key, value in _dict(request.get("secret_params")).items():
        key = str(key or "").strip()
        if not key:
            continue
        if key in params:
            raise SourceHttpError("secret_param_conflict", f"secret query param conflicts with regular params: {key}", request=request)
        if not _contains_secret_ref(value):
            raise SourceHttpError("secret_param_requires_secret_ref", f"secret query param must use a secret ref: {key}", request=request)
        params[key] = _replace_secret_refs(
            value,
            secret_resolver=secret_resolver,
            secret_values=secret_values,
            request=request,
        )
    headers = {}
    for name, value in _header_items(request.get("headers")):
        headers[name] = _replace_secret_refs(
            value,
            secret_resolver=secret_resolver,
            secret_values=secret_values,
            request=request,
        )
    resolved["headers"] = headers
    resolved["params"] = params
    body_value = request.get("json") if "json" in request else request.get("json_body")
    if body_value not in (None, "", [], {}):
        body_text = json.dumps(body_value, sort_keys=True, ensure_ascii=True)
        if _contains_secret_ref(body_text):
            raise SourceHttpError("secret_ref_in_body", "secret refs are not allowed in source request bodies", request=request)
    elif request.get("body") not in (None, "", b""):
        body_text = request.get("body")
        if isinstance(body_text, bytes):
            body_text = body_text.decode("utf-8", errors="replace")
        if _contains_secret_ref(body_text):
            raise SourceHttpError("secret_ref_in_body", "secret refs are not allowed in source request bodies", request=request)
    return resolved


def request_url(request):
    request = _dict(request)
    url = str(request.get("url") or "").strip()
    params = _dict(request.get("params"))
    if not params:
        return url
    query = parse.urlencode(params, doseq=True)
    separator = "&" if parse.urlsplit(url).query else "?"
    return f"{url}{separator}{query}"


def _validate_url(url, *, request=None, allowed_schemes=None, allowed_hosts=None):
    allowed_schemes = {str(value).strip().lower() for value in (allowed_schemes or ("http", "https"))}
    parts = parse.urlsplit(str(url or ""))
    if parts.scheme.lower() not in allowed_schemes:
        raise SourceHttpError("disallowed_scheme", f"source HTTP scheme is not allowed: {parts.scheme}", request=request)
    if not parts.netloc:
        raise SourceHttpError("missing_host", "source HTTP URL must include a host", request=request)
    if parts.username is not None or parts.password is not None:
        raise SourceHttpError("url_userinfo_not_allowed", "source HTTP URLs cannot contain userinfo", request=request)
    exact_hosts, suffix_hosts = _split_host_cap(allowed_hosts)
    if (exact_hosts or suffix_hosts) and parts.hostname and not _host_in_cap(parts.hostname, exact_hosts, suffix_hosts):
        raise SourceHttpError("disallowed_host", f"source HTTP host is not allowed: {parts.hostname}", request=request)
    return parts


def _split_host_cap(values):
    """Normalize a host cap into exact hostnames and domain-family suffixes.

    A '*.mangadex.network' (or '.mangadex.network') entry means any host under
    that domain. Needed because MangaDex@Home page servers use per-node
    hostnames handed out at runtime -- no static exact list can name them, and
    an entry like '*.mangadex.network' in an exact-match set silently matched
    nothing (measured live 2026-07-29: 39 mangadex page-pack tasks failed
    'request host policy has no globally allowed hosts' because the node host
    could never intersect the static global cap).
    """
    exact_hosts = set()
    suffix_hosts = set()
    for value in values or []:
        text = str(value or "").strip().lower()
        if not text:
            continue
        if "://" in text:
            text = str(parse.urlsplit(text).hostname or "").strip().lower()
        text = text.strip("[]").rstrip(".")
        if text.startswith("*."):
            text = text[1:]
        if not text or text == ".":
            continue
        if text.startswith("."):
            suffix_hosts.add(text)
        else:
            exact_hosts.add(text)
    return exact_hosts, suffix_hosts


def _host_in_cap(host, exact_hosts, suffix_hosts):
    host = str(host or "").strip().lower().rstrip(".")
    if not host:
        return False
    if host in exact_hosts:
        return True
    return any(host.endswith(suffix) for suffix in suffix_hosts)


def _effective_host_cap(global_hosts, request_hosts, *, request=None):
    """Apply a request route cap without allowing global configuration to widen it."""
    global_exact, global_suffix = _split_host_cap(global_hosts)
    request_exact, request_suffix = _split_host_cap(request_hosts)
    if not (request_exact or request_suffix):
        return sorted(global_exact | global_suffix)
    if not (global_exact or global_suffix):
        return sorted(request_exact | request_suffix)
    # Keep only request-cap entries the global cap authorizes: an exact host
    # allowed exactly or under a global family; a family only when the global
    # cap grants the same family or a wider one. A single global exact host
    # never authorizes a whole request family.
    effective = {
        host
        for host in request_exact
        if host in global_exact or any(host.endswith(suffix) for suffix in global_suffix)
    }
    effective |= {
        suffix
        for suffix in request_suffix
        if suffix in global_suffix or any(suffix.endswith(parent) for parent in global_suffix if suffix != parent)
    }
    if not effective:
        raise SourceHttpError(
            "disallowed_host",
            "source HTTP request host policy has no globally allowed hosts",
            request=request,
        )
    return sorted(effective)


def _url_hash(value):
    return "sha256:" + hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


class _ConstrainedRedirectHandler(urlrequest.HTTPRedirectHandler):
    """Validate every urllib redirect before the next request is issued."""

    def __init__(self, *, request, allowed_schemes, allowed_hosts, max_redirects):
        self.original_request = request
        self.allowed_schemes = allowed_schemes
        self.allowed_hosts = allowed_hosts
        self.max_redirects = max(0, int(max_redirects or 0))
        self.redirect_count = 0
        self.redirect_url_hashes = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        next_count = self.redirect_count + 1
        if next_count > self.max_redirects:
            raise SourceHttpError(
                "redirect_limit_exceeded",
                f"source HTTP redirect limit exceeded: {next_count}",
                request=self.original_request,
            )
        _validate_url(
            newurl,
            request=self.original_request,
            allowed_schemes=self.allowed_schemes,
            allowed_hosts=self.allowed_hosts,
        )
        self.redirect_count = next_count
        self.redirect_url_hashes.append(_url_hash(newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _read_limited(response, max_bytes, *, allow_truncated=False):
    chunks = []
    total = 0
    truncated = False
    while True:
        chunk = response.read(65536)
        if not chunk:
            break
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        if total + len(chunk) > max_bytes:
            if allow_truncated:
                remaining = max(0, max_bytes - total)
                if remaining:
                    chunks.append(chunk[:remaining])
                    total += remaining
                truncated = True
                break
            raise SourceHttpError("response_too_large", f"source response exceeded {max_bytes} bytes")
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks), truncated


def _request_bool(request, key, default=False):
    value = _dict(request).get(key)
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return bool(default)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _request_host_values(value):
    if value in (None, ""):
        return []
    values = value if isinstance(value, (list, tuple, set)) else str(value or "").split(",")
    out = []
    seen = set()
    for item in values:
        text = str(item or "").strip().lower()
        if not text:
            continue
        if "://" in text:
            text = parse.urlsplit(text).hostname or ""
        text = text.strip("[]")
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _response_headers(response):
    headers = getattr(response, "headers", None)
    if headers is None and hasattr(response, "info"):
        headers = response.info()
    if hasattr(headers, "items"):
        return {str(key): str(value) for key, value in headers.items()}
    return {}


def _status_code(response):
    if hasattr(response, "getcode"):
        code = response.getcode()
        if code is not None:
            return int(code)
    return int(getattr(response, "status", 0) or 0)


def _connected_peer_ip(response):
    for name in ("peer_ip", "connected_ip"):
        value = str(getattr(response, name, "") or "").strip()
        if value:
            return value
    candidates = [response]
    for path in (("fp",), ("fp", "raw"), ("fp", "raw", "_sock"), ("raw",), ("raw", "_connection"), ("raw", "_connection", "sock")):
        value = response
        for name in path:
            value = getattr(value, name, None)
            if value is None:
                break
        if value is not None:
            candidates.append(value)
    for candidate in candidates:
        getpeername = getattr(candidate, "getpeername", None)
        if not callable(getpeername):
            continue
        try:
            peer = getpeername()
            return str(peer[0] if isinstance(peer, (tuple, list)) else peer or "").strip()
        except Exception:
            continue
    return ""


def _decode_payload(body, headers):
    content_type = _lower(headers.get("Content-Type") or headers.get("content-type"))
    charset = "utf-8"
    if "charset=" in content_type:
        charset = content_type.split("charset=", 1)[1].split(";", 1)[0].strip() or charset
    if "json" in content_type:
        text = body.decode(charset, errors="replace")
        return {"json": json.loads(text)}
    if content_type.startswith("text/") or "xml" in content_type or "atom" in content_type:
        return {"text": body.decode(charset, errors="replace")}
    stripped = body.lstrip()
    if stripped.startswith((b"{", b"[")):
        text = body.decode(charset, errors="replace")
        try:
            return {"json": json.loads(text)}
        except ValueError:
            return {"text": text}
    return {"body": body}


def source_http_get(
    request,
    *,
    secret_resolver=None,
    secret_values=None,
    timeout_seconds=15.0,
    max_bytes=5 * 1024 * 1024,
    stream_max_bytes=None,
    allow_stream_body=False,
    allowed_schemes=None,
    allowed_hosts=None,
    allow_request_hosts=False,
    max_redirects=DEFAULT_MAX_REDIRECTS,
    opener=None,
    user_agent="InkDropSourceWorker/1",
):
    """Execute a settings-derived source request with secret redaction gates."""

    original_request = _dict(request)
    resolved = resolve_request_secrets(
        original_request,
        secret_resolver=secret_resolver,
        secret_values=secret_values,
    )
    method = str(resolved.get("method") or "GET").strip().upper()
    if method not in {"GET", "HEAD", "POST"}:
        raise SourceHttpError("unsupported_method", f"source HTTP method is not supported: {method}", request=original_request)
    url = request_url(resolved)
    request_allowed_hosts = (
        _request_host_values(original_request.get("allowed_hosts"))
        if allow_request_hosts
        else []
    )
    effective_allowed_hosts = _effective_host_cap(
        allowed_hosts,
        request_allowed_hosts,
        request=original_request,
    )
    _validate_url(url, request=original_request, allowed_schemes=allowed_schemes, allowed_hosts=effective_allowed_hosts)
    request_max_redirects = original_request.get("max_redirects")
    if request_max_redirects not in (None, ""):
        try:
            max_redirects = min(int(max_redirects), max(0, int(request_max_redirects)))
        except (TypeError, ValueError):
            raise SourceHttpError("invalid_redirect_limit", "source HTTP redirect limit is invalid", request=original_request)
    max_redirects = max(0, min(int(max_redirects or 0), 20))
    timeout = _bounded_timeout(timeout_seconds)
    stream_body = bool(allow_stream_body) and _request_bool(original_request, "stream_body", False)
    request_limit = original_request.get("max_bytes")
    if stream_body:
        limit = _bounded_stream_bytes(stream_max_bytes)
        if request_limit not in (None, ""):
            limit = min(limit, _bounded_stream_bytes(request_limit, default=limit))
    else:
        limit = _bounded_bytes(max_bytes)
        if request_limit not in (None, ""):
            limit = min(limit, _bounded_bytes(request_limit, default=limit))
    allow_truncated = _request_bool(original_request, "allow_truncated", False)
    headers = dict(resolved.get("headers") or {})
    headers.setdefault("User-Agent", user_agent)
    data = None
    if method == "POST":
        if "json" in resolved or "json_body" in resolved:
            json_payload = resolved.get("json") if "json" in resolved else resolved.get("json_body")
            data = json.dumps(json_payload if json_payload is not None else {}, ensure_ascii=True).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        elif resolved.get("body") not in (None, ""):
            body = resolved.get("body")
            data = body if isinstance(body, bytes) else str(body or "").encode("utf-8")
        else:
            data = b""
    prepared = urlrequest.Request(url, data=data, headers=headers, method=method)
    start = time.time()
    stream_handle = None
    try:
        redirect_handler = None
        if opener is None:
            redirect_handler = _ConstrainedRedirectHandler(
                request=original_request,
                allowed_schemes=allowed_schemes,
                allowed_hosts=effective_allowed_hosts,
                max_redirects=max_redirects,
            )
            opener_fn = urlrequest.build_opener(redirect_handler).open
        else:
            opener_fn = opener
        try:
            response = opener_fn(prepared, timeout=timeout)
        except TypeError:
            response = opener_fn(prepared)
        try:
            status = _status_code(response)
            response_headers = _response_headers(response)
            peer_ip = _connected_peer_ip(response)
            response_geturl = getattr(response, "geturl", None)
            final_url = str(response_geturl() if callable(response_geturl) else getattr(response, "url", "") or url).strip()
            _validate_url(
                final_url,
                request=original_request,
                allowed_schemes=allowed_schemes,
                allowed_hosts=effective_allowed_hosts,
            )
            redirect_count = int(
                redirect_handler.redirect_count
                if redirect_handler is not None
                else getattr(response, "redirect_count", 1 if final_url != url else 0)
                or 0
            )
            if redirect_count > max_redirects:
                raise SourceHttpError("redirect_limit_exceeded", "source HTTP redirect limit exceeded", request=original_request)
            redirect_url_hashes = list(
                redirect_handler.redirect_url_hashes
                if redirect_handler is not None
                else getattr(response, "redirect_url_hashes", []) or []
            )
            if method == "HEAD":
                body = b""
                truncated = False
            elif stream_body:
                # Hand the connection to the caller instead of buffering the
                # payload; the wrapper enforces the byte limit on every read
                # and closes the response at EOF or on overflow.
                body = b""
                truncated = False
                stream_handle = _LimitedStreamBody(response, limit)
            else:
                body, truncated = _read_limited(response, limit, allow_truncated=allow_truncated)
            if truncated:
                response_headers = dict(response_headers)
                response_headers["X-InkDrop-Truncated"] = "1"
        finally:
            if stream_handle is None:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
    except SourceHttpError:
        raise
    except urlerror.HTTPError as exc:
        raise SourceHttpError("http_error", f"source HTTP error {exc.code}", request=original_request) from exc
    except Exception as exc:
        raise SourceHttpError("request_failed", f"{type(exc).__name__}: {exc}", request=original_request) from exc
    elapsed_ms = int((time.time() - start) * 1000)
    if stream_handle is not None:
        decoded = {"body": stream_handle}
    else:
        decoded = _decode_payload(body, response_headers)
    return {
        "source_http_contract_version": CONTRACT_VERSION,
        "status_code": status,
        "headers": response_headers,
        "elapsed_ms": elapsed_ms,
        "url": url,
        "final_url": final_url,
        "redirect_count": redirect_count,
        "redirect_url_hashes": redirect_url_hashes,
        "peer_ip": peer_ip,
        **decoded,
    }


def make_source_http_get(**client_options):
    """Return a callable suitable for `run_source_worker_batch(..., source_http_get=...)`."""

    def _client(request):
        return source_http_get(request, **client_options)

    return _client
