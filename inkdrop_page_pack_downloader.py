"""Safe page-image-to-CBZ staging helper for InkDrop reader sources.

The helper writes files only when a caller supplies an HTTP client and an
explicit staging path. It does not talk to the database or import into Kavita.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
import zipfile
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import inkdrop_source_providers as providers
import inkdrop_sources


CONTRACT_VERSION = 2
DEFAULT_MAX_PAGES = 400
DEFAULT_MAX_PAGE_BYTES = 25 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_MAX_REDIRECTS = 5
MAX_CERTIFIED_REDIRECT_COUNT = 2**31 - 1
IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "application/octet-stream"}
HTML_MARKERS = (b"<!doctype html", b"<html", b"<body", b"captcha", b"cloudflare", b"login")


FAILURE_CLASS_INFRASTRUCTURE = "infrastructure"
FAILURE_CLASS_SOURCE_CONTENT = "source_content"
FAILURE_CLASS_POLICY = "policy"


class PagePackSourceContentError(ValueError):
    pass


class PagePackPolicyError(ValueError):
    pass


def _blocked(reason, *, failure_class=FAILURE_CLASS_INFRASTRUCTURE, **extra):
    out = {
        "page_pack_contract_version": CONTRACT_VERSION,
        "ok": False,
        "status": "blocked",
        "reason": str(reason or "page_pack_blocked"),
        "failure_reason": str(reason or "page_pack_blocked"),
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
    return {str(key).lower(): value for key, value in headers.items()}


def _content_type(headers):
    return providers.content_type_base(_headers_map(headers).get("content-type"))


def _intish(value, default=None):
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except Exception:
        return default


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
    redirect_count_valid = False
    transport_contract_version = None
    status_present = False
    redirect_evidence_present = False
    peer_ip = ""
    redirect_hops = []
    redirect_chain_valid = True
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
        status_present = "status_code" in response or "status" in response
        status_code = _intish(response.get("status_code", response.get("status")))
        final_url = str(response.get("final_url") or response.get("response_url") or response.get("url") or "").strip()
        redirect_evidence_present = "redirect_count" in response
        raw_redirect_count = response.get("redirect_count")
        redirect_count_valid = bool(
            type(raw_redirect_count) is int
            and 0 <= raw_redirect_count <= MAX_CERTIFIED_REDIRECT_COUNT
        )
        redirect_count = raw_redirect_count if redirect_count_valid else 0
        transport_contract_version = _intish(response.get("source_http_contract_version"))
        peer_ip = str(response.get("peer_ip") or response.get("connected_ip") or "").strip()
        hops_present = "redirect_hops" in response
        hashes_present = "redirect_url_hashes" in response
        raw_hops = response.get("redirect_hops") if hops_present else None
        raw_hashes = response.get("redirect_url_hashes") if hashes_present else None
        hops_valid = not hops_present or (
            isinstance(raw_hops, list)
            and all(isinstance(item, dict) for item in raw_hops)
        )
        hashes_valid = not hashes_present or (
            isinstance(raw_hashes, list)
            and all(isinstance(item, str) and item for item in raw_hashes)
        )
        redirect_chain_valid = bool(
            hops_valid
            and hashes_valid
            and not (
                hops_present
                and hashes_present
                and len(raw_hops) != len(raw_hashes)
            )
        )
        if redirect_chain_valid:
            redirect_hops = list(raw_hops if hops_present else (raw_hashes or []))
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
    if isinstance(body, str):
        body = body.encode("utf-8")
    return {
        "headers": _headers_map(headers),
        "status_code": status_code,
        "final_url": final_url,
        "redirect_count": redirect_count,
        "redirect_count_valid": redirect_count_valid,
        "redirect_evidence_present": redirect_evidence_present,
        "status_present": status_present,
        "transport_contract_version": transport_contract_version,
        "peer_ip": peer_ip,
        "redirect_hops": redirect_hops,
        "redirect_chain_valid": redirect_chain_valid,
        "body": bytes(body or b""),
    }


def _looks_html(data):
    sample = bytes(data or b"")[:8192].lower().lstrip()
    return any(marker in sample for marker in HTML_MARKERS)


def _confined_paths(staging_root, local_path=None, partial_path=None):
    if not str(staging_root or "").strip():
        return None, None, None, "missing_staging_root"
    root = Path(staging_root).expanduser().resolve()
    target = Path(str(local_path or "")).expanduser()
    if not str(local_path or "").strip():
        return root, None, None, "missing_local_path"
    if not target.is_absolute():
        target = root / target
    target = target.resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return root, target, None, "target_outside_staging_root"
    if target.suffix.lower() != ".cbz":
        return root, target, None, "target_not_cbz"
    partial = Path(str(partial_path or f"{target}.part")).expanduser()
    if not partial.is_absolute():
        partial = root / partial
    partial = partial.resolve()
    try:
        partial.relative_to(root)
    except ValueError:
        return root, target, partial, "partial_outside_staging_root"
    if partial == target:
        return root, target, partial, "partial_matches_final_path"
    return root, target, partial, ""


def _cleanup_partial(partial_path, root):
    try:
        partial = Path(partial_path).resolve()
        partial.relative_to(Path(root).resolve())
        if partial.exists() and partial.is_file():
            partial.unlink()
    except Exception:
        pass


def _metadata_path(final_path):
    final_path = Path(final_path)
    return final_path.with_name(final_path.name + ".source.json")


def _write_metadata(final_path, metadata):
    path = _metadata_path(final_path)
    partial = path.with_name(path.name + ".part")
    partial.write_text(json.dumps(metadata, sort_keys=True, indent=2, ensure_ascii=True), encoding="utf-8")
    partial.replace(path)
    return path


def _candidate_from_task(download_task):
    task = download_task if isinstance(download_task, dict) else {}
    raw = task.get("raw_json")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except ValueError:
            raw = {}
    raw = raw if isinstance(raw, dict) else {}
    nested_raw = raw.get("raw") if isinstance(raw.get("raw"), dict) else raw
    seed = nested_raw.get("download_task_seed") if isinstance(nested_raw.get("download_task_seed"), dict) else {}
    seed_raw = seed.get("raw_json") if isinstance(seed.get("raw_json"), dict) else {}
    candidate = seed_raw.get("candidate") if isinstance(seed_raw.get("candidate"), dict) else {}
    if not candidate:
        candidate = raw.get("candidate") if isinstance(raw.get("candidate"), dict) else {}
    if not candidate:
        candidate = task.get("candidate") if isinstance(task.get("candidate"), dict) else {}
    return candidate


def _task_raw_json(download_task):
    task = download_task if isinstance(download_task, dict) else {}
    raw = task.get("raw_json")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except ValueError:
            raw = {}
    return raw if isinstance(raw, dict) else {}


def _page_pack_task_seed(download_task):
    """The nested download_task_seed capsule, as persisted by the recorder.

    reader_page_pack_attempt_seed() puts the executable task seed (with the
    page_pack_task/download_guard markers under its own raw_json, and its
    own local_path/partial_path/provider/identity fields at this level)
    under raw.download_task_seed, and download_task_from_attempt()
    serializes that whole normalized attempt as the task's own raw_json --
    so a persisted row's markers live here, not at the task raw's own root.
    _candidate_from_task() already reads the candidate from this same
    nested location; this mirrors it for the provenance markers and the
    seed's own recorded path/identity fields, which prove the capsule
    wasn't tampered with before a rebase trusts it.
    """

    raw = _task_raw_json(download_task)
    nested_raw = raw.get("raw") if isinstance(raw.get("raw"), dict) else raw
    seed = nested_raw.get("download_task_seed") if isinstance(nested_raw.get("download_task_seed"), dict) else {}
    return seed


def owned_page_pack_rebase_target(download_task, staging_root):
    """Rebuild only an exact InkDrop discovery-default page-pack target."""
    task = download_task if isinstance(download_task, dict) else {}
    if str(task.get("download_client") or "").strip().lower() != providers.PAGE_PACK_DOWNLOAD_CLIENT:
        return None
    if not str(staging_root or "").strip():
        return None
    raw = _task_raw_json(task)
    seed = _page_pack_task_seed(task)
    seed_raw = seed.get("raw_json") if isinstance(seed.get("raw_json"), dict) else {}
    candidate = _candidate_from_task(task)
    root_modern_provenance = raw.get("page_pack_task") is True and raw.get("download_guard") == "reader_page_pack_verdict"
    nested_modern_provenance = seed_raw.get("page_pack_task") is True and seed_raw.get("download_guard") == "reader_page_pack_verdict"
    modern_provenance = root_modern_provenance or nested_modern_provenance
    legacy_provenance = bool(
        raw.get("artifact_safe") is True
        and str(raw.get("auto_grab_verdict") or "").strip().lower() == "auto_grab_safe"
        and raw.get("suwayomi_page_pack") is True
        and candidate.get("artifact_safe") is True
        and str(candidate.get("auto_grab_verdict") or "").strip().lower() == "auto_grab_safe"
        and candidate.get("suwayomi_page_pack") is True
        and str(raw.get("download_client") or "").strip().lower() == providers.PAGE_PACK_DOWNLOAD_CLIENT
        and str(raw.get("category") or "").strip().lower() == "inkdrop-page-pack"
        and str(task.get("category") or "").strip().lower() == "inkdrop-page-pack"
        and str(raw.get("staging_origin") or "").strip().lower() == "discovery_default"
    )
    if not modern_provenance and not legacy_provenance:
        return None
    identity = str(candidate.get("candidate_identity") or "").strip().lower()
    task_identity = str(task.get("candidate_identity") or "").strip().lower()
    external_id = str(task.get("external_id") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{24}", identity) or task_identity != identity or external_id != identity:
        return None
    provider_id = inkdrop_sources.provider_key(candidate.get("provider_id"))
    task_provider_id = inkdrop_sources.provider_key(task.get("provider_id") or task.get("source"))
    if not provider_id or provider_id != task_provider_id:
        return None
    if legacy_provenance:
        raw_identity = str(raw.get("candidate_identity") or "").strip().lower()
        raw_provider_values = (
            inkdrop_sources.provider_key(raw.get("provider")),
            inkdrop_sources.provider_key(raw.get("provider_id")),
            inkdrop_sources.provider_key(raw.get("source")),
            inkdrop_sources.provider_key(candidate.get("provider_id")),
            inkdrop_sources.provider_key(candidate.get("source")),
            inkdrop_sources.provider_key(task.get("provider")),
            inkdrop_sources.provider_key(task.get("provider_id")),
            inkdrop_sources.provider_key(task.get("source")),
        )
        if raw_identity != identity or any(value != provider_id for value in raw_provider_values):
            return None

    nested_only_modern_provenance = nested_modern_provenance and not root_modern_provenance and not legacy_provenance
    if nested_only_modern_provenance:
        # The task raw's own root carries none of the trusted markers --
        # they only exist because the recorder nested this exact seed
        # capsule under raw.download_task_seed. Prove the capsule's own
        # recorded identity/provider/paths are exactly what got persisted
        # onto the task's real columns before trusting a rebase derived
        # from it; a tampered or unrelated capsule fails here instead of
        # silently reusing the task's own (unverified) columns.
        seed_identity = str(seed.get("candidate_identity") or "").strip().lower()
        seed_external_id = str(seed.get("external_id") or "").strip().lower()
        seed_provider_id = inkdrop_sources.provider_key(seed.get("provider_id") or seed.get("provider"))
        if seed_identity != identity or seed_external_id != identity or seed_provider_id != provider_id:
            return None
        supplied_local = Path(str(task.get("local_path") or "")).expanduser().resolve()
        supplied_partial_text = str(task.get("partial_path") or "").strip()
        seed_local_text = str(seed.get("local_path") or "").strip()
        seed_partial_text = str(seed.get("partial_path") or "").strip()
        if not seed_local_text or Path(seed_local_text).expanduser().resolve() != supplied_local:
            return None
        if seed_partial_text and supplied_partial_text and (
            Path(seed_partial_text).expanduser().resolve() != Path(supplied_partial_text).expanduser().resolve()
        ):
            return None
    else:
        legacy = providers.reader_page_pack_task_seed(candidate, None)
        supplied_local = Path(str(task.get("local_path") or "")).expanduser().resolve()
        legacy_local = Path(legacy["local_path"]).expanduser().resolve()
        legacy_partial = Path(legacy["partial_path"]).expanduser().resolve()
        if supplied_local != legacy_local:
            return None
        supplied_partial_text = str(task.get("partial_path") or "").strip()
        if legacy_provenance and not supplied_partial_text:
            return None
        if supplied_partial_text and Path(supplied_partial_text).expanduser().resolve() != legacy_partial:
            return None
    if legacy_provenance:
        raw_root = Path(str(raw.get("staging_root") or "")).expanduser().resolve()
        raw_local = Path(str(raw.get("local_path") or "")).expanduser().resolve()
        raw_download = Path(str(raw.get("download_path") or "")).expanduser().resolve()
        legacy_root = legacy_local.parent.parent
        if raw_root != legacy_root or raw_local != legacy_local or raw_download != legacy_local:
            return None
        source_url = str(candidate.get("source_url") or "").strip()
        source_path = str(candidate.get("source_path") or "").strip()
        raw_source_url = str(raw.get("source_url") or "").strip()
        raw_source_path = str(raw.get("source_path") or "").strip()
        source_kind = str(candidate.get("source_kind") or "").strip().lower()
        raw_source_kind = str(raw.get("source_kind") or "").strip().lower()
        if not source_url or not (source_url == source_path == raw_source_url == raw_source_path):
            return None
        if source_kind != "suwayomi_api_page_provider" or raw_source_kind != source_kind:
            return None

    rebuilt = providers.reader_page_pack_task_seed(candidate, staging_root)
    return rebuilt["local_path"], rebuilt["partial_path"]


def _page_image_extensions_from_task(download_task):
    candidate = _candidate_from_task(download_task)
    values = candidate.get("page_image_extensions") if isinstance(candidate.get("page_image_extensions"), list) else []
    return [providers.normalize_extension(value) for value in values]


def _declared_page_image_extension(download_task, index):
    extensions = _page_image_extensions_from_task(download_task)
    if 0 <= int(index or 0) < len(extensions):
        ext = extensions[int(index or 0)]
        if ext:
            return ext
    candidate = _candidate_from_task(download_task)
    return providers.normalize_extension(candidate.get("page_image_extension"))


def _image_extension_for_content_type(content_type):
    content_type = providers.content_type_base(content_type)
    if content_type == "image/jpeg":
        return ".jpg"
    if content_type == "image/png":
        return ".png"
    if content_type == "image/webp":
        return ".webp"
    return ""


def _image_extension_for_bytes(data):
    data = bytes(data or b"")
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    return ""


def _host_allowed(url, allowed_hosts):
    host = _url_host(url)
    allowed = set(_host_values(allowed_hosts))
    return bool(host and (not allowed or host in allowed))


def _unsafe_host(host):
    host = str(host or "").strip().lower().rstrip(".").strip("[]")
    if not host or host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return True
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    return not address.is_global


def _url_origin(value):
    parsed = urlparse(str(value or "").strip())
    scheme = str(parsed.scheme or "").lower()
    host = str(parsed.hostname or "").lower().rstrip(".").strip("[]")
    try:
        port = parsed.port
    except ValueError:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    if scheme not in {"http", "https"} or not host:
        return None
    return scheme, host, port or (443 if scheme == "https" else 80)


def _trusted_endpoint_matches(url, trusted_endpoint):
    endpoint_origin = _url_origin(trusted_endpoint)
    return bool(endpoint_origin and _url_origin(url) == endpoint_origin)


def _resolve_host_ips(host, port, resolver):
    host = str(host or "").strip().lower().strip("[]")
    try:
        return [str(ipaddress.ip_address(host.split("%", 1)[0]))]
    except ValueError:
        pass
    resolver = resolver or socket.getaddrinfo
    try:
        try:
            rows = resolver(host, int(port), type=socket.SOCK_STREAM)
        except TypeError:
            rows = resolver(host, int(port))
    except Exception:
        return []
    out = []
    for row in list(rows or [])[:32]:
        address = ""
        if isinstance(row, str):
            address = row
        elif isinstance(row, (tuple, list)) and len(row) >= 5 and isinstance(row[4], (tuple, list)):
            address = row[4][0] if row[4] else ""
        elif isinstance(row, (tuple, list)) and row:
            address = row[0]
        try:
            normalized = str(ipaddress.ip_address(str(address or "").split("%", 1)[0]))
        except ValueError:
            continue
        if normalized not in out:
            out.append(normalized)
    return out[:16]


def _url_resolution(url, *, resolver=None, trusted_endpoint=""):
    origin = _url_origin(url)
    if not origin:
        return {}, "page_image_url_not_safe_http"
    scheme, host, port = origin
    ips = _resolve_host_ips(host, port, resolver)
    if not ips:
        return {}, "page_image_dns_resolution_failed"
    trusted = _trusted_endpoint_matches(url, trusted_endpoint)
    if any(_unsafe_host(ip) for ip in ips) and not trusted:
        return {}, "untrusted_private_page_image_address"
    return {"scheme": scheme, "host": host, "port": port, "ips": ips, "trusted": trusted}, ""


def _canonical_page_pack_identity(download_task):
    candidate = _candidate_from_task(download_task)
    work_id = str(candidate.get("canonical_work_id") or "").strip()
    series = str(candidate.get("series_title") or "").strip()
    unit = str(candidate.get("unit_type") or candidate.get("unitType") or "").strip().lower()
    chapter = str(candidate.get("chapter_number") or candidate.get("chapter") or "").strip()
    volume = str(candidate.get("volume_number") or candidate.get("volume") or "").strip()
    volume_pack = bool(candidate.get("volume_page_pack") or candidate.get("mangadex_volume_page_pack") or candidate.get("volume_pack"))
    if not work_id or not series:
        return {}, "missing_canonical_work_identity"
    if unit == "chapter" and chapter:
        number = chapter
    elif unit == "volume" and volume and volume_pack:
        number = volume
    elif unit not in {"chapter", "volume"}:
        return {}, "unsupported_page_pack_unit"
    else:
        return {}, "missing_canonical_unit_number"
    if not re.fullmatch(r"\d{1,5}(?:\.\d{1,5})?", number):
        return {}, "invalid_canonical_unit_number"
    return {
        "work_id": work_id,
        "series": series,
        "unit": unit,
        "number": number,
        "language": str(candidate.get("language") or candidate.get("translated_language") or "").strip().lower(),
    }, ""


def _page_pack_comicinfo(download_task):
    identity, reason = _canonical_page_pack_identity(download_task)
    if reason:
        return b""
    unit = identity["unit"]
    number = identity["number"]
    if unit == "chapter":
        fields = (("Series", identity["series"]), ("Title", f"Chapter {number}"), ("Number", number), ("Format", "Manga Chapter"), ("LanguageISO", identity["language"]))
    else:
        fields = (("Series", identity["series"]), ("Title", f"Volume {number}"), ("Volume", number), ("Format", "Manga"), ("LanguageISO", identity["language"]))
    root = ET.Element("ComicInfo")
    for name, value in fields:
        if value:
            ET.SubElement(root, name).text = value
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"


def _write_archive_member(archive, name, data):
    info = zipfile.ZipInfo(str(name), date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    archive.writestr(info, data, compresslevel=6)


def _identity_hash(value):
    return "sha256:" + hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _safe_summary_token(value, maximum=100):
    text = str(value or "").strip()
    return text[:maximum] if text and re.fullmatch(r"[A-Za-z0-9_.:@+-]+", text) else ""


def page_image_urls_from_task(download_task):
    candidate = _candidate_from_task(download_task)
    urls = candidate.get("page_image_urls") if isinstance(candidate.get("page_image_urls"), list) else []
    return [str(url or "").strip() for url in urls if str(url or "").strip()]


def _url_host(url):
    host = str(urlparse(str(url or "")).hostname or "").strip().lower()
    return host.strip("[]")


def _host_values(value):
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
            text = str(urlparse(text).hostname or "")
        text = text.strip("[]")
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _candidate_allowed_hosts(download_task):
    candidate = _candidate_from_task(download_task)
    hosts = []
    for key in ("page_image_allowed_hosts", "allowed_hosts"):
        hosts.extend(_host_values(candidate.get(key)))
    out = []
    seen = set()
    for host in hosts:
        if host and host not in seen:
            seen.add(host)
            out.append(host)
    return out


def stage_page_pack(
    download_task,
    *,
    staging_root=None,
    http_get=None,
    max_pages=DEFAULT_MAX_PAGES,
    max_page_bytes=DEFAULT_MAX_PAGE_BYTES,
    max_total_bytes=DEFAULT_MAX_TOTAL_BYTES,
    max_redirects=DEFAULT_MAX_REDIRECTS,
    resolver=None,
    trusted_page_endpoint="",
):
    task = download_task if isinstance(download_task, dict) else {}
    provider_id = str(task.get("provider_id") or task.get("source") or "").strip()
    download_task_id = task.get("id") or task.get("external_id") or task.get("candidate_identity")
    if not http_get:
        return _blocked("http_client_required", provider_id=provider_id, download_task_id=download_task_id)
    effective_root = staging_root or task.get("save_path")
    local_path = task.get("local_path")
    partial_path = task.get("partial_path")
    root, target, partial, reason = _confined_paths(
        effective_root,
        local_path=local_path,
        partial_path=partial_path,
    )
    if reason in {"target_outside_staging_root", "partial_outside_staging_root"}:
        rebased = owned_page_pack_rebase_target(task, staging_root)
        if rebased:
            local_path, partial_path = rebased
            root, target, partial, reason = _confined_paths(
                effective_root,
                local_path=local_path,
                partial_path=partial_path,
            )
    if reason:
        return _blocked(reason, provider_id=provider_id, download_task_id=download_task_id)
    urls = page_image_urls_from_task(task)
    if not urls:
        return _blocked("missing_page_image_urls", provider_id=provider_id, download_task_id=download_task_id)
    if len(urls) > max(1, int(max_pages or DEFAULT_MAX_PAGES)):
        return _policy_blocked("too_many_page_images", provider_id=provider_id, download_task_id=download_task_id, page_count=len(urls))
    if len(set(urls)) != len(urls):
        return _source_content_blocked("duplicate_page_image_url", provider_id=provider_id, download_task_id=download_task_id)
    identity, identity_reason = _canonical_page_pack_identity(task)
    if identity_reason:
        return _source_content_blocked(identity_reason, provider_id=provider_id, download_task_id=download_task_id)
    comicinfo = _page_pack_comicinfo(task)
    if not comicinfo:
        return _source_content_blocked("missing_authoritative_comicinfo", provider_id=provider_id, download_task_id=download_task_id)
    if target.exists():
        return _blocked("target_exists", provider_id=provider_id, download_task_id=download_task_id, path=str(target))
    page_image_allowed_hosts = _candidate_allowed_hosts(task)
    if not page_image_allowed_hosts:
        return _blocked("missing_page_image_allowed_hosts", provider_id=provider_id, download_task_id=download_task_id)
    trusted_origin = _url_origin(trusted_page_endpoint)
    if trusted_page_endpoint and not trusted_origin:
        return _policy_blocked("invalid_trusted_page_endpoint", provider_id=provider_id, download_task_id=download_task_id)
    total_bytes = 0
    written = 0
    try:
        partial.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for index, url in enumerate(urls, start=1):
                parsed = urlparse(url)
                if str(parsed.scheme or "").lower() not in {"http", "https"}:
                    raise PagePackPolicyError("unsupported_page_image_scheme")
                if parsed.username is not None or parsed.password is not None:
                    raise PagePackPolicyError("page_image_url_userinfo_not_allowed")
                initial_origin = _url_origin(url)
                if not initial_origin:
                    raise PagePackPolicyError("page_image_url_not_safe_http")
                if page_image_allowed_hosts and not _host_allowed(url, page_image_allowed_hosts):
                    raise PagePackPolicyError("page_image_host_not_allowed")
                initial_resolution, resolution_reason = _url_resolution(
                    url,
                    resolver=resolver,
                    trusted_endpoint=trusted_page_endpoint,
                )
                if resolution_reason:
                    raise OSError(resolution_reason)
                url_ext = providers.normalize_extension(url)
                if url_ext and url_ext not in providers.GENERIC_PAGE_IMAGE_EXTENSIONS:
                    raise PagePackPolicyError("unsupported_page_image_extension")
                request = {
                    "method": "GET",
                    "url": url,
                    "headers": {"Accept": "image/avif,image/webp,image/png,image/jpeg,*/*;q=0.1"},
                    "purpose": "fetch_reader_page_image",
                }
                if page_image_allowed_hosts:
                    request["allowed_hosts"] = page_image_allowed_hosts
                response = _response_parts(
                    _response_from_http_get(
                        http_get,
                        request,
                    )
                )
                status_code = response["status_code"]
                if int(response["transport_contract_version"] or 0) < 2:
                    raise OSError("uncertified_page_image_transport")
                if not response["status_present"] or status_code is None or status_code <= 0:
                    raise OSError("missing_page_image_http_status")
                if status_code < 200 or status_code >= 300:
                    raise OSError("http_status_not_ok")
                if not response["final_url"] or not response["redirect_evidence_present"]:
                    raise OSError("missing_page_image_redirect_evidence")
                if not response["redirect_count_valid"]:
                    raise OSError("invalid_page_image_redirect_count")
                if not response["redirect_chain_valid"]:
                    raise OSError("invalid_page_image_redirect_chain")
                final = urlparse(response["final_url"])
                if str(final.scheme or "").lower() not in {"http", "https"} or not final.hostname:
                    raise PagePackPolicyError("page_image_final_url_not_safe_http")
                if final.username is not None or final.password is not None:
                    raise PagePackPolicyError("page_image_final_url_userinfo_not_allowed")
                if _url_origin(response["final_url"]) != initial_origin:
                    raise PagePackPolicyError("page_image_final_origin_mismatch")
                if response["redirect_count"] > max(0, int(max_redirects or 0)):
                    raise PagePackPolicyError("page_image_redirect_limit_exceeded")
                if len(response["redirect_hops"]) != response["redirect_count"]:
                    raise OSError("page_image_redirect_chain_mismatch")
                if response["redirect_count"] > 0:
                    raise OSError("uncertified_page_image_redirect_chain")
                if page_image_allowed_hosts and not _host_allowed(response["final_url"], page_image_allowed_hosts):
                    raise PagePackPolicyError("page_image_redirect_host_not_allowed")
                final_resolution, resolution_reason = _url_resolution(
                    response["final_url"],
                    resolver=resolver,
                    trusted_endpoint=trusted_page_endpoint,
                )
                if resolution_reason:
                    raise OSError(resolution_reason)
                try:
                    peer_ip = str(ipaddress.ip_address(response["peer_ip"].split("%", 1)[0]))
                except ValueError:
                    raise OSError("missing_page_image_peer_address")
                if peer_ip not in final_resolution["ips"]:
                    raise OSError("page_image_peer_address_mismatch")
                if _unsafe_host(peer_ip) and not final_resolution["trusted"]:
                    raise PagePackPolicyError("unsafe_page_image_peer_address")
                content_type = _content_type(response["headers"])
                if content_type and content_type not in IMAGE_CONTENT_TYPES:
                    raise PagePackSourceContentError("unsupported_image_content_type")
                data = response["body"]
                if not data:
                    raise PagePackSourceContentError("zero_size_page_image")
                if len(data) > max_page_bytes:
                    raise PagePackPolicyError("page_image_too_large")
                total_bytes += len(data)
                if total_bytes > max_total_bytes:
                    raise PagePackPolicyError("page_pack_too_large")
                if _looks_html(data):
                    raise PagePackSourceContentError("html_or_login_response")
                detected_ext = _image_extension_for_bytes(data)
                if not detected_ext:
                    raise PagePackSourceContentError("unrecognized_page_image_payload")
                content_ext = _image_extension_for_content_type(content_type)
                if content_ext and content_ext != detected_ext:
                    raise PagePackSourceContentError("image_content_type_mismatch")
                ext = detected_ext or url_ext or content_ext or _declared_page_image_extension(task, index - 1)
                _write_archive_member(archive, f"{index:04d}{ext}", data)
                written += 1
            _write_archive_member(archive, "ComicInfo.xml", comicinfo)
        if written != len(urls):
            raise PagePackSourceContentError("page_count_mismatch")
        partial.replace(target)
    except PagePackSourceContentError as exc:
        _cleanup_partial(partial, root)
        return _source_content_blocked(str(exc), provider_id=provider_id, download_task_id=download_task_id)
    except PagePackPolicyError as exc:
        _cleanup_partial(partial, root)
        return _policy_blocked(str(exc), provider_id=provider_id, download_task_id=download_task_id)
    except Exception as exc:
        _cleanup_partial(partial, root)
        return _blocked(str(exc), provider_id=provider_id, download_task_id=download_task_id)
    metadata = {
        "page_pack_contract_version": CONTRACT_VERSION,
        "provider_id": _safe_summary_token(provider_id),
        "download_client": providers.PAGE_PACK_DOWNLOAD_CLIENT,
        "page_count": written,
        "size_bytes": total_bytes,
        "page_image_url_hashes": [providers.url_hash(url) for url in urls],
        "download_task_id_hash": _identity_hash(download_task_id),
        "candidate_identity_hash": _identity_hash(task.get("candidate_identity")),
        "canonical_work_id_hash": _identity_hash(identity["work_id"]),
        "unit_type": identity["unit"],
        "unit_number": _safe_summary_token(identity["number"], maximum=32),
    }
    try:
        metadata_path = _write_metadata(target, metadata)
    except Exception as exc:
        _cleanup_partial(target, root)
        _cleanup_partial(_metadata_path(target), root)
        _cleanup_partial(_metadata_path(target).with_name(_metadata_path(target).name + ".part"), root)
        return _blocked(str(exc), provider_id=provider_id, download_task_id=download_task_id)
    return {
        "page_pack_contract_version": CONTRACT_VERSION,
        "ok": True,
        "status": "staged_file_ready",
        "provider_id": provider_id,
        "download_task_id": download_task_id,
        "download_client": providers.PAGE_PACK_DOWNLOAD_CLIENT,
        "local_path": str(target),
        "metadata_path": str(metadata_path),
        "page_count": written,
        "size_bytes": total_bytes,
    }
