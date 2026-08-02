#!/usr/bin/env python3
"""Regression smoke for planned-path import inspection evidence precedence."""

from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSPECTOR_PATH = ROOT / "tests/inkdrop-planned-path-live-inspection.py"


def load_inspector():
    spec = importlib.util.spec_from_file_location("inkdrop_planned_path_live_inspection", INSPECTOR_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def fail(message):
    raise AssertionError(message)


def smoke_recorded_evidence_wins_over_live_preview():
    inspector = load_inspector()
    row = {
        "id": "import-recorded",
        "queue_id": "queue-recorded",
        "series": "Recorded Series",
        "issue_number": "1",
        "source_path": "/downloads/Recorded Series 001.cbz",
        "dest_path": "/library/Comics/Recorded Series (2026)/Recorded Series #001 (2026).cbz",
        "media_management_recorded_evidence": {
            "media_management_destination_decision": {
                "enabled": True,
                "override": False,
                "legacy_dest_path": "/downloads/Recorded Series 001.cbz",
                "selected_dest_path": "/library/Comics/Recorded Series (2026)/Recorded Series #001 (2026).cbz",
                "planned_path": "/library/Comics/Recorded Series (2026)/Recorded Series #001 (2026).cbz",
                "applied": True,
                "reason": "planned_path_selected",
            },
            "media_management_preview": {
                "root": "/library/Comics",
                "planned_path": "/library/Comics/Recorded Series (2026)/Recorded Series #001 (2026).cbz",
                "selected_import_dest_path": "/library/Comics/Recorded Series (2026)/Recorded Series #001 (2026).cbz",
                "planned_path_applied": True,
                "planned_path_apply_status": "selected",
            },
        },
        "media_management_preview": {
            "root": "/library/Comics",
            "planned_path": "/library/Comics/Renamed Series (2026)/Renamed Series #001 (2026).cbz",
            "selected_import_dest_path": "/library/Comics/Renamed Series (2026)/Renamed Series #001 (2026).cbz",
            "planned_path_applied": False,
            "planned_path_apply_status": "eligible",
        },
    }
    result = inspector.inspect_rows([row], min_applied=1)
    if not result.get("ok"):
        fail(f"recorded applied evidence was not accepted: {result}")
    if result.get("applied_rows") != 1 or result.get("eligible_not_applied_rows") != 0:
        fail(f"recorded evidence did not win over live preview: {result}")
    if result.get("recorded_ok") is not True or result.get("recorded_applied_rows") != 1:
        fail(f"recorded-only safety counters did not accept applied evidence: {result}")
    if result.get("evidence_source_counts") != {"recorded": 1}:
        fail(f"unexpected evidence source counts: {result}")


def smoke_live_preview_still_counts_without_recorded_evidence():
    inspector = load_inspector()
    row = {
        "id": "import-live",
        "queue_id": "queue-live",
        "series": "Live Series",
        "issue_number": "2",
        "source_path": "/downloads/Live Series 002.cbz",
        "dest_path": "/downloads/Live Series 002.cbz",
        "media_management_preview": {
            "root": "/library/Comics",
            "planned_path": "/library/Comics/Live Series (2026)/Live Series #002 (2026).cbz",
            "selected_import_dest_path": "/library/Comics/Live Series (2026)/Live Series #002 (2026).cbz",
            "planned_path_applied": False,
            "planned_path_apply_status": "eligible",
        },
    }
    result = inspector.inspect_rows([row], min_applied=0)
    if result.get("eligible_not_applied_rows") != 1:
        fail(f"live preview fallback did not preserve not-applied evidence: {result}")
    if result.get("recorded_ok") is not False or result.get("recorded_evidence_rows") != 0:
        fail(f"preview-only evidence should not satisfy recorded safety: {result}")
    if result.get("legacy_preview_only_rows") != 1 or result.get("legacy_preview_eligible_not_applied_rows") != 1:
        fail(f"preview-only debt counters were not reported: {result}")
    if result.get("evidence_source_counts") != {"live_preview": 1}:
        fail(f"unexpected live evidence source counts: {result}")


def main():
    smoke_recorded_evidence_wins_over_live_preview()
    smoke_live_preview_still_counts_without_recorded_evidence()
    print("PLANNED_PATH_INSPECTION_OK")


if __name__ == "__main__":
    main()
