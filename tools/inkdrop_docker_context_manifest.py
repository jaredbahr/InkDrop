#!/usr/bin/env python3
"""Emit a Docker-context manifest for InkDrop's public image review."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "__pycache__"}
DEFAULT_TOTAL_SIZE_WARN_BYTES = 10 * 1024 * 1024
ACCEPTED_TOTAL_SIZE_BYTES = DEFAULT_TOTAL_SIZE_WARN_BYTES + (4 * 1024 * 1024) + (608 * 1024)
DEFAULT_FILE_SIZE_WARN_BYTES = 1024 * 1024
ACCEPTED_TOTAL_CONTEXT_GROWTH = {
    "reason": "Closed-alpha acquisition support now includes automatic SLSKD handoff, remote SAB delivery and retry, retained partial Prowlarr results, safe pack selection, update awareness, truthful first-run status, durable removal and completion safeguards, read-only library adoption (registering a pre-existing comic/manga folder without downloading or moving anything), observe-only NFO pack-fanout scope evidence, the release calendar (what shipped, on which day, and whether it's owned), an administrator-only support bundle with bounded log tails, fail-closed credential redaction, and direct ZIP delivery, an authenticated OPDS catalog for reader clients, a WAL-consistent diagnostic-artifact integrity core, a standalone image-folder-to-CBZ builder, bounded runtime log rotation, atomic notification delivery reservation, and the Wanted, History, and Manual Review pages' React island migrations.",
    "risk": "Bounded packaging growth; any context above the accepted 14 MiB + 608 KiB ceiling remains release-blocking. The ceiling moved from 14 MiB + 288 KiB on 2026-08-05 when the History and Manual Review pages' React island migrations (two more table view components and their payload type definitions, following the same pattern already accepted for Blocklist/Wanted/Queue) landed together, adding about 279 KiB of frontend source combined -- History's own PR stayed under the old ceiling alone, so this move is the honest sum of both islands landing close together, not creep; the 320 KiB increment leaves only bounded headroom. The ceiling moved from 14 MiB + 256 KiB on 2026-08-05 when the Wanted page's React island migration (a new table view component and its payload type definitions, following the same pattern already accepted for Blocklist) added about 8 KiB of frontend source; the 32 KiB increment leaves only bounded headroom. The ceiling moved from 13 MiB + 768 KiB on 2026-08-03 when one merge day landed five independently-built runtime modules (OPDS catalog, diagnostic-artifact core, CBZ builder, log rotation, notification reservation store) that each fit under the old ceiling alone but not together -- about 290 KiB combined -- plus bounded headroom reserved for the already-reviewed SLSKD root-health and content-mode modules awaiting rebase; per-PR CI cannot see cumulative growth, so this move is the honest sum of that day's accepted work, not creep. The ceiling moved from 704 KiB for the support-bundle runtime module and its auth, UI, confidentiality, and deadline controls; the candidate adds about 57 KiB and the 64 KiB increment leaves only bounded headroom. The ceiling moved from 448 KiB after the launch-hardening pass: the authentication store split with its fail-closed generation record, the single-identity pack ownership model, the folder-move/import fence, bounded unit-identity parsing, and the supervised one-container entrypoint -- each closing an audited wrong-file, resurrection, or dead-install path. The ceiling moved from 384 KiB after a correctness pass added staged-file ownership verification, identity-based Manual Search result pairing, a ctime-aware archive validation cache and a precision-safe backfill cursor -- roughly 15 KiB of runtime source, each piece closing a path that silently imported or reported the wrong file. Growth was accepted because the alternative was leaving those open; it is not licence for routine creep.",
    "owner": "InkDrop maintainers",
    "next_action": "Reduce legacy runtime module size before a broader release while preserving Manual Search and direct-source contracts.",
    "exit_criteria": "The public Docker context returns below 10 MiB without removing supported runtime behavior.",
}
ACCEPTED_LARGE_CONTEXT_FILES = {
    "inkdrop_state.py": {
        "reason": "Legacy state/schema/queue compatibility module required by current public runtime.",
        "risk": "Packaging debt; split stable state helpers before a broader release.",
        "owner": "InkDrop maintainers",
        "next_action": "Extract stable state read/view helpers behind tests before removing this accepted warning.",
        "exit_criteria": "No Docker context file exceeds the large-file warning threshold except deliberate data/catalog artifacts.",
    },
    "inkdrop_web.py": {
        "reason": "Current InkDrop web/API implementation remains a large single module during closed alpha.",
        "risk": "Packaging debt; split the shipped web/API surface into smaller InkDrop modules before a broader release.",
        "owner": "InkDrop maintainers",
        "next_action": "Move public API/view route helpers into focused InkDrop modules while preserving route contracts.",
        "exit_criteria": "Public web/API routes are split into focused InkDrop modules without changing API contracts.",
    },
}


def dockerignore_patterns(root: Path):
    patterns = []
    for raw in (root / ".dockerignore").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(line)
    return patterns


def dockerignore_pattern_matches(relative_path: Path, pattern: str) -> bool:
    relative = relative_path.as_posix()
    parts = relative_path.parts
    name = relative_path.name
    anchored = pattern.startswith("/")
    normalized = pattern.strip("/")
    if not normalized:
        return False
    if anchored:
        if pattern.endswith("/"):
            return relative == normalized or relative.startswith(normalized + "/")
        if "/" not in normalized:
            return "/" not in relative and fnmatch.fnmatch(relative, normalized)
        return fnmatch.fnmatch(relative, normalized)
    if pattern.endswith("/"):
        if "/" in normalized:
            return relative == normalized or relative.startswith(normalized + "/") or fnmatch.fnmatch(relative, normalized + "/*")
        return any(fnmatch.fnmatch(part, normalized) for part in parts)
    if "/" in normalized:
        return fnmatch.fnmatch(relative, normalized)
    return fnmatch.fnmatch(name, normalized) or any(fnmatch.fnmatch(part, normalized) for part in parts)


def dockerignore_matches(relative_path: Path, patterns) -> bool:
    ignored = False
    for pattern in patterns:
        negate = pattern.startswith("!")
        raw_pattern = pattern[1:] if negate else pattern
        if dockerignore_pattern_matches(relative_path, raw_pattern):
            ignored = not negate
    return ignored


def negated_patterns(patterns):
    return [pattern[1:] for pattern in patterns if pattern.startswith("!")]


def should_descend(relative_dir: Path, patterns, include_patterns) -> bool:
    if not relative_dir.parts:
        return True
    if not dockerignore_matches(relative_dir, patterns):
        return True
    relative = relative_dir.as_posix().strip("/")
    relative_prefix = relative + "/"
    for pattern in include_patterns:
        normalized = pattern.strip("/")
        if not normalized:
            continue
        if normalized == relative or normalized.startswith(relative_prefix):
            return True
        if "/" in normalized and fnmatch.fnmatch(relative, normalized):
            return True
    return False


def included_files(root: Path):
    patterns = dockerignore_patterns(root)
    include_patterns = negated_patterns(patterns)
    files = []
    for current, dirs, names in os.walk(root):
        current_path = Path(current)
        try:
            current_relative = current_path.relative_to(root)
        except ValueError:
            dirs[:] = []
            continue
        if any(part in SKIP_DIRS for part in current_relative.parts):
            dirs[:] = []
            continue
        dirs[:] = [
            name
            for name in sorted(dirs)
            if name not in SKIP_DIRS
            and should_descend(current_relative / name, patterns, include_patterns)
        ]
        for name in sorted(names):
            relative = current_relative / name
            if any(part in SKIP_DIRS for part in relative.parts):
                continue
            if not dockerignore_matches(relative, patterns):
                files.append(relative)
    return files


def file_entry(root: Path, relative: Path):
    path = root / relative
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "path": relative.as_posix(),
        "size_bytes": path.stat().st_size,
        "sha256": digest,
    }


def build_manifest(root: Path = ROOT):
    files = [file_entry(root, relative) for relative in included_files(root)]
    return {
        "schema": "inkdrop.docker_context_manifest.v1",
        "file_count": len(files),
        "total_size_bytes": sum(item["size_bytes"] for item in files),
        "files": files,
    }


def size_warnings(manifest, *, total_limit=DEFAULT_TOTAL_SIZE_WARN_BYTES, file_limit=DEFAULT_FILE_SIZE_WARN_BYTES):
    warnings = []
    if manifest["total_size_bytes"] > total_limit:
        accepted = (
            total_limit == DEFAULT_TOTAL_SIZE_WARN_BYTES
            and manifest["total_size_bytes"] <= ACCEPTED_TOTAL_SIZE_BYTES
        )
        warnings.append(
            {
                "kind": "total_context_size",
                "limit_bytes": total_limit,
                "actual_bytes": manifest["total_size_bytes"],
                "accepted": accepted,
                "reason": ACCEPTED_TOTAL_CONTEXT_GROWTH["reason"] if accepted else "",
                "risk": ACCEPTED_TOTAL_CONTEXT_GROWTH["risk"] if accepted else "",
                "owner": ACCEPTED_TOTAL_CONTEXT_GROWTH["owner"] if accepted else "",
                "next_action": ACCEPTED_TOTAL_CONTEXT_GROWTH["next_action"] if accepted else "",
                "exit_criteria": ACCEPTED_TOTAL_CONTEXT_GROWTH["exit_criteria"] if accepted else "",
                "message": f"Docker context is {manifest['total_size_bytes']} bytes; review before a broader release if it grows past {total_limit} bytes.",
            }
        )
    for item in manifest["files"]:
        if item["size_bytes"] > file_limit:
            accepted = ACCEPTED_LARGE_CONTEXT_FILES.get(item["path"])
            warnings.append(
                {
                    "kind": "large_context_file",
                    "path": item["path"],
                    "limit_bytes": file_limit,
                    "actual_bytes": item["size_bytes"],
                    "accepted": bool(accepted),
                    "reason": (accepted or {}).get("reason", ""),
                    "risk": (accepted or {}).get("risk", ""),
                    "owner": (accepted or {}).get("owner", ""),
                    "next_action": (accepted or {}).get("next_action", ""),
                    "exit_criteria": (accepted or {}).get("exit_criteria", ""),
                    "message": f"{item['path']} is {item['size_bytes']} bytes in the Docker context.",
                }
            )
    return warnings


def warnings_ok(warnings):
    return all(bool(item.get("accepted")) for item in warnings)


def print_summary(manifest, *, limit=12):
    largest = sorted(manifest["files"], key=lambda item: item["size_bytes"], reverse=True)[:limit]
    warnings = size_warnings(manifest)
    print(f"schema: {manifest['schema']}")
    print(f"files: {manifest['file_count']}")
    print(f"total_size_bytes: {manifest['total_size_bytes']}")
    print(f"warnings: {len(warnings)}")
    for warning in warnings:
        print(f"- {warning['kind']}: {warning['message']}")
    print("largest_files:")
    for item in largest:
        print(f"- {item['path']} ({item['size_bytes']} bytes, sha256={item['sha256']})")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Emit the Docker build-context manifest implied by .dockerignore.")
    parser.add_argument("--json", action="store_true", help="Print the full manifest as JSON.")
    parser.add_argument("--summary", action="store_true", help="Print a concise review summary.")
    parser.add_argument("--limit", type=int, default=12, help="Largest-file count for --summary output.")
    parser.add_argument("--warnings-json", action="store_true", help="Print non-blocking size warnings as JSON.")
    args = parser.parse_args(argv)

    manifest = build_manifest(ROOT)
    manifest["warnings"] = size_warnings(manifest)
    if args.json:
        print(json.dumps(manifest, indent=2, sort_keys=True))
    elif args.warnings_json:
        print(
            json.dumps(
                {
                    "ok": warnings_ok(manifest["warnings"]),
                    "warning_count": len(manifest["warnings"]),
                    "unexpected_warning_count": sum(1 for item in manifest["warnings"] if not item.get("accepted")),
                    "warnings": manifest["warnings"],
                },
                indent=2,
                sort_keys=True,
            )
        )
    elif args.summary:
        print_summary(manifest, limit=max(0, args.limit))
    else:
        print(f"{manifest['file_count']} files, {manifest['total_size_bytes']} bytes")
        for item in manifest["files"]:
            print(f"{item['path']}\t{item['size_bytes']}\t{item['sha256']}")
    # A warnings report that says not-ok must fail the process too; a gate
    # reading only the exit code otherwise treats the finding as a pass.
    if args.warnings_json and not warnings_ok(manifest["warnings"]):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
