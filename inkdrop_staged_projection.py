"""Read-only classification of abandoned staged import artifacts."""

from __future__ import annotations

import os
import time
from pathlib import Path

import inkdrop_runtime_config


STALE_SECONDS = 6 * 60 * 60
MAX_ATTEMPTS = 12
STAGED_STATUSES = {
    "staged",
    "staged_file_ready",
    "preview_importable",
    "ready_import",
    "downloaded",
}
ARCHIVE_EXTENSIONS = {".cbz", ".cbr", ".pdf"}


def _text(value):
    return str(value or "").strip()


def _number(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _roots():
    values = (
        os.environ.get("INKDROP_UNMATCHED_DOWNLOAD_ROOT"),
        os.environ.get("INKDROP_DIRECT_DOWNLOAD_ROOT"),
        os.environ.get("INKDROP_QBITTORRENT_DOWNLOAD_ROOT"),
        os.environ.get("INKDROP_DOWNLOAD_STAGING_ROOT"),
        os.environ.get("INKDROP_STAGING_DIR"),
        str(inkdrop_runtime_config.staging_dir()),
        "/downloads",
    )
    roots = []
    for value in values:
        if not _text(value):
            continue
        try:
            root = Path(value).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if root.is_dir() and root not in roots:
            roots.append(root)
    return roots


def _within_roots(path, roots):
    return any(_relative_to(path, root) for root in roots)


def _relative_to(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _identity_matches(attempt, row):
    raw = attempt.get("raw") if isinstance(attempt.get("raw"), dict) else {}
    for key in ("wanted_id", "series_id", "issue_id"):
        actual = _text(attempt.get(key) or raw.get(key))
        expected = _text(row.get(key))
        if actual and expected and actual != expected:
            return False
    return True


def _attempt_paths(attempt):
    raw = attempt.get("raw") if isinstance(attempt.get("raw"), dict) else {}
    values = (
        attempt.get("save_path"),
        raw.get("staged_path"),
        raw.get("source_path"),
        raw.get("detected_path"),
        raw.get("local_path"),
        raw.get("download_path"),
    )
    paths = []
    for value in values:
        # Persisted JSON evidence must be an exact scalar path. Never stringify
        # objects/lists, traverse nested payloads, or interpret a directory as a
        # request to discover files recursively.
        if not isinstance(value, str):
            continue
        path = value.strip()
        if not path or len(path) > 4096 or "\x00" in path:
            continue
        paths.append(path)
    return tuple(paths)


def classify(row, attempts, queue_raw=None, now=None):
    """Return safe stalled-import evidence, or empty when evidence is insufficient."""
    row = row if isinstance(row, dict) else {}
    queue_raw = queue_raw if isinstance(queue_raw, dict) else {}
    if not bool(row.get("active")) or _text(row.get("state") or row.get("queue_state")).lower() != "importing":
        return {}
    if row.get("download_task") or row.get("latest_import") or row.get("task_id") or row.get("import_id"):
        return {}
    roots = _roots()
    if not roots:
        return {}
    valid = {}
    for attempt in list(attempts or [])[:MAX_ATTEMPTS]:
        if not isinstance(attempt, dict) or not _identity_matches(attempt, row):
            continue
        status = _text(attempt.get("status")).lower()
        if status not in STAGED_STATUSES:
            continue
        for raw_path in _attempt_paths(attempt):
            if not _text(raw_path):
                continue
            try:
                artifact = Path(raw_path).expanduser().resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if not artifact.is_file() or artifact.suffix.lower() not in ARCHIVE_EXTENSIONS or not _within_roots(artifact, roots):
                continue
            sidecars = (Path(f"{artifact}.source.json"), artifact.with_suffix(".source.json"))
            sidecar = next((candidate for candidate in sidecars if candidate.is_file() and _within_roots(candidate.resolve(), roots)), None)
            if sidecar is None:
                continue
            valid[str(artifact)] = (attempt, artifact)
    if len(valid) != 1:
        return {}
    attempt, artifact = next(iter(valid.values()))
    evidence_at = max(
        _number(attempt.get("completed_at")),
        _number(attempt.get("started_at")),
        _number(row.get("updated_at") or row.get("queue_updated_at")),
        _number(row.get("created_at") or row.get("queue_created_at")),
    )
    age_seconds = max(0.0, float(now or time.time()) - evidence_at) if evidence_at else None
    if age_seconds is None or age_seconds <= STALE_SECONDS:
        return {}
    return {
        "state": "stalled_import",
        "label": "Stalled import",
        "recoverable": True,
        "needs_attention": True,
        "ownership_state": "unclaimed_staged_artifact",
        "recovery_policy": "guarded_import_existing_artifact",
        "artifact_name": artifact.name,
        "artifact_exists": True,
        "sidecar_exists": True,
        "artifact_preserved": True,
        "source_attempt_id": attempt.get("id") or attempt.get("source_attempt_id"),
        "source_attempt_status": _text(attempt.get("status")).lower(),
        "age_seconds": age_seconds,
        "next_action": "Recover the existing staged file through guarded import; do not download again or discard the artifact",
    }
