#!/usr/bin/env python3
"""Bundle InkDrop's own log files into one redacted, downloadable archive.

Backs the "Download logs only" button on the System page: the logs half of
the support bundle, without the configuration and diagnostics members. It is
the same archive a support thread would get, narrowed.

Collection and redaction both come from ``inkdrop_support_bundle`` rather
than being reimplemented here. That module already rejects symlinked,
hardlinked and out-of-root log paths, builds the secret inventory, redacts
each tail, and re-verifies the result before anything is written. This module
previously did its own plain ``glob`` and tail-read with no redaction at all,
so a button sitting one row below "Download support bundle" shipped API keys,
basic-auth headers and passwords in cleartext -- and named the host log
directory in its manifest.

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
import re
import time
import zipfile
from pathlib import Path

from core import inkdrop_support_bundle


EXPORT_SCHEMA = "inkdrop.log_export.v2"
DEFAULT_PER_FILE_CAP_BYTES = inkdrop_support_bundle.DEFAULT_PER_FILE_CAP_BYTES
DEFAULT_TOTAL_CAP_BYTES = inkdrop_support_bundle.DEFAULT_TOTAL_CAP_BYTES
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def collect_log_files(log_dir=None):
    """List log files newest-first without reading their contents.

    Thin wrapper over the support bundle's collector so both exports agree on
    which files are logs and which are unsafe to follow.
    """
    rows, _skipped = inkdrop_support_bundle.collect_log_files(log_dir)
    return [
        {
            "name": row.get("name") or Path(row["path"]).name,
            "path": str(row["path"]),
            "size_bytes": row["size_bytes"],
            "modified_at": row["modified_at"],
        }
        for row in rows
    ]


def _member_name(row, index):
    name = _UNSAFE_NAME.sub("_", str(row.get("name") or "")).strip("._")[:100]
    return f"logs/{index:03d}-{name or 'log'}"


def log_archive_filename(now=None):
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    return f"inkdrop-logs-{stamp}.zip"


def build_log_archive_bytes(
    log_dir=None,
    per_file_cap_bytes=DEFAULT_PER_FILE_CAP_BYTES,
    total_cap_bytes=DEFAULT_TOTAL_CAP_BYTES,
    *,
    state_db=None,
    environ=None,
    secret_root=None,
    deadline_seconds=inkdrop_support_bundle.DEFAULT_DEADLINE_SECONDS,
):
    """Return ``(zip_bytes, manifest)`` of redacted logs for the log directory.

    Every included file is redacted with the support bundle's inventory-backed
    redactor and then re-checked for the exact secret values before it is
    written. A file that cannot be proven clean is skipped with a reason in
    the manifest rather than shipped, and if the inventory itself could not be
    built no logs are included at all -- the same fail-closed posture the full
    support bundle takes.
    """
    started = time.monotonic()
    deadline_seconds = max(1.0, min(float(deadline_seconds or inkdrop_support_bundle.DEFAULT_DEADLINE_SECONDS), 30.0))
    deadline = started + deadline_seconds
    per_file_cap_bytes = min(inkdrop_support_bundle.HARD_PER_FILE_CAP_BYTES, max(4096, int(per_file_cap_bytes)))
    total_cap_bytes = min(inkdrop_support_bundle.HARD_TOTAL_LOG_BYTES, max(per_file_cap_bytes, int(total_cap_bytes)))
    inventory = inkdrop_support_bundle.collect_secret_inventory(
        state_db, environ=environ, secret_root=secret_root, deadline=deadline
    )
    encoded_variants = tuple(item.encode("utf-8") for item in inventory.variants())
    manifest = {
        "schema": EXPORT_SCHEMA,
        "generated_at": int(time.time()),
        # Never the real path: it carries the host account name.
        "log_dir": "<runtime-log-dir>",
        "per_file_cap_bytes": per_file_cap_bytes,
        "total_cap_bytes": total_cap_bytes,
        "redacted": True,
        "files": [],
        "skipped": [],
        "redactions": 0,
        "decode_replacements": 0,
    }
    rows, path_skips = inkdrop_support_bundle.collect_log_files(log_dir, deadline=deadline)
    manifest["skipped"].extend(path_skips)
    if inventory.errors:
        manifest["skipped"].extend(
            {"name": f"log-{index:03d}", "reason": "secret_inventory_unavailable"}
            for index, _row in enumerate(rows, 1)
        )
        rows = []
    budget = total_cap_bytes
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for index, row in enumerate(rows, 1):
            member = _member_name(row, index)
            if budget <= 0:
                manifest["skipped"].append({"name": member, "reason": "total_cap_reached"})
                continue
            redactor = inkdrop_support_bundle.SupportRedactor(inventory, deadline=deadline)
            try:
                cap = min(per_file_cap_bytes, budget)
                raw, truncated = inkdrop_support_bundle.tail_with_context(row, cap)
                payload = redactor.log_bytes(raw)
                if len(payload) > cap:
                    payload = payload[-cap:].decode("utf-8", errors="ignore").encode("utf-8")
                    truncated = True
                if inkdrop_support_bundle.contains_secret((payload,), encoded_variants, deadline=deadline):
                    raise ValueError("post-redaction secret verification failed")
            except Exception:
                manifest["skipped"].append({"name": member, "reason": "redaction_failed"})
                continue
            budget -= len(payload)
            archive.writestr(member, payload)
            manifest["redactions"] += redactor.redactions
            manifest["decode_replacements"] += redactor.decode_replacements
            manifest["files"].append(
                {
                    "name": member,
                    "size_bytes": row["size_bytes"],
                    "included_bytes": len(payload),
                    "truncated": bool(truncated or len(payload) < row["size_bytes"]),
                    "modified_at": row["modified_at"],
                }
            )
        archive.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
    payload = buffer.getvalue()
    with zipfile.ZipFile(io.BytesIO(payload), "r") as archive:
        if archive.testzip() is not None:
            raise RuntimeError("log archive ZIP validation failed")
        for item in archive.infolist():
            metadata = item.filename.encode("utf-8") + item.comment + item.extra
            if inkdrop_support_bundle.contains_secret(
                (metadata, archive.read(item)), encoded_variants, deadline=deadline
            ):
                raise RuntimeError("log archive post-build secret verification failed")
    return payload, manifest


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
