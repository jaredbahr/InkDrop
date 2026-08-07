#!/usr/bin/env python3
"""Bundle InkDrop's own log files into one downloadable archive.

Backs a "Download logs" button on the System page: collects the ``*.log``
files (and rotated ``.log.1`` style siblings) under the runtime log
directory, keeps only the most recent tail of each file so one runaway log
cannot balloon the archive, and writes a zip with a manifest describing
exactly what was captured and where truncation happened.

Newest-modified logs are packed first, so if the total budget runs out it is
the stale files that get dropped, not the ones a support thread needs.

CLI for headless installs:

    python inkdrop_log_export.py --out /tmp
"""

from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))


import argparse
import io
import json
import time
import zipfile
from pathlib import Path

from core import inkdrop_runtime_config


EXPORT_SCHEMA = "inkdrop.log_export.v1"
DEFAULT_PER_FILE_CAP_BYTES = 8 * 1024 * 1024
DEFAULT_TOTAL_CAP_BYTES = 64 * 1024 * 1024


def collect_log_files(log_dir=None):
    """List log files newest-first without reading their contents."""
    root = Path(log_dir or inkdrop_runtime_config.log_dir())
    rows = []
    if not root.is_dir():
        return rows
    for path in root.glob("*.log*"):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        rows.append(
            {
                "name": path.name,
                "path": str(path),
                "size_bytes": int(stat.st_size),
                "modified_at": int(stat.st_mtime),
            }
        )
    rows.sort(key=lambda row: (row["modified_at"], row["name"]), reverse=True)
    return rows


def _tail_bytes(path, cap_bytes):
    """Read at most the last ``cap_bytes`` of a file."""
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > cap_bytes:
            handle.seek(size - cap_bytes)
        return handle.read(cap_bytes), size > cap_bytes


def log_archive_filename(now=None):
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    return f"inkdrop-logs-{stamp}.zip"


def build_log_archive_bytes(
    log_dir=None,
    per_file_cap_bytes=DEFAULT_PER_FILE_CAP_BYTES,
    total_cap_bytes=DEFAULT_TOTAL_CAP_BYTES,
):
    """Return ``(zip_bytes, manifest)`` for the current log directory.

    The manifest is also embedded in the zip as ``manifest.json``. Files that
    would push the archive past ``total_cap_bytes`` are listed in the
    manifest as skipped rather than silently absent.
    """
    per_file_cap_bytes = max(4096, int(per_file_cap_bytes))
    total_cap_bytes = max(per_file_cap_bytes, int(total_cap_bytes))
    manifest = {
        "schema": EXPORT_SCHEMA,
        "generated_at": int(time.time()),
        "log_dir": str(Path(log_dir or inkdrop_runtime_config.log_dir())),
        "per_file_cap_bytes": per_file_cap_bytes,
        "total_cap_bytes": total_cap_bytes,
        "files": [],
        "skipped": [],
    }
    budget = total_cap_bytes
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for row in collect_log_files(log_dir):
            path = Path(row["path"])
            if budget <= 0:
                manifest["skipped"].append({"name": row["name"], "reason": "total_cap_reached"})
                continue
            try:
                payload, truncated = _tail_bytes(path, min(per_file_cap_bytes, budget))
            except OSError as exc:
                manifest["skipped"].append({"name": row["name"], "reason": f"read_failed:{type(exc).__name__}"})
                continue
            budget -= len(payload)
            archive.writestr(f"logs/{row['name']}", payload)
            manifest["files"].append(
                {
                    "name": row["name"],
                    "size_bytes": row["size_bytes"],
                    "included_bytes": len(payload),
                    "truncated": bool(truncated or len(payload) < row["size_bytes"]),
                    "modified_at": row["modified_at"],
                }
            )
        archive.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
    return buffer.getvalue(), manifest


def write_log_archive(out_dir, log_dir=None, **caps):
    """Write the archive to ``out_dir`` and return ``(path, manifest)``."""
    payload, manifest = build_log_archive_bytes(log_dir=log_dir, **caps)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    dest = out / log_archive_filename(manifest["generated_at"])
    dest.write_bytes(payload)
    return dest, manifest


def main():
    parser = argparse.ArgumentParser(description="Export InkDrop log files as one zip archive.")
    parser.add_argument("--out", default=".", help="directory to write the archive into")
    parser.add_argument("--log-dir", default=None, help="override the runtime log directory")
    args = parser.parse_args()
    dest, manifest = write_log_archive(args.out, log_dir=args.log_dir)
    print(json.dumps({"ok": True, "archive": str(dest), "files": len(manifest["files"]), "skipped": len(manifest["skipped"])}, indent=2))


if __name__ == "__main__":
    main()
