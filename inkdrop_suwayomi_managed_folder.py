#!/usr/bin/env python3
"""Dry-run scanner for a Suwayomi-managed download folder.

The scanner is intentionally report-only. It does not move files, write DB rows,
or mark anything import-ready. Future automation can use the same evidence shape
after the user enables the managed-folder import-ready setting.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import time
from pathlib import Path

import inkdrop_state
import inkdrop_source_catalog
import inkdrop_sources


DEFAULT_PROVIDER_ID = "suwayomi_managed_folder"
DEFAULT_ALLOWED_EXTENSIONS = {".cbz", ".cbr", ".zip", ".pdf", ".epub"}
PARTIAL_SUFFIXES = (".part", ".tmp", ".crdownload", ".download")
GENERIC_FOLDER_NAMES = {
    "",
    "download",
    "downloads",
    "manga",
    "manhwa",
    "manhua",
    "suwayomi",
    "tachiyomi",
    "complete",
    "completed",
}


def _json_loads(value, default=None):
    if value in (None, ""):
        return default if default is not None else {}
    try:
        loaded = json.loads(value)
    except Exception:
        return default if default is not None else {}
    return loaded if loaded is not None else (default if default is not None else {})


def _number_text(value):
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        number = float(text)
    except Exception:
        return inkdrop_sources.normalize_title(text)
    if number.is_integer():
        return str(int(number))
    return str(number).rstrip("0").rstrip(".")


def _first_text(*values):
    for value in values:
        if value not in (None, "", [], {}):
            return str(value).strip()
    return ""


def _catalog_policy(provider_id=DEFAULT_PROVIDER_ID):
    entry = inkdrop_source_catalog.provider_entry(provider_id, default={}) or {}
    return dict(entry.get("policy") or {})


def _provider_policy_from_db(db_path, provider_id=DEFAULT_PROVIDER_ID):
    if not db_path:
        return {}
    path = Path(db_path)
    if not path.exists():
        return {}
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except Exception:
        return {}
    try:
        row = con.execute(
            "select settings_json from provider_configs where id=? limit 1",
            (provider_id,),
        ).fetchone()
    except Exception:
        row = None
    finally:
        con.close()
    if not row:
        return {}
    settings = _json_loads(row[0], {})
    policy = settings.get("policy") if isinstance(settings, dict) and isinstance(settings.get("policy"), dict) else {}
    merged = dict(policy)
    for key, value in (settings if isinstance(settings, dict) else {}).items():
        if key.startswith("suwayomi_folder_") or key in {
            "suwayomi_download_root",
            "suwayomi_import_staging_root",
            "allowed_extensions",
            "allowed_languages",
        }:
            merged[key] = value
    return merged


def _merged_policy(db_path=None, provider_id=DEFAULT_PROVIDER_ID, overrides=None):
    policy = _catalog_policy(provider_id)
    policy.update(_provider_policy_from_db(db_path, provider_id))
    for key, value in (overrides or {}).items():
        if value not in (None, ""):
            policy[key] = value
    return policy


def _allowed_extensions(policy):
    values = policy.get("allowed_extensions") or []
    if isinstance(values, str):
        values = [part.strip() for part in values.split(",")]
    exts = {
        ext if str(ext).startswith(".") else f".{ext}"
        for ext in values
        if str(ext or "").strip()
    }
    return {str(ext).lower() for ext in exts} or set(DEFAULT_ALLOWED_EXTENSIONS)


def _is_partial_path(path):
    name = path.name.lower()
    return any(name.endswith(suffix) for suffix in PARTIAL_SUFFIXES)


def _safe_path_component(value, fallback="item", max_length=120):
    text = str(value or "").strip()
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if not text:
        text = fallback
    return text[:max(1, int(max_length or 120))].strip(" .") or fallback


def _sha256_path(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _path_identity(path):
    text = str(Path(path).expanduser().resolve())
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _path_under(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except Exception:
        return False


def _staging_root_block_reason(staging_root, download_root):
    if not staging_root:
        return "staging_root_missing"
    try:
        staging = Path(staging_root).expanduser().resolve()
        source = Path(download_root).expanduser().resolve()
    except Exception:
        return "staging_root_invalid"
    if staging == source:
        return "staging_root_same_as_download_root"
    if _path_under(staging, source):
        return "staging_root_inside_download_root"
    return ""


def _candidate_unit(evidence, match):
    unit = str((match or {}).get("unit") or "").strip().lower()
    if unit in {"volume", "chapter"}:
        return unit
    return "volume" if evidence.get("volume") else "chapter"


def _candidate_number(evidence, match):
    unit = _candidate_unit(evidence, match)
    return _number_text((match or {}).get("number") or evidence.get(unit) or evidence.get("volume") or evidence.get("chapter"))


def _staging_target_path(staging_root, evidence, match):
    source_path = Path(evidence["source_path"])
    series = _safe_path_component((match or {}).get("series") or evidence.get("series"), fallback="series")
    queue_token = _safe_path_component((match or {}).get("queue_id"), fallback="queue", max_length=48)
    unit = _candidate_unit(evidence, match)
    prefix = "v" if unit == "volume" else "c"
    number = _safe_path_component(_candidate_number(evidence, match), fallback="0", max_length=24)
    filename = f"{series} {prefix}{number} [{queue_token}]{source_path.suffix.lower()}"
    target = Path(staging_root).expanduser().resolve() / series / filename
    return target


def _copy_to_staging(source_path, target_path):
    source = Path(source_path).expanduser().resolve()
    target = Path(target_path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    source_size = source.stat().st_size
    source_hash = None
    if target.exists():
        if not target.is_file():
            return {"ok": False, "reason": "staging_target_not_file", "staged_path": str(target)}
        if target.stat().st_size != source_size:
            return {"ok": False, "reason": "staging_conflict_existing_file", "staged_path": str(target)}
        source_hash = _sha256_path(source)
        if _sha256_path(target) != source_hash:
            return {"ok": False, "reason": "staging_conflict_existing_file", "staged_path": str(target)}
        return {
            "ok": True,
            "idempotent": True,
            "copied": False,
            "staged_path": str(target),
            "size_bytes": source_size,
            "content_sha256": source_hash,
        }
    temp_path = target.with_name(f".{target.name}.tmp-{int(time.time() * 1000)}")
    try:
        shutil.copy2(source, temp_path)
        temp_path.replace(target)
    finally:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except Exception:
            pass
    source_hash = _sha256_path(source)
    return {
        "ok": True,
        "idempotent": False,
        "copied": True,
        "staged_path": str(target),
        "size_bytes": source_size,
        "content_sha256": source_hash,
    }


def _promotion_attempt_payload(provider_id, evidence, match, copy_result, *, now):
    source_path = Path(evidence["source_path"]).expanduser().resolve()
    staged_path = Path(copy_result["staged_path"]).expanduser().resolve()
    path_id = _path_identity(source_path)
    unit = _candidate_unit(evidence, match)
    number = _candidate_number(evidence, match)
    title = f"{(match or {}).get('series') or evidence.get('series')} {'Vol.' if unit == 'volume' else 'Ch.'} {number}".strip()
    raw = {
        "kind": "suwayomi_managed_folder_import_ready",
        "provider_id": provider_id,
        "source_path": str(source_path),
        "staged_path": str(staged_path),
        "copy_result": copy_result,
        "evidence": evidence,
        "match": match,
        "rights_gate": "user_owned_collection_required",
        "copy_policy": "copy_to_staging",
        "cleanup_policy": "keep_source_files",
    }
    return {
        "kind": "suwayomi_managed_folder_import_ready",
        "source": provider_id,
        "provider_id": provider_id,
        "source_type": "suwayomi_managed_folder_source",
        "provider": "suwayomi",
        "protocol": "local",
        "download_client": "inkdrop_local_pack",
        "category": "suwayomi-managed-folder",
        "save_path": str(staged_path.parent),
        "local_path": str(staged_path),
        "staged_path": str(staged_path),
        "source_path": str(source_path),
        "external_id": path_id,
        "download_url_hash": path_id,
        "candidate_identity": f"{provider_id}:{match.get('queue_id')}:{path_id}",
        "lifecycle_phase": "import_ready",
        "status": "staged_file_ready",
        "reason": "Suwayomi managed folder staged file ready",
        "failure_reason": "",
        "retry_eligible": False,
        "title": title,
        "size_bytes": copy_result.get("size_bytes"),
        "progress": 1.0,
        "started_at": now,
        "completed_at": now,
        "raw": raw,
    }


def _promote_candidate_to_import_ready(db_path, provider_id, evidence, match, staging_root, *, now):
    source_path = Path(evidence["source_path"]).expanduser().resolve()
    staging_root_path = Path(staging_root).expanduser().resolve()
    target = _staging_target_path(staging_root_path, evidence, match)
    if not _path_under(target, staging_root_path):
        return {"ok": False, "reason": "staging_target_outside_root", "staged_path": str(target)}
    if source_path == target:
        return {"ok": False, "reason": "source_path_equals_staging_target", "staged_path": str(target)}
    copy_result = _copy_to_staging(source_path, target)
    if not copy_result.get("ok"):
        return copy_result
    attempt = _promotion_attempt_payload(provider_id, evidence, match, copy_result, now=now)
    attempt_id = inkdrop_state.stable_id(
        "suwayomi_managed_folder_import_ready",
        match.get("queue_id"),
        attempt.get("candidate_identity"),
    )
    recorded = inkdrop_state.record_queue_source_attempt(
        db_path,
        match.get("queue_id"),
        attempt,
        attempt_id=attempt_id,
        started_at=now,
        completed_at=now,
    )
    if not recorded.get("ok"):
        return {
            "ok": False,
            "reason": recorded.get("reason") or "source_attempt_record_failed",
            "staged_path": copy_result.get("staged_path"),
            "copy_result": copy_result,
            "recorded": recorded,
        }
    return {
        "ok": True,
        "status": "already_staged_import_ready" if copy_result.get("idempotent") else "promoted_import_ready",
        "staged_path": copy_result.get("staged_path"),
        "copy_result": copy_result,
        "recorded": recorded,
        "attempt_id": recorded.get("attempt_id"),
    }


def _series_from_stem(stem):
    text = str(stem or "")
    text = re.sub(r"(?i)\b(?:vol(?:ume)?|v)\.?\s*\d+(?:\.\d+)?\b", " ", text)
    text = re.sub(r"(?i)\b(?:ch(?:apter)?|c)\.?\s*\d+(?:\.\d+)?\b", " ", text)
    text = re.sub(r"(?i)\b(?:digital|official|english|eng|en)\b", " ", text)
    text = re.sub(r"\[[^\]]+\]|\([^)]+\)", " ", text)
    text = re.sub(r"[_\-.]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _best_series_guess(path, root):
    try:
        rel = path.relative_to(root)
    except Exception:
        rel = path.name
    parts = list(rel.parts if hasattr(rel, "parts") else Path(str(rel)).parts)
    folder_candidates = []
    for part in parts[:-1]:
        clean = re.sub(r"[_\-.]+", " ", str(part or "")).strip()
        if inkdrop_sources.normalize_title(clean) not in GENERIC_FOLDER_NAMES:
            folder_candidates.append(clean)
    if folder_candidates:
        return folder_candidates[-1]
    return _series_from_stem(path.stem)


def parse_managed_folder_file(path, root):
    path = Path(path)
    root = Path(root)
    text = " ".join([path.stem, *path.parts])
    volume_match = re.search(r"(?i)\b(?:vol(?:ume)?|v)\.?\s*(\d+(?:\.\d+)?)\b", text)
    chapter_match = re.search(r"(?i)\b(?:ch(?:apter)?|c)\.?\s*(\d+(?:\.\d+)?)\b", text)
    language = ""
    if re.search(r"(?i)(?:^|[\W_])(english|eng|en)(?:$|[\W_])", text):
        language = "en"
    return {
        "source_path": str(path),
        "relative_path": str(path.relative_to(root)) if str(path).startswith(str(root)) else path.name,
        "series": _best_series_guess(path, root),
        "series_key": inkdrop_sources.normalize_title(_best_series_guess(path, root)),
        "volume": _number_text(volume_match.group(1)) if volume_match else "",
        "chapter": _number_text(chapter_match.group(1)) if chapter_match else "",
        "language": language,
        "extension": path.suffix.lower(),
    }


def _unit_from_query(query, raw_values):
    raw_values = [value for value in raw_values if isinstance(value, dict)]
    for raw in raw_values:
        unit = str(_first_text(raw.get("unitType"), raw.get("unit_type"), raw.get("unit"))).strip().lower()
        if unit in {"volume", "vol", "book_volume", "manga_volume"}:
            return "volume"
        if unit in {"chapter", "ch", "manga_chapter"}:
            return "chapter"
    if re.search(r"(?i)\bvol(?:ume)?\.?\s*\d+", str(query or "")):
        return "volume"
    return "chapter"


def _wanted_number(unit, issue_number, query, raw_values):
    raw_values = [value for value in raw_values if isinstance(value, dict)]
    keys = ("volume", "volume_number", "volumeNumber") if unit == "volume" else ("chapter", "chapter_number", "chapterNumber")
    for raw in raw_values:
        value = _first_text(*(raw.get(key) for key in keys))
        if value:
            return _number_text(value)
    if unit == "volume":
        match = re.search(r"(?i)\bvol(?:ume)?\.?\s*(\d+(?:\.\d+)?)\b", str(query or ""))
        if match:
            return _number_text(match.group(1))
    if unit == "chapter":
        match = re.search(r"(?i)\b(?:ch(?:apter)?|c)\.?\s*(\d+(?:\.\d+)?)\b", str(query or ""))
        if match:
            return _number_text(match.group(1))
    return _number_text(issue_number)


def _provider_config_from_db(db_path, provider_id=DEFAULT_PROVIDER_ID):
    if not db_path:
        return {"exists": False, "enabled": False, "settings": {}, "policy": {}}
    path = Path(db_path)
    if not path.exists():
        return {"exists": False, "enabled": False, "settings": {}, "policy": {}}
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except Exception:
        return {"exists": False, "enabled": False, "settings": {}, "policy": {}}
    try:
        row = con.execute(
            "select enabled, settings_json from provider_configs where id=? limit 1",
            (provider_id,),
        ).fetchone()
    except Exception:
        row = None
    finally:
        con.close()
    if not row:
        return {"exists": False, "enabled": False, "settings": {}, "policy": {}}
    settings = _json_loads(row[1], {})
    policy = settings.get("policy") if isinstance(settings, dict) and isinstance(settings.get("policy"), dict) else {}
    return {
        "exists": True,
        "enabled": bool(row[0]),
        "settings": settings if isinstance(settings, dict) else {},
        "policy": dict(policy),
    }


def managed_folder_automation_gate(db_path, provider_id=DEFAULT_PROVIDER_ID):
    config = _provider_config_from_db(db_path, provider_id)
    if not config.get("exists"):
        return {"ok": False, "reason": "provider_not_claimed", "provider_id": provider_id}
    if not config.get("enabled"):
        return {"ok": False, "reason": "provider_disabled", "provider_id": provider_id}
    policy = _merged_policy(db_path=db_path, provider_id=provider_id)
    if not bool(policy.get("suwayomi_folder_import_ready_enabled")):
        return {"ok": False, "reason": "import_ready_setting_disabled", "provider_id": provider_id}
    if str(policy.get("suwayomi_folder_copy_policy") or "copy_to_staging").strip().lower() != "copy_to_staging":
        return {"ok": False, "reason": "unsupported_copy_policy", "provider_id": provider_id}
    if str(policy.get("suwayomi_folder_cleanup_policy") or "keep_source_files").strip().lower() != "keep_source_files":
        return {"ok": False, "reason": "unsupported_cleanup_policy", "provider_id": provider_id}
    return {"ok": True, "provider_id": provider_id, "policy": policy}


def load_active_wanted_index(db_path, queue_ids=None):
    path = Path(db_path) if db_path else None
    if not path or not path.exists():
        return {}
    queue_ids = [str(value or "").strip() for value in (queue_ids or []) if str(value or "").strip()]
    queue_filter_sql = ""
    params = []
    if queue_ids:
        queue_filter_sql = "and q.id in (%s)" % ",".join("?" for _ in queue_ids)
        params.extend(queue_ids)
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            select q.id as queue_id,
                   q.query,
                   q.raw_json as queue_raw,
                   w.id as wanted_id,
                   w.raw_json as wanted_raw,
                   i.id as issue_id,
                   i.issue_number,
                   i.raw_json as issue_raw,
                   s.id as series_id,
                   s.title as series_title,
                   s.media_type
            from queue_items q
            join series s on s.id=q.series_id
            left join wanted_items w on w.id=q.wanted_id
            left join issues i on i.id=q.issue_id
            where q.active=1
              and lower(coalesce(s.media_type, '')) in ('manga', 'manhwa', 'manhua')
              {queue_filter_sql}
            """
            .format(queue_filter_sql=queue_filter_sql),
            params,
        ).fetchall()
    finally:
        con.close()
    index = {}
    for row in rows:
        raw_values = [
            _json_loads(row["queue_raw"], {}),
            _json_loads(row["wanted_raw"], {}),
            _json_loads(row["issue_raw"], {}),
        ]
        unit = _unit_from_query(row["query"], raw_values)
        number = _wanted_number(unit, row["issue_number"], row["query"], raw_values)
        series_key = inkdrop_sources.normalize_title(row["series_title"])
        if not series_key or not number:
            continue
        key = (series_key, unit, number)
        index.setdefault(key, []).append(
            {
                "queue_id": row["queue_id"],
                "wanted_id": row["wanted_id"],
                "issue_id": row["issue_id"],
                "series_id": row["series_id"],
                "series": row["series_title"],
                "unit": unit,
                "number": number,
                "query": row["query"],
            }
        )
    return index


def _candidate_matches(evidence, wanted_index):
    matches = []
    series_key = evidence.get("series_key") or ""
    if evidence.get("volume"):
        matches.extend(wanted_index.get((series_key, "volume", evidence.get("volume")), []))
    if evidence.get("chapter"):
        matches.extend(wanted_index.get((series_key, "chapter", evidence.get("chapter")), []))
    seen = set()
    out = []
    for row in matches:
        key = row.get("queue_id")
        if key and key not in seen:
            seen.add(key)
            out.append(row)
    return out


def audit_suwayomi_managed_folder(
    *,
    root=None,
    db_path=None,
    provider_id=DEFAULT_PROVIDER_ID,
    limit=None,
    min_age_seconds=None,
    import_ready_enabled=None,
    apply=False,
    queue_ids=None,
    now=None,
):
    overrides = {}
    if root:
        overrides["suwayomi_download_root"] = str(root)
    if min_age_seconds is not None:
        overrides["suwayomi_folder_min_age_seconds"] = int(min_age_seconds)
    if import_ready_enabled is not None:
        overrides["suwayomi_folder_import_ready_enabled"] = bool(import_ready_enabled)
    policy = _merged_policy(db_path=db_path, provider_id=provider_id, overrides=overrides)
    root_path = Path(policy.get("suwayomi_download_root") or "")
    now = float(now if now is not None else time.time())
    scan_limit = max(1, int(limit or policy.get("suwayomi_folder_max_scan_files") or 500))
    min_age = max(0, int(policy.get("suwayomi_folder_min_age_seconds") or 0))
    import_ready = bool(policy.get("suwayomi_folder_import_ready_enabled"))
    apply_enabled = bool(apply)
    copy_policy = str(policy.get("suwayomi_folder_copy_policy") or "copy_to_staging").strip().lower()
    cleanup_policy = str(policy.get("suwayomi_folder_cleanup_policy") or "keep_source_files").strip().lower()
    staging_root = str(policy.get("suwayomi_import_staging_root") or "").strip()
    allowed_extensions = _allowed_extensions(policy)
    wanted_index = load_active_wanted_index(db_path, queue_ids=queue_ids) if db_path else {}
    staging_block_reason = _staging_root_block_reason(staging_root, root_path) if apply_enabled and import_ready else ""
    result = {
        "ok": True,
        "provider_id": provider_id,
        "source_kind": "suwayomi_managed_folder_source",
        "dry_run": not apply_enabled,
        "apply": apply_enabled,
        "mutates_filesystem": bool(apply_enabled and import_ready),
        "mutates_database": bool(apply_enabled and import_ready),
        "root": str(root_path),
        "root_exists": root_path.exists(),
        "source_mode": policy.get("suwayomi_folder_mode") or "report_only",
        "import_ready_enabled": import_ready,
        "copy_policy": copy_policy,
        "cleanup_policy": cleanup_policy,
        "staging_root": staging_root,
        "staging_block_reason": staging_block_reason,
        "allowed_extensions": sorted(allowed_extensions),
        "min_age_seconds": min_age,
        "scan_limit": scan_limit,
        "queue_ids": [str(value or "").strip() for value in (queue_ids or []) if str(value or "").strip()],
        "scanned_files": 0,
        "candidate_count": 0,
        "matched_count": 0,
        "would_promote_import_ready_count": 0,
        "promoted_import_ready_count": 0,
        "already_staged_import_ready_count": 0,
        "promotion_blocked_count": 0,
        "blocked_count": 0,
        "candidates": [],
    }
    if apply_enabled and not import_ready:
        result["ok"] = False
        result["reason"] = "import_ready_setting_disabled"
        result["mutates_filesystem"] = False
        result["mutates_database"] = False
    elif apply_enabled and copy_policy != "copy_to_staging":
        result["ok"] = False
        result["reason"] = "unsupported_copy_policy"
        result["mutates_filesystem"] = False
        result["mutates_database"] = False
    elif apply_enabled and cleanup_policy != "keep_source_files":
        result["ok"] = False
        result["reason"] = "unsupported_cleanup_policy"
        result["mutates_filesystem"] = False
        result["mutates_database"] = False
    elif staging_block_reason:
        result["ok"] = False
        result["reason"] = staging_block_reason
        result["mutates_filesystem"] = False
        result["mutates_database"] = False
    if not root_path.exists():
        result["ok"] = False
        result["reason"] = "download_root_missing"
        return result
    files = []
    for path in root_path.rglob("*"):
        if not path.is_file():
            continue
        files.append(path)
        if len(files) >= scan_limit:
            break
    for path in files:
        result["scanned_files"] += 1
        evidence = parse_managed_folder_file(path, root_path)
        age_seconds = max(0, int(now - path.stat().st_mtime))
        reasons = []
        if evidence["extension"] not in allowed_extensions:
            reasons.append("extension_not_allowed")
        if _is_partial_path(path):
            reasons.append("partial_or_temp_file")
        if age_seconds < min_age:
            reasons.append("file_too_new")
        if not evidence.get("series"):
            reasons.append("series_not_inferred")
        if not evidence.get("volume") and not evidence.get("chapter"):
            reasons.append("unit_not_inferred")
        matches = [] if reasons else _candidate_matches(evidence, wanted_index)
        if wanted_index and not matches and not reasons:
            reasons.append("no_active_wanted_match")
        if not reasons and import_ready and len(matches) > 1:
            reasons.append("ambiguous_active_wanted_matches")
        status = "blocked" if reasons else ("would_promote_import_ready" if import_ready and matches else "matched_report_only")
        candidate = {
            **evidence,
            "size_bytes": path.stat().st_size,
            "age_seconds": age_seconds,
            "status": status,
            "block_reasons": reasons,
            "matched_wanted": matches[:3],
        }
        result["candidates"].append(candidate)
        if reasons:
            result["blocked_count"] += 1
        else:
            result["candidate_count"] += 1
            if matches:
                result["matched_count"] += 1
            if status == "would_promote_import_ready":
                result["would_promote_import_ready_count"] += 1
            if apply_enabled and result.get("ok") and status == "would_promote_import_ready" and len(matches) == 1:
                promotion = _promote_candidate_to_import_ready(
                    db_path,
                    provider_id,
                    evidence,
                    matches[0],
                    staging_root,
                    now=now,
                )
                candidate["promotion"] = promotion
                if promotion.get("ok"):
                    status = promotion.get("status") or "promoted_import_ready"
                    candidate["status"] = status
                    candidate["staged_path"] = promotion.get("staged_path")
                    if status == "already_staged_import_ready":
                        result["already_staged_import_ready_count"] += 1
                    else:
                        result["promoted_import_ready_count"] += 1
                else:
                    candidate["status"] = "promotion_blocked"
                    candidate.setdefault("block_reasons", []).append(promotion.get("reason") or "promotion_failed")
                    result["promotion_blocked_count"] += 1
    return result


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Dry-run a Suwayomi managed-folder source scan")
    parser.add_argument("--db-path", default="", help="Optional InkDrop state DB for read-only wanted matching")
    parser.add_argument("--root", default="", help="Suwayomi download root to scan")
    parser.add_argument("--provider-id", default=DEFAULT_PROVIDER_ID)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--queue-id", action="append", default=[], help="Restrict wanted matching to one or more queue ids")
    parser.add_argument("--min-age-seconds", type=int, default=None)
    parser.add_argument("--import-ready-enabled", action="store_true", help="Show would-promote status without mutating")
    parser.add_argument("--apply", action="store_true", help="Copy exact matches to staging and record import-ready rows; also requires import-ready enabled")
    parser.add_argument("--pretty", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    result = audit_suwayomi_managed_folder(
        root=args.root or None,
        db_path=args.db_path or None,
        provider_id=args.provider_id,
        limit=args.limit,
        min_age_seconds=args.min_age_seconds,
        import_ready_enabled=True if args.import_ready_enabled else None,
        apply=args.apply,
        queue_ids=args.queue_id,
    )
    print(json.dumps(result, indent=2 if args.pretty else None, sort_keys=True))
    return 0 if result.get("ok") is not False else 2


if __name__ == "__main__":
    raise SystemExit(main())
