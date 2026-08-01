#!/usr/bin/env python3
"""Backend operator contracts shared by InkDrop UI and automation.

This module is intentionally small and dependency-light. It describes what the
operator can act on, not how workers perform acquisition.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path


ACTIVITY_STAGES = {
    "queued",
    "searching",
    "candidates_found",
    "candidate_rejected",
    "handoff",
    "downloading",
    "queued_remotely",
    "completed_in_client",
    "validating",
    "importing",
    "writing_metadata",
    "reader_scan",
    "verifying",
    "retry_scheduled",
    "completed",
    "needs_user",
}

ACTIVITY_STAGE_ALIASES = {
    "source_wait": "queued_remotely",
    "provider_wait": "queued_remotely",
    "download": "downloading",
    "downloaded": "completed_in_client",
    "download_complete": "completed_in_client",
    "staged_file_ready": "completed_in_client",
    "preview_importable": "validating",
    "ready_import": "validating",
    "verification_pending": "verifying",
    "verified": "completed",
    "complete": "completed",
    "done": "completed",
    "retry_later": "retry_scheduled",
    "manual_review": "needs_user",
    "needs_you": "needs_user",
    "blocked": "needs_user",
}

AUTOMATIC_RETRY_STATES = {
    "queued",
    "searching",
    "source_wait",
    "downloading",
    "importing",
    "retry_later",
    "provider_wait",
}

AUTOMATIC_RETRY_STATUSES = {
    "no_candidate",
    "no_candidate_retry",
    "retry_scheduled",
    "provider_unavailable",
    "provider_wait",
    "download_failed_retry",
    "transfer_failed",
    "transfer_stale_unknown",
    "import_busy",
    "verification_pending",
}

HUMAN_REASON_ACTIONS = {
    "ambiguous_candidate": ("approve_candidate", "reject_candidate"),
    "ambiguous_trusted_candidate": ("approve_candidate", "reject_candidate"),
    "alias_required": ("choose_alias", "search_again"),
    "blocked": ("resolve_destination", "ignore_wanted_item"),
    "failed": ("repair_identity", "ignore_wanted_item"),
    "identity_mismatch": ("repair_identity", "ignore_wanted_item"),
    "destination_conflict": ("resolve_destination", "ignore_wanted_item"),
    "pack_member_ambiguous": ("choose_pack_member", "reject_candidate"),
    "unsafe_destination": ("resolve_destination", "ignore_wanted_item"),
    "manual_source_candidate": ("approve_candidate", "reject_candidate"),
    "manual_review": ("approve_candidate", "reject_candidate"),
}


def _clean(value):
    return str(value or "").strip()


def _lower(value):
    return _clean(value).lower()


def _listish(value):
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value if _clean(item)]
    text = _clean(value)
    if not text:
        return []
    return [part.strip() for part in re.split(r"[,|]", text) if part.strip()]


def normalize_activity_stage(value):
    stage = re.sub(r"[^a-z0-9]+", "_", _lower(value)).strip("_")
    stage = ACTIVITY_STAGE_ALIASES.get(stage, stage)
    return stage if stage in ACTIVITY_STAGES else "queued"


def manual_review_contract(row):
    """Return the canonical human-decision contract for a queue/review row.

    Rows that automation can safely retry are explicitly not eligible for
    Manual Review. UI should show those in Wanted/Activity instead.
    """
    row = row if isinstance(row, dict) else {}
    state = _lower(row.get("state") or row.get("queue_state"))
    reason_text = _lower(
        row.get("reason_code")
        or row.get("review_reason")
        or row.get("reason")
        or row.get("failure_reason")
        or row.get("last_event")
        or state
    )
    reason_code = re.sub(r"[^a-z0-9]+", "_", reason_text).strip("_")
    raw_actions = _listish(row.get("available_actions"))
    if raw_actions:
        actions = raw_actions
    elif state == "needs_you" and reason_code == "manual_review_required":
        # This is the durable queue event emitted when InkDrop has intentionally
        # paused a candidate for a human decision. Require both the queue state
        # and the canonical event so unrelated needs_you rows remain excluded.
        actions = list(HUMAN_REASON_ACTIONS["manual_review"])
    else:
        actions = list(HUMAN_REASON_ACTIONS.get(reason_code, ()))

    retry_eligible = bool(row.get("retry_eligible"))
    automation_will_retry = bool(
        row.get("automation_will_retry")
        or row.get("automatic_retry")
        or row.get("next_retry")
        or row.get("next_retry_at")
        or state in AUTOMATIC_RETRY_STATES
        or reason_code in AUTOMATIC_RETRY_STATUSES
        or "retry" in reason_code
    )
    provider_wait = (
        state == "provider_wait"
        or reason_code in {"provider_wait", "provider_unavailable", "provider_limited"}
        or "provider wait" in reason_text
        or "waiting on provider" in reason_text
        or "provider unavailable" in reason_text
    )
    no_candidate = (
        reason_code in {"no_candidate", "no_candidate_yet", "no_safe_source", "searched_no_candidates"}
        or "no safe candidate" in reason_text
        or "no candidate" in reason_text
        or "no actionable candidate" in reason_text
    )
    source_failed = (
        reason_code in {"source_search_failed", "source_search_error", "download_api_error"}
        or ("source" in reason_text and "failed" in reason_text)
        or "download api error" in reason_text
    )
    recoverable_import = reason_code in {
        "invalid_archive_retry",
        "bad_archive_retry",
        "import_busy",
        "verification_pending",
        "download_failed_retry",
    } or ("archive" in reason_text and "retry" in reason_text)
    automatic_only = bool(
        provider_wait
        or no_candidate
        or source_failed
        or recoverable_import
        or (automation_will_retry and not actions)
        or (retry_eligible and not actions)
    )
    eligible = bool(actions) and not automatic_only
    if eligible and not actions:
        eligible = False
    safe_default = "automation_retry" if automatic_only or automation_will_retry else "leave_unresolved"
    recommended_action = _clean(row.get("recommended_action") or row.get("next_action"))
    if not recommended_action:
        if eligible:
            recommended_action = {
                "approve_candidate": "Approve or reject the candidate.",
                "choose_alias": "Choose the correct title alias.",
                "repair_identity": "Repair the series or issue identity.",
                "choose_pack_member": "Choose the matching pack member.",
                "resolve_destination": "Resolve the destination conflict.",
            }.get(actions[0], "Review and choose an action.")
        else:
            recommended_action = "No manual action. InkDrop will continue automatically."
    plain_reason = _clean(row.get("plain_language_reason") or row.get("activity_summary") or row.get("detail"))
    if not plain_reason:
        plain_reason = reason_code.replace("_", " ") if reason_code else ("Manual review required" if eligible else "Automatic workflow")
    review_id = _clean(
        row.get("review_id")
        or row.get("id")
        or row.get("queue_id")
        or row.get("wanted_id")
        or row.get("candidate_id")
    )
    now = time.time()
    return {
        "review_id": review_id,
        "eligible": eligible,
        "reason_code": reason_code or ("manual_review" if eligible else "automatic"),
        "reason": plain_reason,
        "recommended_action": recommended_action,
        "available_actions": actions if eligible else [],
        "evidence_summary": _clean(row.get("evidence_summary") or row.get("activity_summary") or row.get("last_event")),
        "safe_default": safe_default,
        "wanted_id": row.get("wanted_id"),
        "queue_id": row.get("queue_id") or row.get("id"),
        "candidate_id": row.get("candidate_id") or row.get("source_attempt_id"),
        "created_at": row.get("created_at") or now,
        "updated_at": row.get("updated_at") or now,
        "excluded_reason": None if eligible else (
            "automatic_retry"
            if automation_will_retry or retry_eligible
            else ("provider_wait" if provider_wait else ("no_candidate" if no_candidate else "no_meaningful_action"))
        ),
    }


ADVANCED_KEY_PATTERNS = (
    "raw_json",
    "json",
    "handoff",
    "byte",
    "cap",
    "secret_ref",
    "registry",
    "developer",
    "timeout",
    "budget",
    "stale",
    "cooldown",
)


def setting_contract(setting, field_schema=None):
    setting = setting if isinstance(setting, dict) else {}
    key = _clean(setting.get("key") or setting.get("name"))
    schema = dict(field_schema or setting.get("field_schema") or {})
    kind = schema.get("kind") or schema.get("value_type") or "string"
    advanced = bool(setting.get("advanced")) or any(token in key.lower() for token in ADVANCED_KEY_PATTERNS)
    secret = bool(setting.get("secret") or setting.get("write_only") or "secret" in key.lower() or "api_key" in key.lower())
    choices = schema.get("choices") or setting.get("choices") or setting.get("options") or []
    return {
        "key": key,
        "label": setting.get("label") or key,
        "description": setting.get("description") or setting.get("help_text") or "",
        "help_text": setting.get("help_text") or setting.get("description") or "",
        "value_type": kind,
        "value": None if secret else setting.get("value"),
        "default": setting.get("default", schema.get("default")),
        "recommended": setting.get("recommended", schema.get("recommended")),
        "editable": bool(setting.get("editable", True)),
        "advanced": advanced,
        "secret": secret,
        "write_only": secret,
        "required": bool(setting.get("required", False)),
        "choices": list(choices) if isinstance(choices, (list, tuple)) else [],
        "minimum": schema.get("min") if "min" in schema else schema.get("minimum"),
        "maximum": schema.get("max") if "max" in schema else schema.get("maximum"),
        "units": setting.get("units") or schema.get("units"),
        "validation_pattern": setting.get("validation_pattern") or schema.get("pattern"),
        "restart_required": bool(setting.get("restart_required", False)),
        "dangerous": bool(setting.get("dangerous", False)),
        "capability_dependency": setting.get("capability_dependency"),
        "disabled_reason": setting.get("disabled_reason"),
        "storage": setting.get("storage") or "sqlite",
        "field_schema": schema,
    }


def download_client_registry():
    implemented = {
        "qbittorrent": {
            "display_name": "qBittorrent",
            "protocols": ["torrent"],
            "certification_tier": "implemented",
            "capabilities": {
                "test": True,
                "add_grab": True,
                "polling": True,
                "progress_eta": True,
                "completed_path": True,
                "cancellation": False,
                "import_integration": True,
            },
        },
        "sabnzbd": {
            "display_name": "SABnzbd",
            "protocols": ["usenet"],
            "certification_tier": "implemented",
            "capabilities": {
                "test": True,
                "add_grab": True,
                "polling": True,
                "progress_eta": True,
                "completed_path": True,
                "cancellation": False,
                "import_integration": True,
            },
        },
        "slskd": {
            "display_name": "SLSKD",
            "protocols": ["soulseek"],
            "certification_tier": "implemented",
            "capabilities": {
                "test": True,
                "add_grab": True,
                "polling": True,
                "progress_eta": False,
                "completed_path": True,
                "cancellation": False,
                "import_integration": True,
            },
        },
        "transmission": {
            "display_name": "Transmission",
            "protocols": ["torrent"],
            "certification_tier": "beta",
            "capabilities": {
                "test": True,
                "add_grab": True,
                "polling": True,
                "progress_eta": True,
                "completed_path": True,
                "cancellation": True,
                "import_integration": True,
            },
        },
        "deluge": {
            "display_name": "Deluge",
            "protocols": ["torrent"],
            "certification_tier": "beta",
            "capabilities": {
                "test": True,
                "add_grab": True,
                "polling": True,
                "progress_eta": True,
                "completed_path": True,
                "cancellation": True,
                "import_integration": True,
            },
        },
        "nzbget": {
            "display_name": "NZBGet",
            "protocols": ["usenet"],
            "certification_tier": "beta",
            "capabilities": {
                "test": True,
                "add_grab": True,
                "polling": True,
                "progress_eta": True,
                "completed_path": True,
                "cancellation": True,
                "import_integration": True,
            },
        },
        "utorrent": {
            "display_name": "uTorrent",
            "protocols": ["torrent"],
            "certification_tier": "beta",
            "capabilities": {"test": True, "add_grab": True, "polling": True, "progress_eta": True, "completed_path": True, "cancellation": True, "import_integration": True},
        },
        "rtorrent": {
            "display_name": "rTorrent",
            "protocols": ["torrent"],
            "certification_tier": "beta",
            "capabilities": {"test": True, "add_grab": True, "polling": True, "progress_eta": True, "completed_path": True, "cancellation": True, "import_integration": True},
        },
    }
    requested = {}
    rows = []
    for key, item in implemented.items():
        rows.append(
            {
                "client_id": key,
                "client_type": key,
                "display_name": item["display_name"],
                "implemented": True,
                "certification_tier": item["certification_tier"],
                "supported_protocols": item["protocols"],
                "configuration_schema": "inkdrop.download_client_config.v1",
                "disabled_reason": None,
                **item["capabilities"],
            }
        )
    for key, (label, protocols) in requested.items():
        rows.append(
            {
                "client_id": key,
                "client_type": key,
                "display_name": label,
                "implemented": False,
                "certification_tier": "planned",
                "supported_protocols": protocols,
                "configuration_schema": "inkdrop.download_client_config.v1",
                "test": False,
                "add_grab": False,
                "polling": False,
                "progress_eta": False,
                "completed_path": False,
                "cancellation": False,
                "import_integration": False,
                "disabled_reason": "Adapter not implemented yet.",
            }
        )
    return {
        "schema": "inkdrop.download_clients.v1",
        "clients": rows,
        "implemented": [row["client_id"] for row in rows if row["implemented"]],
        "planned": [row["client_id"] for row in rows if not row["implemented"]],
    }


def storage_metric(key, path, label=None):
    target = Path(path)
    if not target.exists():
        return {
            "key": key,
            "label": label or key,
            "path": str(target),
            "available": False,
            "status": "unavailable",
            "error": "path does not exist",
            "free_bytes": None,
            "total_bytes": None,
            "used_bytes": None,
            "used_percent": None,
        }
    try:
        usage = shutil.disk_usage(target)
    except Exception as exc:
        return {
            "key": key,
            "label": label or key,
            "path": str(target),
            "available": False,
            "status": "unavailable",
            "error": str(exc),
            "free_bytes": None,
            "total_bytes": None,
            "used_bytes": None,
            "used_percent": None,
        }
    total = int(usage.total)
    free = int(usage.free)
    used = max(0, total - free)
    used_percent = round((used / total) * 100, 2) if total else None
    return {
        "key": key,
        "label": label or key,
        "path": str(target),
        "available": True,
        "status": "ok",
        "free_bytes": free,
        "total_bytes": total,
        "used_bytes": used,
        "used_percent": used_percent,
    }


def maintenance_catalog():
    def row(
        action_id,
        display_name,
        category,
        description,
        confirmation_required,
        expected_effect,
        permission,
        *,
        current_state="available",
        last_run=None,
        last_run_status=None,
        unavailable_reason=None,
    ):
        return {
            "id": action_id,
            "display_name": display_name,
            "category": category,
            "description": description,
            "confirmation_required": bool(confirmation_required),
            "expected_effect": expected_effect,
            "permission": permission,
            "auth_requirement": permission,
            "last_run": last_run,
            "last_run_status": last_run_status,
            "current_state": current_state,
            "state": current_state,
            "unavailable_reason": unavailable_reason,
        }

    return {
        "schema": "inkdrop.maintenance_actions.v1",
        "actions": [
            row(
                "state_summary",
                "Read state summary",
                "safe_read_only",
                "Reads current InkDrop state without running workers.",
                False,
                "No state changes.",
                "read",
            ),
            row(
                "queue_maintenance",
                "Queue maintenance",
                "routine",
                "Retires stale active rows and refreshes queue bookkeeping.",
                False,
                "May update queue/task status rows.",
                "operator",
            ),
            row(
                "full_reconciliation",
                "Full reconciliation",
                "advanced",
                "Runs broader state reconciliation and can take longer.",
                True,
                "Updates derived state from current filesystem/provider evidence.",
                "admin",
            ),
            row(
                "delete_files",
                "Delete media files",
                "destructive_high_impact",
                "Deletes managed media files only after explicit confirmation.",
                True,
                "Irreversible file deletion unless external backups exist.",
                "admin",
            ),
        ],
    }


def bounded_activity_event(row):
    row = row if isinstance(row, dict) else {}
    stage = normalize_activity_stage(row.get("display_phase") or row.get("state") or row.get("status") or "queued")
    transfer = row.get("transfer") if isinstance(row.get("transfer"), dict) else None
    return {
        "queue_id": row.get("queue_id") or row.get("id"),
        "wanted_id": row.get("wanted_id"),
        "series_id": row.get("series_id"),
        "issue_id": row.get("issue_id"),
        "stage": stage,
        "provider": row.get("provider") or row.get("provider_id"),
        "source": row.get("source") or row.get("current_source"),
        "source_ladder_position": row.get("source_ladder_position"),
        "query_summary": row.get("query") or row.get("title"),
        "candidate_counts": row.get("candidate_counts") or {},
        "rejection_reason_counts": row.get("rejection_reason_counts") or {},
        "downloader_client": (transfer or {}).get("client") or row.get("download_client"),
        "transfer_status": (transfer or {}).get("transfer_state") or row.get("transfer_status") or row.get("status"),
        "transfer": transfer,
        "import_stage": (transfer or {}).get("import_stage") or row.get("import_stage"),
        "next_source": row.get("next_source"),
        "next_retry": row.get("next_retry") or row.get("next_retry_at"),
        "needs_user": bool(row.get("needs_user") or row.get("manual_review_actionable")),
        "timestamp": row.get("activity_at") or row.get("updated_at") or row.get("created_at"),
    }
