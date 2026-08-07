#!/usr/bin/env python3
"""InkDrop acquisition-provider compatibility adapter.

This module is the narrow boundary between the InkDrop web/runtime layer and
the older acquisition helper implementation. Keep legacy module names here so
operator entrypoints do not need to import them directly.
"""

from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))


import importlib.util
import os
from pathlib import Path


def script_path(name: str, *, env_var: str | None = None, fallback: str | None = None) -> Path:
    configured = os.environ.get(env_var or "")
    if configured:
        return Path(configured)
    local = Path(__file__).resolve().with_name(name)
    if local.exists():
        return local
    if fallback:
        return Path(fallback)
    return local


ACQUIRE_MODULE_SCRIPT = script_path(
    "inkdrop_acquire.py",
    env_var="INKDROP_ACQUIRE_MODULE_SCRIPT",
)


def load_acquire_module():
    spec = importlib.util.spec_from_file_location("inkdrop_legacy_acquire", ACQUIRE_MODULE_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load acquisition helper: {ACQUIRE_MODULE_SCRIPT}")
    acquire = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(acquire)
    return acquire


def prowlarr_search(query, media_type, indexer_ids=None, limit=10, timeout_seconds=None):
    return load_acquire_module().prowlarr_search(
        query,
        media_type,
        indexer_ids=indexer_ids,
        limit=limit,
        timeout_seconds=timeout_seconds,
    )


def main():
    """Run the legacy-compatible acquisition CLI behind the InkDrop boundary."""
    return load_acquire_module().main()


if __name__ == "__main__":
    main()
