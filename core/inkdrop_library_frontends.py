#!/usr/bin/env python3
"""Library frontend sync helpers for InkDrop imports.

Folder completion is InkDrop's durable truth. Frontends such as Kavita and Komga
are optional scan/visibility adapters that can be asked to catch up afterwards.
"""

import inspect
import re
import threading
import time
from pathlib import Path
from urllib.parse import quote

try:
    import requests as _requests
except Exception:
    _requests = None


def normalized_folder_key(value):
    return str(value or "").replace("\\", "/").rstrip("/").lower()


def unique_folders(folders):
    result = []
    seen = set()
    for folder in folders or []:
        text = str(folder or "").strip()
        if not text:
            continue
        key = normalized_folder_key(text)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return sorted(result)


def _log(log_event, payload):
    if not callable(log_event):
        return
    try:
        log_event(payload)
    except Exception:
        pass


_ERROR_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:api[_-]?key|apikey|access[_-]?token|token|password|passwd|secret)=)[^&\s]+"
)
_ERROR_NAMED_SECRET_RE = re.compile(
    r"(?i)(\b(?:api[_-]?key|apikey|access[_-]?token|token|password|passwd|secret|authorization|x-api-key)\b"
    r"[\"']?\s*[:=]\s*[\"']?)(?:bearer\s+)?[^\s,;\"'}]+"
)
_ERROR_URL_USERINFO_RE = re.compile(r"(?i)(https?://)[^/@\s]+@")
_KOMGA_FAILURE_BACKOFF = {}
_KOMGA_FAILURE_BACKOFF_LOCK = threading.Lock()


def safe_error_text(exc, secrets=(), limit=1000):
    """Return bounded diagnostic text with common credential forms removed."""
    text = str(exc or "")
    for secret in secrets or ():
        secret = str(secret or "")
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = _ERROR_URL_USERINFO_RE.sub(r"\1[REDACTED]@", text)
    text = _ERROR_QUERY_SECRET_RE.sub(r"\1[REDACTED]", text)
    text = _ERROR_NAMED_SECRET_RE.sub(r"\1[REDACTED]", text)
    limit = max(80, int(limit or 1000))
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _komga_backoff_key(settings):
    return str((settings or {}).get("base_url") or "").strip().rstrip("/").casefold()


def _komga_failure_backoff_seconds(settings):
    try:
        value = int((settings or {}).get("failure_backoff_seconds") or 30)
    except (TypeError, ValueError):
        value = 30
    return max(5, min(value, 300))


def _komga_failure_backoff_status(settings):
    key = _komga_backoff_key(settings)
    if not key:
        return None
    now = time.monotonic()
    with _KOMGA_FAILURE_BACKOFF_LOCK:
        until = float(_KOMGA_FAILURE_BACKOFF.get(key) or 0)
        if until <= now:
            _KOMGA_FAILURE_BACKOFF.pop(key, None)
            return None
    return {
        "reason": "komga_temporarily_unavailable",
        "retry_after_seconds": max(1, int(until - now + 0.999)),
    }


def _remember_komga_failure(settings):
    key = _komga_backoff_key(settings)
    if not key:
        return
    with _KOMGA_FAILURE_BACKOFF_LOCK:
        _KOMGA_FAILURE_BACKOFF[key] = time.monotonic() + _komga_failure_backoff_seconds(settings)


def _clear_komga_failure(settings):
    key = _komga_backoff_key(settings)
    if not key:
        return
    with _KOMGA_FAILURE_BACKOFF_LOCK:
        _KOMGA_FAILURE_BACKOFF.pop(key, None)


def _komga_endpoint_unavailable(exc):
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code is None:
        return True
    try:
        status_code = int(status_code)
    except (TypeError, ValueError):
        return True
    return status_code in {401, 403, 408, 429} or status_code >= 500


def _call_kavita_scan(trigger, folder, force_library_scan):
    if not callable(trigger):
        raise RuntimeError("Kavita scan adapter is unavailable")
    try:
        signature = inspect.signature(trigger)
    except (TypeError, ValueError):
        signature = None
    if signature and "force_library_scan" in signature.parameters:
        return trigger(folder, force_library_scan=bool(force_library_scan))
    return trigger(folder)


def _load_komga_settings(loader):
    if not callable(loader):
        return {"enabled": False, "reason": "komga_settings_loader_unavailable"}
    settings = loader()
    return settings if isinstance(settings, dict) else {"enabled": False, "reason": "komga_settings_not_dict"}


def _task_requested(task):
    return isinstance(task, dict) and not task.get("skipped") and not task.get("error")


def _normalize_visibility(provider, payload):
    if isinstance(payload, dict):
        result = dict(payload)
    else:
        result = {"visible": bool(payload)}
    result["provider"] = provider
    result["visible"] = bool(result.get("visible") or result.get("library_visible"))
    if not result.get("status"):
        result["status"] = "library_visible" if result["visible"] else "not_visible"
    return result


def _visibility_error(provider, exc):
    return {
        "provider": provider,
        "visible": False,
        "status": "error",
        "error": f"{type(exc).__name__}: {safe_error_text(exc)}",
    }


def http_requests():
    global _requests
    if _requests is None:
        import requests as requests_module

        _requests = requests_module
    return _requests


def normalized_adapter_path(value):
    return str(value or "").replace("\\", "/").rstrip("/")


def host_path_to_frontend_path(
    path,
    *,
    comic_root,
    manga_root,
    frontend_comic_root,
    frontend_manga_root,
):
    path = Path(path)
    try:
        rel = path.relative_to(Path(manga_root))
        return f"{str(frontend_manga_root).rstrip('/')}/{rel.as_posix()}"
    except ValueError:
        rel = path.relative_to(Path(comic_root))
        return f"{str(frontend_comic_root).rstrip('/')}/{rel.as_posix()}"


def library_ids_for_host_folder(host_folder, settings, *, comic_root, manga_root):
    settings = settings if isinstance(settings, dict) else {}
    path = Path(host_folder)
    try:
        path.relative_to(Path(manga_root))
        return settings.get("manga_library_ids") or [], "manga"
    except ValueError:
        pass
    try:
        path.relative_to(Path(comic_root))
        return settings.get("comic_library_ids") or [], "comics"
    except ValueError:
        return [], "unknown"


def komga_list_libraries(settings):
    """Ask Komga's own /libraries for the libraries it actually has, id/name/root.

    Kavita never needs operator-entered library ids because InkDrop reads Kavita's
    own database directly. Komga has no local database to read, but it does expose
    its library roots over the API -- this is the live source of truth that lets
    Komga match that behavior instead of requiring hand-typed library UUIDs.
    """
    settings = settings if isinstance(settings, dict) else {}
    if not settings.get("base_url") or not settings.get("username") or not settings.get("password"):
        return {"ok": False, "reason": "komga_credentials_missing", "libraries": []}
    backoff = _komga_failure_backoff_status(settings)
    if backoff:
        return {"ok": False, "libraries": [], **backoff}
    try:
        response = http_requests().get(
            settings["base_url"].rstrip("/") + "/libraries",
            auth=(settings["username"], settings["password"]),
            timeout=settings.get("timeout_seconds") or 8,
        )
        response.raise_for_status()
    except Exception as exc:
        if _komga_endpoint_unavailable(exc):
            _remember_komga_failure(settings)
        return {
            "ok": False,
            "reason": "komga_libraries_request_failed",
            "error": safe_error_text(exc, (settings.get("username"), settings.get("password"))),
            "libraries": [],
        }
    _clear_komga_failure(settings)
    try:
        payload = response.json()
    except ValueError:
        payload = []
    libraries = []
    for entry in payload if isinstance(payload, list) else []:
        if not isinstance(entry, dict):
            continue
        library_id = str(entry.get("id") or "").strip()
        if not library_id:
            continue
        libraries.append(
            {
                "id": library_id,
                "name": str(entry.get("name") or "").strip(),
                "root": normalized_adapter_path(entry.get("root")),
            }
        )
    return {"ok": True, "libraries": libraries}


def komga_candidate_library_ids_for_host_folder(host_folder, settings, *, comic_root, manga_root):
    """Live-discovery fallback for library_ids_for_host_folder.

    Translates host_folder to the path Komga would see, then asks Komga's own
    /libraries which library roots contain it -- the Komga analogue of Kavita's
    live FolderPath-table lookup. Used only when comic_library_ids/manga_library_ids
    are left unconfigured; explicit operator settings always take precedence.
    """
    settings = settings if isinstance(settings, dict) else {}
    try:
        komga_folder = host_path_to_frontend_path(
            host_folder,
            comic_root=comic_root,
            manga_root=manga_root,
            frontend_comic_root=settings.get("komga_comic_root", "/data/comics"),
            frontend_manga_root=settings.get("komga_manga_root", "/data/manga"),
        )
    except ValueError:
        return []
    listing = komga_list_libraries(settings)
    if not listing.get("ok"):
        return []
    folder_key = normalized_adapter_path(komga_folder).lower()
    matches = []
    for library in listing.get("libraries") or []:
        root = str(library.get("root") or "").lower()
        if not root:
            continue
        if folder_key == root or folder_key.startswith(root + "/"):
            matches.append(library["id"])
    return matches


def trigger_komga_empty_trash(host_folder, *, settings, comic_root, manga_root):
    """Drop Komga's record of files that are gone from disk.

    Komga soft-deletes: a book whose file disappears stays in the database until
    the library's trash is emptied. That is normally harmless, but it breaks a
    cover injection that changes a file's extension -- rewriting `v01.cbr` as
    `v01.cbz` leaves two books with the same name, and Komga picked the stale
    `.cbr` as the series' first book and kept serving its old cover. Measured:
    the covers were still wrong after a scan and correct immediately after this
    call. Kavita has no equivalent problem.
    """
    settings = settings if isinstance(settings, dict) else {}
    result = {"provider": "komga", "folder": str(host_folder), "mode": "empty_trash", "skipped": False}
    if not settings.get("enabled"):
        result.update({"skipped": True, "reason": "komga_disabled"})
        return result
    if not settings.get("username") or not settings.get("password"):
        result.update({"skipped": True, "reason": "komga_credentials_missing"})
        return result
    library_ids, media_type = library_ids_for_host_folder(
        host_folder, settings, comic_root=comic_root, manga_root=manga_root
    )
    result["media_type"] = media_type
    if not library_ids:
        library_ids = komga_candidate_library_ids_for_host_folder(
            host_folder, settings, comic_root=comic_root, manga_root=manga_root
        )
    if not library_ids:
        result.update({"skipped": True, "reason": f"komga_{media_type}_library_ids_missing"})
        return result
    statuses, errors = [], []
    for library_id in library_ids:
        library_id = str(library_id).strip()
        if not library_id:
            continue
        try:
            response = http_requests().post(
                settings["base_url"].rstrip("/") + f"/libraries/{quote(library_id, safe='')}/empty-trash",
                auth=(settings["username"], settings["password"]),
                timeout=settings.get("timeout_seconds") or 8,
            )
            if response.status_code in {200, 202, 204}:
                statuses.append({"library_id": library_id, "status_code": response.status_code})
            else:
                errors.append({"library_id": library_id, "status_code": response.status_code})
        except Exception as exc:
            errors.append({
                "library_id": library_id,
                "error": safe_error_text(exc, (settings.get("username"), settings.get("password"))),
            })
    result["statuses"] = statuses
    if errors:
        result["errors"] = errors[:3]
    return result


def trigger_komga_scan_folder(host_folder, *, settings, comic_root, manga_root):
    settings = settings if isinstance(settings, dict) else {}
    result = {
        "provider": "komga",
        "folder": str(host_folder),
        "mode": "library_scan",
        "skipped": False,
    }
    if not settings.get("enabled"):
        result.update({"skipped": True, "reason": "komga_disabled"})
        return result
    if not settings.get("add_after_import", settings.get("scan_after_import", True)):
        result.update({"skipped": True, "reason": "komga_add_after_import_disabled", "legacy_reason": "komga_scan_after_import_disabled"})
        return result
    if not settings.get("username") or not settings.get("password"):
        result.update({"skipped": True, "reason": "komga_credentials_missing"})
        return result
    library_ids, media_type = library_ids_for_host_folder(host_folder, settings, comic_root=comic_root, manga_root=manga_root)
    result["media_type"] = media_type
    if media_type == "unknown":
        result.update({"skipped": True, "reason": "komga_folder_outside_managed_roots"})
        return result
    result["komga_folder"] = host_path_to_frontend_path(
        host_folder,
        comic_root=comic_root,
        manga_root=manga_root,
        frontend_comic_root=settings.get("komga_comic_root", "/data/comics"),
        frontend_manga_root=settings.get("komga_manga_root", "/data/manga"),
    )
    if not library_ids:
        library_ids = komga_candidate_library_ids_for_host_folder(
            host_folder, settings, comic_root=comic_root, manga_root=manga_root
        )
        if library_ids:
            result["library_ids_source"] = "live_discovery"
    if not library_ids:
        result.update({"skipped": True, "reason": f"komga_{media_type}_library_ids_missing"})
        return result
    backoff = _komga_failure_backoff_status(settings)
    if backoff:
        result.update({"skipped": True, **backoff})
        return result
    statuses = []
    errors = []
    for library_id in library_ids:
        library_id = str(library_id).strip()
        if not library_id:
            continue
        try:
            response = http_requests().post(
                settings["base_url"].rstrip("/") + f"/libraries/{quote(library_id, safe='')}/scan",
                auth=(settings["username"], settings["password"]),
                timeout=settings.get("timeout_seconds") or 8,
            )
            if response.status_code in {200, 202, 204}:
                statuses.append({"library_id": library_id, "status_code": response.status_code})
            else:
                response.raise_for_status()
        except Exception as exc:
            errors.append(
                {
                    "library_id": library_id,
                    "error": safe_error_text(
                        exc,
                        (settings.get("username"), settings.get("password")),
                    ),
                }
            )
            if _komga_endpoint_unavailable(exc):
                _remember_komga_failure(settings)
                break
    result["libraries"] = statuses
    if errors:
        result["errors"] = errors
    result["status_code"] = statuses[0]["status_code"] if statuses else None
    if statuses:
        _clear_komga_failure(settings)
    if not statuses and errors:
        raise RuntimeError(f"Komga scan failed for all configured {media_type} libraries: {errors[:3]}")
    return result


def komga_paginated_content(payload):
    if isinstance(payload, dict) and isinstance(payload.get("content"), list):
        return payload.get("content") or []
    if isinstance(payload, list):
        return payload
    return []


def komga_book_path_values(book):
    values = []

    def add(value):
        text = str(value or "").strip()
        if text and text not in values:
            values.append(text)

    if not isinstance(book, dict):
        return values
    for key in ("url", "filePath", "file_path", "path", "name"):
        add(book.get(key))
    media = book.get("media") if isinstance(book.get("media"), dict) else {}
    for key in ("url", "filePath", "file_path", "path", "fileName", "filename"):
        add(media.get(key))
    metadata = book.get("metadata") if isinstance(book.get("metadata"), dict) else {}
    add(metadata.get("title"))
    return values


def komga_book_matches_path(book, host_path, komga_path):
    filename = Path(host_path).name
    stem = Path(host_path).stem
    wanted_paths = {
        normalized_adapter_path(host_path).lower(),
        normalized_adapter_path(komga_path).lower(),
    }
    for value in komga_book_path_values(book):
        normalized = normalized_adapter_path(value)
        lowered = normalized.lower()
        if lowered in wanted_paths:
            return True, "exact_path", value
        if Path(normalized).name.lower() == filename.lower():
            return True, "filename", value
        if Path(normalized).stem.lower() == stem.lower() and Path(normalized).suffix.lower() == Path(filename).suffix.lower():
            return True, "filename_stem_suffix", value
    return False, "", ""


def komga_book_number_values(book):
    """Every number-ish field Komga's book payload might carry for the issue/chapter/volume number."""
    values = []
    if not isinstance(book, dict):
        return values
    metadata = book.get("metadata") if isinstance(book.get("metadata"), dict) else {}
    for value in (metadata.get("number"), metadata.get("numberSort"), book.get("number")):
        if value not in (None, ""):
            values.append(value)
    return values


def _komga_numeric_tokens(value):
    return {token.lstrip("0") or "0" for token in re.findall(r"\d+(?:\.\d+)?", str(value or ""))}


def komga_book_matches_unit(book, expected_number):
    """True when expected_number can't be checked (nothing to compare) or a book number field matches it."""
    expected_number = str(expected_number or "").strip()
    if not expected_number:
        return True
    expected_tokens = _komga_numeric_tokens(expected_number)
    if not expected_tokens:
        return True
    for value in komga_book_number_values(book):
        if expected_tokens & _komga_numeric_tokens(value):
            return True
    return False


def komga_book_matches_series_folder(matched_value, expected_folder):
    """True when there's nothing to compare, or the matched value's own folder agrees with expected_folder.

    Only path-shaped matched values (containing a '/') carry a folder to compare; a
    title-metadata match has no folder of its own and can't disagree, so it passes.
    """
    expected_folder = str(expected_folder or "").strip()
    if not expected_folder:
        return True
    normalized = normalized_adapter_path(matched_value)
    if "/" not in normalized:
        return True
    return Path(normalized).parent.name.casefold() == expected_folder.casefold()


def komga_search_books(settings, query, library_ids, limit=25):
    settings = settings if isinstance(settings, dict) else {}
    query = str(query or "").strip()
    if not query:
        return {"ok": False, "reason": "empty_query", "books": []}
    params = [("search", query), ("size", str(max(1, min(int(limit or 25), 100))))]
    for library_id in library_ids or []:
        params.append(("library_id", str(library_id)))
    url = settings["base_url"].rstrip("/") + "/books"
    response = http_requests().get(
        url,
        params=params,
        auth=(settings["username"], settings["password"]),
        timeout=settings.get("timeout_seconds") or 8,
    )
    if response.status_code in {404, 405}:
        body = {"search": query, "libraryIds": [str(item) for item in library_ids or [] if str(item).strip()]}
        response = http_requests().post(
            settings["base_url"].rstrip("/") + "/books/list",
            params={"size": str(max(1, min(int(limit or 25), 100)))},
            json=body,
            auth=(settings["username"], settings["password"]),
            timeout=settings.get("timeout_seconds") or 8,
        )
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    books = komga_paginated_content(payload)
    if not books:
        fallback = komga_list_books_page(settings, library_ids, page=0, limit=limit)
        fallback["query"] = query
        fallback["fallback_reason"] = "search_returned_no_books"
        return fallback
    return {"ok": True, "books": books, "query": query}


def komga_list_books_page(settings, library_ids, page=0, limit=100):
    settings = settings if isinstance(settings, dict) else {}
    limit = max(1, min(int(limit or 100), 100))
    params = [("size", str(limit)), ("page", str(max(0, int(page or 0))))]
    for library_id in library_ids or []:
        params.append(("library_id", str(library_id)))
    body = {"libraryIds": [str(item) for item in library_ids or [] if str(item).strip()]}
    response = http_requests().post(
        settings["base_url"].rstrip("/") + "/books/list",
        params=params,
        json=body,
        auth=(settings["username"], settings["password"]),
        timeout=settings.get("timeout_seconds") or 8,
    )
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    return {
        "ok": True,
        "books": komga_paginated_content(payload),
        "page": payload.get("number") if isinstance(payload, dict) else page,
        "total_pages": payload.get("totalPages") if isinstance(payload, dict) else None,
        "total_elements": payload.get("totalElements") if isinstance(payload, dict) else None,
    }


def komga_file_visible_for_host_path(host_path, *, settings, comic_root, manga_root, expectation=None):
    settings = settings if isinstance(settings, dict) else {}
    expectation = expectation if isinstance(expectation, dict) else {}
    result = {
        "provider": "komga",
        "visible": False,
        "status": "not_checked",
        "path": str(host_path),
        "queries": [],
        "candidate_count": 0,
    }
    if not settings.get("enabled"):
        result.update({"status": "disabled", "reason": "komga_disabled"})
        return result
    if not settings.get("username") or not settings.get("password"):
        result.update({"status": "skipped", "reason": "komga_credentials_missing"})
        return result
    host_path = Path(host_path)
    library_ids, media_type = library_ids_for_host_folder(host_path.parent, settings, comic_root=comic_root, manga_root=manga_root)
    result["media_type"] = media_type
    if media_type == "unknown":
        result.update({"status": "skipped", "reason": "komga_file_outside_managed_roots"})
        return result
    expected_library_type = str(expectation.get("library_type") or "").strip().lower()
    if expected_library_type:
        actual_library_type = "manga" if media_type == "manga" else "comics" if media_type == "comics" else media_type
        if expected_library_type != actual_library_type:
            result.update({"status": "wrong_library", "reason": "komga_library_identity_mismatch"})
            return result
    expected_unit_type = str(expectation.get("unit_type") or "").strip().lower()
    expected_unit_number = str(expectation.get("unit_number") or "").strip()
    expected_folder = str(expectation.get("series_folder") or "").strip()
    # Komga has no InkDrop-side series/library binding the way Kavita does (no local
    # DB to join against), so unlike Kavita's expectation gate this never hard-fails
    # on missing identity fields -- it only rejects a path/filename match when the
    # matched book's own number or folder actively disagrees with what was expected.
    unit_check_applicable = bool(expected_unit_type in {"volume", "chapter", "issue"} and expected_unit_number)
    if not library_ids:
        library_ids = komga_candidate_library_ids_for_host_folder(
            host_path.parent, settings, comic_root=comic_root, manga_root=manga_root
        )
        if library_ids:
            result["library_ids_source"] = "live_discovery"
    if not library_ids:
        result.update({"status": "skipped", "reason": f"komga_{media_type}_library_ids_missing"})
        return result
    try:
        komga_path = host_path_to_frontend_path(
            host_path,
            comic_root=comic_root,
            manga_root=manga_root,
            frontend_comic_root=settings.get("komga_comic_root", "/data/comics"),
            frontend_manga_root=settings.get("komga_manga_root", "/data/manga"),
        )
    except ValueError:
        result.update({"status": "skipped", "reason": "komga_path_mapping_failed"})
        return result
    result["komga_path"] = komga_path
    backoff = _komga_failure_backoff_status(settings)
    if backoff:
        result.update({"status": "error", **backoff})
        return result
    queries = [host_path.name]
    if host_path.stem and host_path.stem not in queries:
        queries.append(host_path.stem)
    errors = []
    best_mismatch = None

    def identity_mismatch(book, matched_value):
        if unit_check_applicable and not komga_book_matches_unit(book, expected_unit_number):
            return "wrong_unit", "komga_unit_identity_mismatch"
        if expected_folder and not komga_book_matches_series_folder(matched_value, expected_folder):
            return "wrong_series_folder", "komga_series_folder_identity_mismatch"
        return None

    for query in queries:
        result["queries"].append(query)
        try:
            search = komga_search_books(settings, query, library_ids)
        except Exception as exc:
            errors.append(
                {
                    "query": query,
                    "error": safe_error_text(
                        exc,
                        (settings.get("username"), settings.get("password")),
                    ),
                }
            )
            if _komga_endpoint_unavailable(exc):
                _remember_komga_failure(settings)
                result.update(
                    {
                        "errors": errors[:5],
                        "status": "error",
                        "reason": "komga_temporarily_unavailable",
                        "retry_after_seconds": _komga_failure_backoff_seconds(settings),
                    }
                )
            else:
                result.update(
                    {
                        "errors": errors[:5],
                        "status": "error",
                        "reason": "komga_visibility_search_failed",
                    }
                )
            return result
        _clear_komga_failure(settings)
        books = search.get("books") or []
        result["candidate_count"] += len(books)
        for book in books:
            matched, reason, matched_value = komga_book_matches_path(book, host_path, komga_path)
            if not matched:
                continue
            mismatch = identity_mismatch(book, matched_value)
            if mismatch:
                if best_mismatch is None:
                    status, mismatch_reason = mismatch
                    best_mismatch = {
                        "status": status,
                        "reason": mismatch_reason,
                        "match_reason": reason,
                        "book_id": book.get("id") if isinstance(book, dict) else None,
                        "book_name": book.get("name") if isinstance(book, dict) else None,
                        "matched_path_values": komga_book_path_values(book)[:5],
                    }
                continue
            result.update(
                {
                    "visible": True,
                    "status": "library_visible",
                    "match_reason": reason,
                    "book_id": book.get("id") if isinstance(book, dict) else None,
                    "book_name": book.get("name") if isinstance(book, dict) else None,
                    "matched_path_values": komga_book_path_values(book)[:5],
                }
            )
            return result
    max_pages = max(1, min(int(settings.get("visibility_scan_page_limit") or 50), 100))
    result["path_scan_pages_checked"] = 0
    for page in range(max_pages):
        try:
            listing = komga_list_books_page(settings, library_ids, page=page, limit=100)
        except Exception as exc:
            errors.append(
                {
                    "query": "path_scan",
                    "page": page,
                    "error": safe_error_text(
                        exc,
                        (settings.get("username"), settings.get("password")),
                    ),
                }
            )
            if _komga_endpoint_unavailable(exc):
                _remember_komga_failure(settings)
            break
        _clear_komga_failure(settings)
        books = listing.get("books") or []
        result["path_scan_pages_checked"] += 1
        result["candidate_count"] += len(books)
        for book in books:
            matched, reason, matched_value = komga_book_matches_path(book, host_path, komga_path)
            if not matched:
                continue
            mismatch = identity_mismatch(book, matched_value)
            if mismatch:
                if best_mismatch is None:
                    status, mismatch_reason = mismatch
                    best_mismatch = {
                        "status": status,
                        "reason": mismatch_reason,
                        "match_reason": reason,
                        "book_id": book.get("id") if isinstance(book, dict) else None,
                        "book_name": book.get("name") if isinstance(book, dict) else None,
                        "matched_path_values": komga_book_path_values(book)[:5],
                        "path_scan_match": True,
                        "path_scan_page": page,
                    }
                continue
            result.update(
                {
                    "visible": True,
                    "status": "library_visible",
                    "match_reason": reason,
                    "book_id": book.get("id") if isinstance(book, dict) else None,
                    "book_name": book.get("name") if isinstance(book, dict) else None,
                    "matched_path_values": komga_book_path_values(book)[:5],
                    "path_scan_match": True,
                    "path_scan_page": page,
                }
            )
            return result
        total_pages = listing.get("total_pages")
        if total_pages is not None and page + 1 >= int(total_pages or 0):
            break
        if not books:
            break
    if errors:
        result["errors"] = errors[:5]
        result["status"] = "error"
        backoff = _komga_failure_backoff_status(settings)
        if backoff:
            result.update(backoff)
        else:
            result["reason"] = "komga_visibility_search_failed"
    elif best_mismatch is not None:
        result.update(best_mismatch)
    else:
        result["status"] = "not_visible"
        result["reason"] = "komga_no_matching_book"
    return result


def kavita_file_visible_for_host_path(
    host_path, *, conn, comic_root, manga_root, kavita_comic_root, kavita_manga_root, expectation=None
):
    result = {
        "provider": "kavita",
        "visible": False,
        "status": "not_checked",
        "path": str(host_path),
    }
    if conn is None:
        result.update({"status": "disabled", "reason": "kavita_visibility_adapter_disabled"})
        return result
    expectation = expectation if isinstance(expectation, dict) else {}
    actual_library_type = "unknown"
    try:
        Path(host_path).relative_to(Path(manga_root))
        actual_library_type = "manga"
    except ValueError:
        try:
            Path(host_path).relative_to(Path(comic_root))
            actual_library_type = "comics"
        except ValueError:
            pass
    result["library_type"] = actual_library_type
    expected_library_type = str(expectation.get("library_type") or "").strip().lower()
    if expected_library_type and expected_library_type != actual_library_type:
        result.update({"status": "wrong_library", "reason": "kavita_library_identity_mismatch"})
        return result
    if expectation:
        expected_filename = str(expectation.get("filename") or "").strip()
        expected_folder = str(expectation.get("series_folder") or "").strip()
        if (
            not expectation.get("work_id") or not expectation.get("reader_series_id")
            or not expectation.get("reader_library_id") or not expectation.get("unit_type")
            or not expectation.get("unit_number")
        ):
            result.update({"status": "invalid_expectation", "reason": "canonical_reader_identity_incomplete"})
            return result
        unit_type = str(expectation.get("unit_type") or "").strip().lower()
        if unit_type not in {"volume", "chapter", "issue"}:
            result.update({"status": "unsupported_unit", "reason": "kavita_unit_semantics_not_authoritative"})
            return result
        if expected_filename and Path(host_path).name.casefold() != expected_filename.casefold():
            result.update({"status": "wrong_file", "reason": "kavita_filename_identity_mismatch"})
            return result
        if expected_folder and Path(host_path).parent.name.casefold() != expected_folder.casefold():
            result.update({"status": "wrong_series_folder", "reason": "kavita_series_folder_identity_mismatch"})
            return result
    try:
        kavita_path = host_path_to_frontend_path(
            host_path,
            comic_root=comic_root,
            manga_root=manga_root,
            frontend_comic_root=kavita_comic_root,
            frontend_manga_root=kavita_manga_root,
        )
    except ValueError:
        result.update({"status": "outside_configured_roots", "reason": "kavita_path_mapping_failed"})
        return result
    result["kavita_path"] = kavita_path
    if expectation:
        try:
            row = conn.execute(
                """
                select s.Id,s.LibraryId,v.Number,c.Number,c.Range
                from MangaFile mf
                join ChapterFile cf on cf.MangaFileId=mf.Id
                join Chapter c on c.Id=cf.ChapterId
                join Volume v on v.Id=c.VolumeId
                join Series s on s.Id=v.SeriesId
                where mf.FilePath=? and s.Id=? and s.LibraryId=?
                limit 1
                """,
                (
                    kavita_path,
                    int(expectation["reader_series_id"]),
                    int(expectation["reader_library_id"]),
                ),
            ).fetchone()
        except Exception as exc:
            result.update({
                "status": "association_error",
                "reason": "kavita_association_query_failed",
                "error_type": type(exc).__name__,
            })
            return result
        if row:
            association = list(row)
            values = association[2:3] if unit_type == "volume" else association[3:5]
            expected_number = str(expectation.get("unit_number") or "").lstrip("0") or "0"
            unit_match = any(
                expected_number in {
                    token.lstrip("0") or "0"
                    for token in re.findall(r"\d+(?:\.\d+)?", str(value or ""))
                }
                for value in values
            )
            if not unit_match:
                result.update({"status": "wrong_unit", "reason": "kavita_unit_identity_mismatch"})
                return result
    else:
        row = conn.execute(
            "select 1 from MangaFile where FilePath = ? limit 1",
            (kavita_path,),
        ).fetchone()
    result["visible"] = bool(row)
    result["status"] = "library_visible" if result["visible"] else "not_visible"
    if result["visible"] and expectation:
        result["matched_expectation"] = True
        result["matched_work_id"] = expectation.get("work_id")
        result["matched_unit_type"] = expectation.get("unit_type")
        result["matched_unit_number"] = expectation.get("unit_number")
    return result


def kavita_plugin_token(settings):
    settings = settings if isinstance(settings, dict) else {}
    try:
        response = http_requests().post(
            settings["base_url"].rstrip("/") + "/Plugin/authenticate",
            params={"apiKey": settings["api_key"], "pluginName": settings["plugin_name"]},
            timeout=30,
        )
        response.raise_for_status()
    except Exception as exc:
        raise RuntimeError(
            "Kavita plugin authentication failed: "
            + safe_error_text(exc, (settings.get("api_key"),))
        ) from None
    token = response.json().get("token")
    if not token:
        raise RuntimeError("Kavita plugin authentication did not return a token")
    return token


def kavita_series_ids_for_folder(folder, rows):
    folder = str(folder).rstrip("/")
    matches = []
    for row in rows or []:
        try:
            series_id, folder_path, lowest_folder_path = row
        except Exception:
            if isinstance(row, dict):
                series_id = row.get("Id") or row.get("id")
                folder_path = row.get("FolderPath") or row.get("folder_path")
                lowest_folder_path = row.get("LowestFolderPath") or row.get("lowest_folder_path")
            else:
                continue
        candidates = {str(folder_path or "").rstrip("/"), str(lowest_folder_path or "").rstrip("/")}
        if folder in candidates or any(
            candidate
            and (
                candidate.startswith(folder + "/")
                or folder.startswith(candidate + "/")
            )
            for candidate in candidates
        ):
            matches.append(int(series_id))
    return sorted(set(matches))


def kavita_library_id_for_folder(folder, rows):
    folder = str(folder).rstrip("/")
    best = None
    for row in rows or []:
        try:
            library_id, root = row
        except Exception:
            if isinstance(row, dict):
                library_id = row.get("LibraryId") or row.get("library_id")
                root = row.get("Path") or row.get("path")
            else:
                continue
        root = str(root or "").rstrip("/")
        if root and (folder == root or folder.startswith(root + "/")):
            if best is None or len(root) > len(best[1]):
                best = (int(library_id), root)
    return best[0] if best else None


def trigger_kavita_library_scan(
    folder,
    *,
    settings,
    library_id_for_folder,
    mode,
    folder_scan_status_code=None,
    details=None,
):
    if not callable(library_id_for_folder):
        raise RuntimeError("Kavita library id adapter is unavailable")
    library_id = library_id_for_folder(folder)
    if not library_id:
        raise RuntimeError(f"no Kavita library found for {folder}")
    settings = settings if isinstance(settings, dict) else {}
    token = kavita_plugin_token(settings)
    response = http_requests().post(
        settings["base_url"].rstrip("/") + "/library/scan",
        headers={"Authorization": f"Bearer {token}"},
        params={"libraryId": library_id},
        timeout=30,
    )
    response.raise_for_status()
    result = {
        "folder": folder,
        "status_code": response.status_code,
        "mode": mode,
        "library_id": library_id,
    }
    if folder_scan_status_code is not None:
        result["folder_scan_status_code"] = folder_scan_status_code
    if isinstance(details, dict):
        result.update(details)
    return result


def trigger_kavita_cover_refresh(
    folder,
    *,
    settings,
    library_id_for_folder,
    comic_root=None,
    manga_root=None,
    kavita_comic_root=None,
    kavita_manga_root=None,
    force=True,
):
    """Make Kavita re-derive its cached cover images for a folder's library.

    A scan is not enough, and neither is a forced scan. Measured against a real
    Kavita: after an archive gained a new first page, a `force=true` library
    scan re-read the file -- the stored page count went up -- while the series
    kept serving the cover it had already generated. Only the metadata refresh
    regenerates the image.

    That makes this call load-bearing for cover injection rather than an
    optimisation: without it the archive on disk is correct and every Kavita
    shelf still shows the old picture, which reads as the feature not working.
    """
    if not callable(library_id_for_folder):
        raise RuntimeError("Kavita library id adapter is unavailable")
    settings = settings if isinstance(settings, dict) else {}
    # Kavita's FolderPath table stores the paths *Kavita* sees (/data/manga),
    # not the host path the operator mounted there. Resolving the library from
    # an untranslated host path matches nothing, so every refresh failed with
    # "no Kavita library found" -- which is exactly what happened in production:
    # correct injections, stale covers, and an error only visible if someone
    # read the return value. The scan path has always translated first.
    lookup_folder = folder
    if comic_root and manga_root:
        try:
            lookup_folder = host_path_to_frontend_path(
                folder,
                comic_root=comic_root,
                manga_root=manga_root,
                frontend_comic_root=kavita_comic_root or settings.get("kavita_comic_root", "/data/comics"),
                frontend_manga_root=kavita_manga_root or settings.get("kavita_manga_root", "/data/manga"),
            )
        except ValueError:
            raise RuntimeError(f"folder is outside the configured Kavita roots: {folder}")
    library_id = library_id_for_folder(lookup_folder)
    if not library_id:
        raise RuntimeError(f"no Kavita library found for {lookup_folder}")
    token = kavita_plugin_token(settings)
    response = http_requests().post(
        settings["base_url"].rstrip("/") + "/library/refresh-metadata",
        headers={"Authorization": f"Bearer {token}"},
        params={"libraryId": library_id, "force": "true" if force else "false"},
        timeout=30,
    )
    response.raise_for_status()
    return {
        "folder": folder,
        "status_code": response.status_code,
        "library_id": library_id,
        "mode": "cover_refresh",
        "force": bool(force),
    }


def trigger_kavita_scan_folder(
    host_folder,
    *,
    settings,
    comic_root,
    manga_root,
    kavita_comic_root,
    kavita_manga_root,
    force_library_scan=False,
    series_ids_for_folder=None,
    library_id_for_folder=None,
    max_attempts=3,
    retry_delay_seconds=2,
):
    settings = settings if isinstance(settings, dict) else {}
    folder = host_path_to_frontend_path(
        host_folder,
        comic_root=comic_root,
        manga_root=manga_root,
        frontend_comic_root=kavita_comic_root,
        frontend_manga_root=kavita_manga_root,
    )
    response = None
    last_exc = None
    for attempt in range(max(1, int(max_attempts))):
        try:
            response = http_requests().post(
                settings["base_url"].rstrip("/") + "/Library/scan-folder",
                headers={"x-api-key": settings["api_key"]},
                json={"folderPath": folder, "abortOnNoSeriesMatch": False},
                timeout=30,
            )
            break
        except Exception as exc:
            last_exc = exc
            response = None
            if attempt + 1 < max(1, int(max_attempts)):
                time.sleep(retry_delay_seconds)
    if response is None:
        # Kavita was unreachable across every retry (busy/restarting) -- fall back to a
        # library-wide scan instead of dropping the sync silently, the fire-and-forget
        # behavior that used to leave newly imported issues invisible until a manual scan.
        return trigger_kavita_library_scan(
            folder,
            settings=settings,
            library_id_for_folder=library_id_for_folder,
            mode="library_scan_fallback_after_folder_scan_unreachable",
            details={
                "folder_scan_error": safe_error_text(
                    last_exc,
                    (settings.get("api_key"),),
                )
            },
        )
    if response.status_code < 400:
        return {"folder": folder, "status_code": response.status_code, "mode": "folder_scan"}
    if response.status_code not in {401, 403} and response.status_code < 500:
        response.raise_for_status()
    if force_library_scan:
        return trigger_kavita_library_scan(
            folder,
            settings=settings,
            library_id_for_folder=library_id_for_folder,
            mode="library_scan_forced",
            folder_scan_status_code=response.status_code,
        )
    series_ids = series_ids_for_folder(folder) if callable(series_ids_for_folder) else []
    if series_ids:
        token = kavita_plugin_token(settings)
        headers = {"Authorization": f"Bearer {token}"}
        statuses = []
        errors = []
        for series_id in series_ids:
            try:
                scan = http_requests().post(
                    settings["base_url"].rstrip("/") + "/Series/scan",
                    headers=headers,
                    json={"seriesId": int(series_id)},
                    timeout=30,
                )
                scan.raise_for_status()
                statuses.append({"series_id": int(series_id), "status_code": scan.status_code})
            except Exception as exc:
                errors.append(
                    {
                        "series_id": int(series_id),
                        "error": safe_error_text(
                            exc,
                            (settings.get("api_key"), token),
                        ),
                    }
                )
        if statuses and not errors:
            return {
                "folder": folder,
                "status_code": 200,
                "mode": "series_scan",
                "series": statuses,
                "folder_scan_status_code": response.status_code,
            }
        return trigger_kavita_library_scan(
            folder,
            settings=settings,
            library_id_for_folder=library_id_for_folder,
            mode="library_scan_fallback_after_series_scan",
            folder_scan_status_code=response.status_code,
            details={
                "series": statuses,
                "series_scan_errors": errors,
            },
        )
    return trigger_kavita_library_scan(
        folder,
        settings=settings,
        library_id_for_folder=library_id_for_folder,
        mode="library_scan_fallback",
        folder_scan_status_code=response.status_code,
    )


def check_library_visibility(
    host_path,
    *,
    kavita_enabled=True,
    check_kavita_visibility=None,
    komga_enabled=True,
    check_komga_visibility=None,
    provider_order=("kavita", "komga"),
):
    result = {
        "library_visible": False,
        "library_visibility_provider": "",
        "kavita_visible": False,
        "komga_visible": False,
        "komga_visibility": {},
    }
    checks = {
        "kavita": (bool(kavita_enabled), check_kavita_visibility),
        "komga": (bool(komga_enabled), check_komga_visibility),
    }
    for provider in provider_order or ():
        enabled, checker = checks.get(provider, (False, None))
        if not enabled:
            continue
        if not callable(checker):
            visibility = {
                "provider": provider,
                "visible": False,
                "status": "adapter_unavailable",
            }
        else:
            try:
                visibility = _normalize_visibility(provider, checker(host_path))
            except Exception as exc:
                visibility = _visibility_error(provider, exc)
        if provider == "kavita":
            result["kavita_visibility"] = visibility
            result["kavita_status"] = visibility.get("status")
            result["kavita_visible"] = bool(visibility.get("visible"))
        elif provider == "komga":
            result["komga_visibility"] = visibility
            result["komga_visible"] = bool(visibility.get("visible"))
        if visibility.get("visible"):
            result["library_visible"] = True
            result["library_visibility_provider"] = provider
            break
    return result


def sync_library_frontends(
    folders,
    *,
    frontend_sync_enabled=True,
    kavita_scan_enabled=True,
    trigger_kavita_scan=None,
    load_komga_settings=None,
    trigger_komga_scan=None,
    force_library_scan_folders=None,
    log_event=None,
    event_prefix="",
):
    folders = unique_folders(folders)
    force_keys = {normalized_folder_key(folder) for folder in force_library_scan_folders or []}
    event_prefix = str(event_prefix or "")
    kavita_tasks = []
    komga_tasks = []

    if not folders:
        return {
            "kavita": kavita_tasks,
            "komga": komga_tasks,
            "library_scan_tasks": {"kavita": kavita_tasks, "komga": komga_tasks},
            "requested": 0,
            "errors": [],
            "folders": [],
        }

    if frontend_sync_enabled and kavita_scan_enabled:
        for folder in folders:
            try:
                result = _call_kavita_scan(
                    trigger_kavita_scan,
                    folder,
                    normalized_folder_key(folder) in force_keys,
                )
                if isinstance(result, dict):
                    result.setdefault("provider", "kavita")
                kavita_tasks.append(result)
                _log(log_event, {"event": event_prefix + "kavita_scan_folder_queued", **(result if isinstance(result, dict) else {"result": result})})
            except Exception as exc:
                task = {"provider": "kavita", "folder": folder, "error": safe_error_text(exc)}
                kavita_tasks.append(task)
                _log(log_event, {"event": event_prefix + "kavita_scan_folder_failed", **task})
    else:
        kavita_tasks.append(
            {
                "provider": "kavita",
                "skipped": True,
                "reason": "frontend_sync_disabled" if not frontend_sync_enabled else "kavita_scan_after_import_disabled",
                "folders": folders,
            }
        )

    if frontend_sync_enabled:
        try:
            komga_settings = _load_komga_settings(load_komga_settings)
        except Exception as exc:
            error = safe_error_text(exc)
            task = {"provider": "komga", "skipped": True, "reason": "komga_settings_load_failed", "error": error, "folders": folders}
            komga_tasks.append(task)
            _log(log_event, {"event": event_prefix + "komga_settings_load_failed", "error": error})
            komga_settings = {"enabled": False}
        if komga_settings.get("enabled"):
            if not callable(trigger_komga_scan):
                task = {"provider": "komga", "skipped": True, "reason": "komga_scan_adapter_unavailable", "folders": folders}
                komga_tasks.append(task)
                _log(log_event, {"event": event_prefix + "komga_scan_folder_skipped", **task})
            else:
                for folder in folders:
                    try:
                        result = trigger_komga_scan(folder)
                        if isinstance(result, dict):
                            result.setdefault("provider", "komga")
                        komga_tasks.append(result)
                        _log(
                            log_event,
                            {
                                "event": event_prefix + ("komga_scan_folder_skipped" if isinstance(result, dict) and result.get("skipped") else "komga_scan_folder_queued"),
                                **(result if isinstance(result, dict) else {"result": result}),
                            },
                        )
                    except Exception as exc:
                        task = {"provider": "komga", "folder": folder, "error": safe_error_text(exc)}
                        komga_tasks.append(task)
                        _log(log_event, {"event": event_prefix + "komga_scan_folder_failed", **task})
    else:
        komga_tasks.append(
            {
                "provider": "komga",
                "skipped": True,
                "reason": "frontend_sync_disabled",
                "folders": folders,
            }
        )

    tasks = {"kavita": kavita_tasks, "komga": komga_tasks}
    all_tasks = [
        task
        for provider_tasks in tasks.values()
        for task in provider_tasks
        if isinstance(task, dict)
    ]
    return {
        "kavita": kavita_tasks,
        "komga": komga_tasks,
        "library_scan_tasks": tasks,
        "requested": sum(1 for task in all_tasks if _task_requested(task)),
        "errors": [task for task in all_tasks if task.get("error")],
        "folders": folders,
    }


def folders_from_imported_items(imported):
    folders = []
    for item in imported or []:
        if not isinstance(item, dict):
            continue
        dest = str(item.get("dest") or "").strip()
        if dest:
            folders.append(str(Path(dest).parent))
    return unique_folders(folders)
