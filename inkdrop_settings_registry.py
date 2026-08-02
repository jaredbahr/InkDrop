#!/usr/bin/env python3
"""Typed public settings contract for InkDrop.

Container/bootstrap values remain environment-owned. Normal operator policy is
stored in ``app_settings`` and validated here before it reaches SQLite.
"""

from __future__ import annotations

import os

import inkdrop_operator_contracts


RETIRED_SETTING_KEYS = frozenset({
    "automation.kapowarr_missing_fallback",
    "automation.kapowarr_path_fallback",
    "automation.kapowarr_completed_import_fallback",
})


BOOLEAN_KEYS = {
    "auth.external.enabled",
    "automation.default_auto_future",
    "automation.queue_mode",
    "automation.queue_watchdog_enabled",
    "import_lists.enabled",
    "media_management.apply_planned_path",
    "media_management.delete_empty_folders",
    "media_management.frontend_sync_after_import",
    "media_management.library_visibility_checks_enabled",
    "media_management.library_visibility_required",
    "media_management.remove_chapters_satisfied_by_volume",
    "media_management.rename_imported_files",
    "media_management.replace_illegal_characters",
    "media_management.unmonitor_deleted_issues",
    "media_management.use_series_folders",
    "metadata_files.prefer_provider_metadata",
    "metadata_files.write_comicinfo_xml",
    "quality.allow_non_english",
    "quality.allow_packs",
    "quality.allow_pdfs",
    "quality.allow_raw_image_page_folders",
    "setup.external_adapters_optional",
    "setup.local_folder_only_mode",
    "setup.public_network_warning_acknowledged",
    "ui.advanced_mode_default",
    "ui.relative_dates",
}

ARRAY_KEYS = {
    "auth.allowed_origins",
    "auth.external.trusted_proxies",
    "automation.protocol_order",
    "automation.source_order",
    "media_management.library_visibility_provider_order",
    "quality.blocked_release_terms",
    "quality.preferred_extensions",
}

NUMBER_SPECS = {
    "auth.password_min_length": {"min": 1, "max": 128, "integer": True},
    "auth.session_ttl_seconds": {"min": 300, "max": 2592000, "integer": True},
    "automation.queue_watchdog_slskd_stale_minutes": {
        "min": 5, "max": 1440, "integer": True, "units": "minutes", "default": 45, "recommended": 45,
    },
    "automation.queue_watchdog_slskd_never_started_hours": {
        "min": 1, "max": 336, "integer": False, "units": "hours", "default": 24, "recommended": 24,
    },
    "automation.queue_watchdog_handoff_stale_hours": {"min": 1, "max": 168, "integer": False},
    "automation.queue_watchdog_download_client_stale_hours": {"min": 1, "max": 336, "integer": False},
    "automation.queue_watchdog_retry_delay_minutes": {"min": 1, "max": 1440, "integer": True},
    "media_management.minimum_free_space_gb": {"min": 0, "max": 100000, "integer": False},
}

CHOICE_SPECS = {
    "auth.mode": ["built_in", "external", "built_in_or_external", "disabled"],
    "automation.manual_review_mode": ["exception_only"],
    "general.log_level": ["debug", "info", "warning", "error"],
    "import_lists.default_monitoring": ["monitored_future", "monitored_all", "unmonitored"],
    "import_lists.minimum_match_confidence": ["high", "medium"],
    "media_management.folder_completion_policy": ["folder_first", "folder_and_frontend"],
    "media_management.import_conflict_policy": ["skip_existing", "replace_lower_quality", "manual_review"],
    "media_management.root_folder_strategy": ["media_type", "series_override"],
    "metadata_files.apply_mode": ["proposed_then_import", "disabled"],
    "quality.preferred_language": ["english", "any"],
    "ui.theme": ["system_dark", "dark", "light"],
}

STRING_KEYS = {
    "auth.external.admin_group",
    "auth.external.group_header",
    "auth.external.identity_header",
    "general.public_release_safety",
    "media_management.comic_issue_format",
    "media_management.manga_chapter_format",
    "media_management.series_folder_format",
    "path.comic_root",
    "path.kavita_comic_root",
    "path.kavita_manga_root",
    "path.manga_root",
    "path.manual_comics_inbox",
    "path.manual_ebooks_inbox",
    "path.slskd_download_root",
    "path.slskd_incomplete_root",
}

PUBLIC_SETTING_KEYS = frozenset(
    BOOLEAN_KEYS | ARRAY_KEYS | set(NUMBER_SPECS) | set(CHOICE_SPECS) | STRING_KEYS
)


def is_defined_public_setting(key):
    return str(key or "").strip() in PUBLIC_SETTING_KEYS


def is_retired_setting(key):
    return str(key or "").strip() in RETIRED_SETTING_KEYS


def field_schema(key):
    key = str(key or "").strip()
    if key in BOOLEAN_KEYS:
        return {"kind": "boolean"}
    if key in ARRAY_KEYS:
        return {"kind": "array"}
    if key in NUMBER_SPECS:
        return {"kind": "number", **NUMBER_SPECS[key]}
    if key in CHOICE_SPECS:
        return {"kind": "choice", "choices": list(CHOICE_SPECS[key])}
    return {"kind": "string"}


def validate_value(key, value):
    schema = field_schema(key)
    kind = schema["kind"]
    if kind == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be true or false")
        return value
    if kind == "array":
        if not isinstance(value, list):
            raise ValueError(f"{key} must be a list")
        return [str(item).strip() for item in value if str(item or "").strip()]
    if kind == "number":
        if isinstance(value, bool):
            raise ValueError(f"{key} must be a number")
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a number") from None
        if parsed < schema["min"] or parsed > schema["max"]:
            raise ValueError(f"{key} must be between {schema['min']} and {schema['max']}")
        return int(parsed) if schema.get("integer") else parsed
    if kind == "choice":
        parsed = str(value or "").strip().lower()
        if parsed not in schema["choices"]:
            raise ValueError(f"{key} must be one of: {', '.join(schema['choices'])}")
        return parsed
    if isinstance(value, (dict, list)):
        raise ValueError(f"{key} must be text")
    return str(value if value is not None else "").strip()


def augment_setting(setting):
    item = dict(setting or {})
    item["field_schema"] = field_schema(item.get("key"))
    item["editable"] = True
    item["storage"] = "sqlite"
    item["restart_required"] = False
    item.update(inkdrop_operator_contracts.setting_contract(item, item["field_schema"]))
    return item


def classify_environment_name(name):
    """Classify a runtime variable without exposing its value."""
    name = str(name or "").strip().upper()
    if any(token in name for token in ("API_KEY", "PASSWORD", "TOKEN", "SECRET")):
        return "secret"
    if name in {"INKDROP_HOST", "INKDROP_PORT", "INKDROP_CONFIG_DIR", "INKDROP_STATE_DIR", "INKDROP_LOG_DIR", "INKDROP_CACHE_DIR", "INKDROP_BACKUP_DIR", "INKDROP_STAGING_DIR", "INKDROP_MANUAL_INBOX_DIR", "INKDROP_QUARANTINE_DIR", "INKDROP_EXTERNAL_NETWORK"}:
        return "container_bootstrap"
    if any(token in name for token in ("_ROOT", "_DB", "_CONFIG", "_SCRIPT", "_MODULE", "_COMPOSE", "_BIN_DIR", "_APP_DIR", "_PATH_MAPPINGS")):
        return "container_bootstrap"
    if any(token in name for token in ("TIMEOUT", "INTERVAL", "LIMIT", "MAX_", "MIN_", "COOLDOWN", "STALE", "RETRY", "BUDGET", "CONCURRENCY", "PROTECTED_")):
        return "advanced_tuning"
    if name.startswith("INKDROP_"):
        return "internal_only"
    return "unrelated"


def environment_contract(environ=None):
    env = os.environ if environ is None else environ
    rows = []
    counts = {}
    for name in sorted(key for key in env if str(key).startswith("INKDROP_")):
        classification = classify_environment_name(name)
        counts[classification] = counts.get(classification, 0) + 1
        rows.append(
            {
                "name": name,
                "classification": classification,
                "configured": bool(str(env.get(name) or "").strip()),
                "value_exposed": False,
                "storage": "environment",
                "restart_required": True,
            }
        )
    return {
        "schema": "inkdrop.runtime_environment.v1",
        "counts": counts,
        "variables": rows,
        "values_exposed": False,
    }
