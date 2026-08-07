"""Canonical library, unit naming, folder, and reader-completion contracts."""

import re
from pathlib import Path


MANGA_MEDIA_TYPES = {"manga", "manhwa", "manhua"}
COMIC_MEDIA_TYPES = {"comic", "comics", "western_comic"}
UNIT_TYPES = {"issue", "chapter", "volume", "collected", "pack_member"}


def canonical_library_classification(record):
    """Classify from durable work metadata; provider and filename are never evidence."""
    record = record if isinstance(record, dict) else {}
    evidence = []
    for key in ("canonical_media_type", "work_media_type", "series_media_type", "media_type"):
        value = str(record.get(key) or "").strip().lower()
        if value:
            evidence.append((key, value))
    normalized = {"manga" if value in MANGA_MEDIA_TYPES else "comics" if value in COMIC_MEDIA_TYPES else value for _, value in evidence}
    if len(normalized) > 1:
        return {"ok": False, "library_type": "unknown", "reason": "conflicting_durable_media_identity", "evidence": evidence}
    library_type = next(iter(normalized), "unknown")
    return {
        "ok": library_type != "unknown",
        "library_type": library_type,
        "media_type": "manga" if library_type == "manga" else "comic" if library_type == "comics" else library_type,
        "reason": "durable_media_identity" if evidence else "missing_durable_media_identity",
        "evidence": evidence,
    }


def normalize_number(value):
    text = str(value or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"\d+\.0+", text):
        return str(int(float(text)))
    return text


def canonical_unit_identity(record):
    record = record if isinstance(record, dict) else {}
    raw_type = str(record.get("unit_type") or record.get("source_unit") or "").strip().lower().replace("-", "_")
    aliases = {
        "collection": "collected", "collected_edition": "collected",
        "issue_member": "pack_member", "pack": "pack_member",
    }
    unit_type = aliases.get(raw_type, raw_type)
    if unit_type not in UNIT_TYPES:
        return {"ok": False, "unit_type": "unknown", "number": "", "reason": "missing_explicit_unit_type"}
    key = {
        "issue": "issue_number",
        "chapter": "chapter_number",
        "volume": "volume_number",
        "collected": "collected_number",
        "pack_member": "pack_member_number",
    }[unit_type]
    number = normalize_number(record.get(key))
    if not number:
        fallback = record.get("normalized_number") if unit_type in {"issue", "chapter", "collected", "pack_member"} else None
        number = normalize_number(fallback)
    return {"ok": bool(number), "unit_type": unit_type, "number": number, "number_source": key, "reason": "explicit_unit_identity" if number else "missing_unit_number"}


def format_number(value, width):
    text = normalize_number(value)
    if re.fullmatch(r"\d+", text):
        return str(int(text)).zfill(width)
    return text


def canonical_filename(series_title, unit, extension=".cbz"):
    unit = unit if isinstance(unit, dict) else {}
    series = str(series_title or "Unknown Series").strip() or "Unknown Series"
    unit_type = unit.get("unit_type")
    number = unit.get("number")
    if not unit.get("ok") or unit_type not in UNIT_TYPES:
        raise ValueError("explicit canonical unit identity is required")
    labels = {
        "issue": f"#{format_number(number, 3)}",
        "chapter": f"c{format_number(number, 3)}",
        "volume": f"v{format_number(number, 2)}",
        "collected": f"Collected Edition {format_number(number, 2)}",
        "pack_member": f"Pack Member {format_number(number, 3)}",
    }
    ext = str(extension or ".cbz")
    ext = ext if ext.startswith(".") else f".{ext}"
    return f"{series} {labels[unit_type]}{ext.lower()}"


def stable_folder_decision(proposed_folder, persisted_folder=None, reader_observed_folder=None):
    proposed = Path(str(proposed_folder or "Unknown")).name
    persisted = Path(str(persisted_folder or "")).name if persisted_folder else ""
    observed = Path(str(reader_observed_folder or "")).name if reader_observed_folder else ""
    selected = persisted or observed or proposed
    drift = bool(persisted and proposed and persisted.casefold() != proposed.casefold())
    collision = bool(persisted and observed and persisted.casefold() != observed.casefold())
    return {
        "series_folder": selected,
        "persisted": bool(persisted),
        "metadata_drift": drift,
        "operator_review_required": collision,
        "reason": "persisted_folder_identity" if persisted else "reader_observed_folder" if observed else "initial_folder_identity",
    }


def reader_expectation(record, *, filename, series_folder):
    classification = canonical_library_classification(record)
    unit = canonical_unit_identity(record)
    work_id = str(record.get("work_id") or record.get("native_series_id") or "").strip()
    reader_series_id = str(record.get("kavita_series_id") or record.get("reader_series_id") or "").strip()
    reader_library_id = str(record.get("kavita_library_id") or record.get("reader_library_id") or "").strip()
    return {
        "ok": bool(
            classification.get("ok") and unit.get("ok") and work_id and reader_series_id
            and reader_library_id and filename and series_folder
        ),
        "library_type": classification.get("library_type"),
        "work_id": work_id,
        "canonical_series_id": str(record.get("canonical_series_id") or work_id),
        "reader_series_id": reader_series_id,
        "reader_library_id": reader_library_id,
        "unit_type": unit.get("unit_type"),
        "unit_number": unit.get("number"),
        "series_folder": str(series_folder),
        "filename": str(filename),
    }


def completion_projection(record):
    record = record if isinstance(record, dict) else {}
    reader_configured = bool(record.get("reader_configured"))
    reader_required = bool(record.get("reader_required"))
    imported = bool(record.get("imported"))
    scan_requested = bool(record.get("reader_scan_requested"))
    visibility = str(record.get("reader_visibility_status") or "").strip().lower()
    if not imported:
        state = "artifact_verified" if record.get("artifact_verified") else "downloaded" if record.get("downloaded") else "pending"
    elif reader_required and not reader_configured:
        state = "reader_visibility_failed"
    elif not reader_configured:
        state = "imported"
    elif visibility in {"library_visible", "visible"}:
        state = "reader_visible"
    elif visibility in {
        "failed", "scan_timeout", "timeout", "missing_file", "wrong_library",
        "wrong_file", "wrong_series_folder", "duplicate_series",
    }:
        state = "reader_visibility_failed"
    elif scan_requested or visibility in {"pending", "not_visible"}:
        state = "reader_scan_pending"
    else:
        state = "imported"
    complete = bool(imported and (not reader_required or state == "reader_visible"))
    return {"state": state, "complete": complete, "reader_configured": reader_configured, "reader_required": reader_required}
