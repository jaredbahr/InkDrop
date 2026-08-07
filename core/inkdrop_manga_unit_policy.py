#!/usr/bin/env python3
"""Manga unit-model/policy lookup tables, shared by inkdrop_completed_import
and inkdrop_web_config -- previously two byte-identical, unlinked copies that
were a live drift risk (a new unit model added to one without the other
would silently disagree between import-time enforcement and settings-page
display)."""

from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))


MANGA_UNIT_MODELS = {
    "volume",
    "chapter",
    "pack",
    "mixed_volume_preferred",
    "mixed_chapter_preferred",
    "unknown/manual",
}
MANGA_UNIT_POLICY_TO_MODEL = {
    "volume_only": "volume",
    "chapter_native": "chapter",
    "pack_only": "pack",
    "mixed_allowed": "mixed_volume_preferred",
    "mixed_volume_preferred": "mixed_volume_preferred",
    "mixed_chapter_preferred": "mixed_chapter_preferred",
    "manual_review": "unknown/manual",
    "unknown_manual": "unknown/manual",
    "unknown/manual": "unknown/manual",
}
MANGA_UNIT_MODEL_TO_POLICY = {
    "volume": "volume_only",
    "chapter": "chapter_native",
    "pack": "pack_only",
    "mixed_volume_preferred": "mixed_allowed",
    "mixed_chapter_preferred": "mixed_allowed",
    "unknown/manual": "manual_review",
}
MANGA_UNIT_POLICY_LABELS = {
    "volume_only": "Volume only",
    "chapter_native": "Chapter native",
    "pack_only": "Pack only",
    "mixed_allowed": "Volumes and chapters",
    "mixed_volume_preferred": "Volumes and chapters",
    "mixed_chapter_preferred": "Chapters and volumes",
    "manual_review": "Manual review",
}


def normalize_manga_unit_model(value):
    raw = str(value or "unknown/manual").strip().lower().replace("-", "_").replace(" ", "_")
    if raw in MANGA_UNIT_POLICY_TO_MODEL:
        return MANGA_UNIT_POLICY_TO_MODEL[raw]
    raw = raw.replace("unknown_manual", "unknown/manual")
    return raw if raw in MANGA_UNIT_MODELS else "unknown/manual"


def manga_unit_policy_for_model(model):
    return MANGA_UNIT_MODEL_TO_POLICY.get(normalize_manga_unit_model(model), "manual_review")


MANGA_UNIT_VOLUME_PREFERRED_MODELS = {"volume", "mixed_volume_preferred"}
MANGA_UNIT_CHAPTER_PREFERRED_MODELS = {"chapter", "mixed_chapter_preferred"}


def manga_unit_model_prefers_volume(model):
    return normalize_manga_unit_model(model) in MANGA_UNIT_VOLUME_PREFERRED_MODELS


def manga_unit_model_prefers_chapter(model):
    return normalize_manga_unit_model(model) in MANGA_UNIT_CHAPTER_PREFERRED_MODELS


def series_manga_unit_override(series_row):
    """Read an explicit per-series manga-unit override from a series row's
    raw_json, if one has ever been set. Returns "" when absent -- callers
    must treat that as "no override, use the default behavior", not as any
    particular model, since "unknown/manual" is a real, distinct value a
    user can also explicitly choose.
    """
    row = series_row if isinstance(series_row, dict) else {}
    raw = row.get("raw_json")
    if isinstance(raw, str):
        try:
            import json as _json
            raw = _json.loads(raw or "{}")
        except (TypeError, ValueError):
            raw = {}
    raw = raw if isinstance(raw, dict) else {}
    value = raw.get("manga_unit_model_override")
    if value in (None, ""):
        return ""
    return normalize_manga_unit_model(value)
