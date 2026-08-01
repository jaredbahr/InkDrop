#!/usr/bin/env python3
"""Inventory InkDrop environment variables without reading their values."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import inkdrop_settings_registry
from tools import inkdrop_docker_context_manifest


ENV_PATTERN = re.compile(r"\bINKDROP_[A-Z0-9_]+\b")


def audit(root=ROOT):
    root = Path(root)
    manifest = inkdrop_docker_context_manifest.build_manifest(root)
    usage = {}
    for item in manifest.get("files") or []:
        relative = str(item.get("path") or "")
        path = root / relative
        if path.suffix.lower() not in {".py", ".sh", ".yml", ".yaml", ".example"} and path.name != "Dockerfile":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        for name in ENV_PATTERN.findall(text):
            usage.setdefault(name, set()).add(relative.replace("\\", "/"))
    variables = []
    counts = {}
    for name in sorted(usage):
        classification = inkdrop_settings_registry.classify_environment_name(name)
        counts[classification] = counts.get(classification, 0) + 1
        variables.append(
            {
                "name": name,
                "classification": classification,
                "files": sorted(usage[name]),
                "value_exposed": False,
            }
        )
    return {
        "schema": "inkdrop.config_surface_audit.v1",
        "ok": True,
        "variable_count": len(variables),
        "counts": counts,
        "variables": variables,
        "values_exposed": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = audit()
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"InkDrop config surface: {result['variable_count']} variables")
        for key, value in sorted(result["counts"].items()):
            print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
