#!/usr/bin/env python3
"""Read an external HTTP body under a size bound, refusing during the read.

The bound has to be enforced while the body is arriving, not after. Checking
`len(response.content)` measures an allocation that has already happened, which
leaves the limit describing the damage instead of preventing it -- the shape
`core/inkdrop_download_clients.py` shipped with until it was corrected.

Two transports are in use across the callers, so both are handled here rather
than in seven local copies:

* `requests` responses, which stream through `iter_content()` when the call
  site passes `stream=True`;
* `urllib.request.urlopen()` file objects, which stream through `read(n)`.

A transport offering neither still gets the bound enforced -- it just cannot
avoid the allocation first, because the bytes are already resident by the time
this helper sees them. That fallback is a floor, not the intended path.
"""

from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))


import json


DEFAULT_CHUNK_BYTES = 64 * 1024

# Deliberately generous per-caller defaults: these exist to stop a runaway or
# hostile endpoint from spending the process's memory, not to police ordinary
# payloads. A limit tight enough to reject real traffic would be a new failure
# mode rather than a fix.
RSS_FEED_MAX_BYTES = 16 * 1024 * 1024
SCRAPE_PAGE_MAX_BYTES = 16 * 1024 * 1024
LOCAL_CLIENT_JSON_MAX_BYTES = 64 * 1024 * 1024
NOTIFICATION_RESPONSE_MAX_BYTES = 1024 * 1024
INTERNAL_JSON_MAX_BYTES = 64 * 1024 * 1024


class ResponseTooLarge(RuntimeError):
    """An external body exceeded the bound configured for its caller."""


def _declared_length(response):
    headers = getattr(response, "headers", None)
    if headers is None:
        return 0
    getter = getattr(headers, "get", None)
    raw = getter("Content-Length") if callable(getter) else None
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def _too_large(label, max_bytes):
    return ResponseTooLarge(
        f"{label} response exceeded the {int(max_bytes)} byte bound"
    )


def _stream_chunks(response, chunk_bytes):
    """Yield body chunks from whichever streaming interface the response has.

    Returns None when the response streams neither way, so the caller can fall
    back to the already-buffered body rather than silently reading nothing --
    an empty generator here would look exactly like an empty response.
    """
    iter_content = getattr(response, "iter_content", None)
    if callable(iter_content):
        return iter_content(chunk_size=chunk_bytes)

    read = getattr(response, "read", None)
    if callable(read):

        def _read_chunks():
            while True:
                chunk = read(chunk_bytes)
                if not chunk:
                    return
                yield chunk

        return _read_chunks()

    return None


def bounded_read_bytes(response, max_bytes, *, label="external", chunk_bytes=DEFAULT_CHUNK_BYTES):
    """Return at most `max_bytes` of the body, raising as soon as it is crossed.

    An honestly-declared oversize `Content-Length` is refused before a single
    byte is pulled. A lie there changes nothing: the streaming accumulation is
    what actually enforces the bound.
    """
    max_bytes = int(max_bytes)
    chunk_bytes = max(1, int(chunk_bytes))
    if _declared_length(response) > max_bytes:
        raise _too_large(label, max_bytes)

    chunks = _stream_chunks(response, chunk_bytes)
    if chunks is None:
        buffered = bytes(getattr(response, "content", b"") or b"")
        if len(buffered) > max_bytes:
            raise _too_large(label, max_bytes)
        return buffered

    body = bytearray()
    for chunk in chunks:
        if not chunk:
            continue
        body.extend(chunk)
        if len(body) > max_bytes:
            raise _too_large(label, max_bytes)
    return bytes(body)


def _response_encoding(response, fallback="utf-8"):
    # Deliberately NOT `requests`' `apparent_encoding`: it is a property that
    # runs chardet over `response.content`, and on a streamed response whose
    # body this module has already consumed that raises StreamConsumedError.
    # Reaching for it would break exactly the callers that stream, which is
    # all of them.
    declared = getattr(response, "encoding", None)
    if declared:
        return str(declared)
    headers = getattr(response, "headers", None)
    getter = getattr(headers, "get", None) if headers is not None else None
    content_type = getter("Content-Type") if callable(getter) else None
    for part in str(content_type or "").split(";"):
        part = part.strip()
        if part.lower().startswith("charset="):
            charset = part.split("=", 1)[1].strip().strip('"\'')
            if charset:
                return charset
    return fallback


def bounded_read_text(
    response,
    max_bytes,
    *,
    label="external",
    chunk_bytes=DEFAULT_CHUNK_BYTES,
    encoding=None,
    errors="replace",
):
    """Bounded read decoded to text.

    Callers that reach for `response.text` on a streamed response would pull the
    whole body back down uncapped, so they take this instead and reuse the one
    string it returns.
    """
    payload = bounded_read_bytes(
        response, max_bytes, label=label, chunk_bytes=chunk_bytes
    )
    return payload.decode(encoding or _response_encoding(response), errors=errors)


def bounded_read_json(
    response,
    max_bytes,
    *,
    label="external",
    chunk_bytes=DEFAULT_CHUNK_BYTES,
    default=None,
):
    """Bounded read parsed as JSON, with `default` for an empty body."""
    text = bounded_read_text(
        response, max_bytes, label=label, chunk_bytes=chunk_bytes
    ).strip()
    if not text:
        return default
    return json.loads(text)
