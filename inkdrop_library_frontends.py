#!/usr/bin/env python3
"""Library frontend sync helpers for InkDrop imports.

Folder completion is InkDrop's durable truth. Frontends such as Kavita and Komga
are optional scan/visibility adapters that can be asked to catch up afterwards.
"""

import inspect
import re
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
        "error": f"{type(exc).__name__}: {exc}",
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
        result.update({"skipped": True, "reason": f"komga_{media_type}_library_ids_missing"})
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
            errors.append({"library_id": library_id, "error": str(exc)})
    result["libraries"] = statuses
    if errors:
        result["errors"] = errors
    result["status_code"] = statuses[0]["status_code"] if statuses else None
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
            return True, "exact_path"
        if Path(normalized).name.lower() == filename.lower():
            return True, "filename"
        if Path(normalized).stem.lower() == stem.lower() and Path(normalized).suffix.lower() == Path(filename).suffix.lower():
            return True, "filename_stem_suffix"
    return False, ""


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


def komga_file_visible_for_host_path(host_path, *, settings, comic_root, manga_root):
    settings = settings if isinstance(settings, dict) else {}
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
    queries = [host_path.name]
    if host_path.stem and host_path.stem not in queries:
        queries.append(host_path.stem)
    errors = []
    for query in queries:
        result["queries"].append(query)
        try:
            search = komga_search_books(settings, query, library_ids)
        except Exception as exc:
            errors.append({"query": query, "error": str(exc)})
            continue
        books = search.get("books") or []
        result["candidate_count"] += len(books)
        for book in books:
            matched, reason = komga_book_matches_path(book, host_path, komga_path)
            if not matched:
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
            errors.append({"query": "path_scan", "page": page, "error": str(exc)})
            break
        books = listing.get("books") or []
        result["path_scan_pages_checked"] += 1
        result["candidate_count"] += len(books)
        for book in books:
            matched, reason = komga_book_matches_path(book, host_path, komga_path)
            if not matched:
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
        result["reason"] = "komga_visibility_search_failed"
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
    response = http_requests().post(
        settings["base_url"].rstrip("/") + "/Plugin/authenticate",
        params={"apiKey": settings["api_key"], "pluginName": settings["plugin_name"]},
        timeout=30,
    )
    response.raise_for_status()
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
            details={"folder_scan_error": str(last_exc)},
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
                errors.append({"series_id": int(series_id), "error": str(exc)})
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
                task = {"provider": "kavita", "folder": folder, "error": str(exc)}
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
            task = {"provider": "komga", "skipped": True, "reason": "komga_settings_load_failed", "error": str(exc), "folders": folders}
            komga_tasks.append(task)
            _log(log_event, {"event": event_prefix + "komga_settings_load_failed", "error": str(exc)})
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
                        task = {"provider": "komga", "folder": folder, "error": str(exc)}
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
