#!/usr/bin/env python3
"""Prune folders that a file removal left empty.

Backs the ``media_management.delete_empty_folders`` setting. When a
quarantine, upgrade, or delete moves the last file out of a series folder,
the operator can opt to have the emptied folder chain removed instead of
letting hollow directories pile up under the library roots. The sweep walks
upward from the removed file, stops at the first directory that still holds
anything, and never touches the library root itself.

Deliberately dependency-light: callers decide whether the setting is on and
which root bounds the sweep; this module only does the safe walk.
"""

from __future__ import annotations

from pathlib import Path


def _resolved(path):
    try:
        return Path(path).resolve()
    except (OSError, RuntimeError):
        return None


def containing_root(path, roots):
    """Return the resolved library root that holds ``path``, or None.

    A path equal to a root returns None on purpose — sweeping needs at least
    one directory level between the file and the root.
    """
    resolved = _resolved(path)
    if resolved is None:
        return None
    for root in roots or ():
        text = str(root or "").strip()
        if not text:
            continue
        root_resolved = _resolved(text)
        if root_resolved is None or resolved == root_resolved:
            continue
        if root_resolved in resolved.parents:
            return root_resolved
    return None


def remove_empty_parents(removed_path, stop_root, dry_run=False):
    """Remove now-empty directories above ``removed_path``, up to ``stop_root``.

    ``stop_root`` itself is never removed. A directory that still holds any
    entry — file, subfolder, sidecar — ends the walk immediately, so only an
    unbroken chain of genuinely empty folders is pruned. Filesystem races
    (something appearing mid-walk, permissions) end the walk rather than
    erroring: leaving an empty folder behind is always acceptable.
    """
    report = {"ok": False, "removed": [], "reason": ""}
    target = _resolved(removed_path)
    root = _resolved(stop_root)
    if target is None or root is None:
        report["reason"] = "resolve_failed"
        return report
    if root not in target.parents:
        report["reason"] = "outside_root"
        return report
    pruned = set()
    current = target.parent
    while current != root and root in current.parents:
        try:
            if not current.is_dir():
                break
            if any(entry for entry in current.iterdir() if entry not in pruned):
                break
            if not dry_run:
                current.rmdir()
        except OSError:
            break
        pruned.add(current)
        report["removed"].append(str(current))
        current = current.parent
    report["ok"] = True
    report["reason"] = "swept" if report["removed"] else "nothing_empty"
    return report
