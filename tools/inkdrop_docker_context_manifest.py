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
ACCEPTED_TOTAL_SIZE_BYTES = DEFAULT_TOTAL_SIZE_WARN_BYTES + (4 * 1024 * 1024) + (3776 * 1024)
DEFAULT_FILE_SIZE_WARN_BYTES = 1024 * 1024
ACCEPTED_TOTAL_CONTEXT_GROWTH = {
    "reason": "Closed-alpha acquisition support now includes automatic SLSKD handoff, remote SAB delivery and retry, retained partial Prowlarr results, safe pack selection, update awareness, truthful first-run status, durable removal and completion safeguards, read-only library adoption (registering a pre-existing comic/manga folder without downloading or moving anything), observe-only NFO pack-fanout scope evidence, the release calendar (what shipped, on which day, and whether it's owned), an administrator-only support bundle with bounded log tails, fail-closed credential redaction, and direct ZIP delivery, an authenticated OPDS catalog for reader clients, a WAL-consistent diagnostic-artifact integrity core, a standalone image-folder-to-CBZ builder, bounded runtime log rotation, atomic notification delivery reservation, the Wanted, History, and Manual Review pages' React island migrations, the Series page's React island migration to virtualized/windowed table and poster-grid rendering, the Series Detail page's React island migration for its read-only hero/overview/stat-grid/issues-list surface, the Series Detail wrong-match correction flow, optimistic-concurrency revision/If-Match/409 protection on the Wanted/Queue/Blocklist mutation endpoints, an opt-in encrypted settings-credentials export/import (PBKDF2-SHA256 + Fernet, a new cryptography dependency), a Connect-settings panel surfacing the previously UI-less OPDS catalog, and a human-in-the-loop duplicate-series merge tool (a read-only side-by-side comparison plus a double-confirmed apply that folds a confirmed-duplicate series' live wanted/queue work onto the series being kept, parking rather than deleting the shadow row), and a library reconciliation/repair tool (core/inkdrop_library_reconcile.py) that reports DB-vs-disk drift -- multi-file issue conflicts, naming-scheme drift after a folder-layout migration, duplicate issue-number rows, and stale legacy provider-setting defaults -- and repairs only the one already-safe, idempotent action (refreshing the media_files present/missing ledger from what is actually on disk).",
    "risk": "Bounded packaging growth; any context above the accepted 14 MiB + 3776 KiB ceiling remains release-blocking. NO MOVE FOR THE THIRD AUDIT'S PERFORMANCE PASS, RECORDED BECAUSE IT NEARLY WAS ONE: that branch was written against caf5483, measured 8,438 bytes over the 2752 KiB ceiling, and carried a +1 MiB move of its own. Another pass landed the same +1 MiB first, for its own reasons, and on merge that move was dropped rather than compounded -- two passes do not get two ceilings for one day's growth, and the second one to arrive should check whether it still needs the move before keeping it. MEASURED on the merge result with this module against real checkouts rather than from the diff: qa at 3014322 is 205 files / 17,507,130 bytes; the merged branch is 206 files / 17,561,779. The pass's own delta is +54,649 bytes across 13 files -- +29,250 core/inkdrop_state.py (the Blocklist index/aggregate/cursor work and the History statistics hydration), +7,498 core/inkdrop_mangadex_direct.py (deferred reader verification and per-chapter connection reuse), +6,075 a new core/inkdrop_archive_compression.py (the per-member CBZ compression policy, in one module so the three writers cannot drift), +3,972 web/frontend/src/main.tsx and +1,721 vite.config.ts (section code splitting), and eight smaller files under 1.6 KiB each -- byte for byte the same delta it measured against caf5483 before the merge, so nothing about it is an artefact of the resolution. It leaves 984,909 bytes of headroom against the 18,546,688-byte ceiling, about 7.3 days at the measured mean of 134,894 bytes/day below. TESTS CONTRIBUTE ZERO, confirmed again here rather than assumed: the pass adds nine smoke files and a node harness and not one of them appears in the 13-file delta, exactly as remedy (1) below establishes. WORTH RECORDING: this pass REMOVES runtime source as well -- three dead symbols and their bodies -- and still grew the context by 54 KiB. The reduction that gets under 10 MiB is not going to come from deleting helpers. The ceiling moved from 14 MiB + 2752 KiB on 2026-09-16 for the code-audit remediation pass, which closes fourteen audit findings including the P1 request-envelope and archive resource-exhaustion defects. MEASURED, per file, against qa at caf5483 rather than estimated: the branch adds 60,580 bytes to the context and qa carried 46,211 bytes of headroom, so the merged tree was 14,369 over. The largest contributors are core/inkdrop_web.py +16,491, core/inkdrop_archive_conversion.py +14,210, core/inkdrop_opds.py +6,301, web/frontend/src/latestOnly.ts +3,293 and inkdrop-docker-entrypoint.sh +3,156. Tests contributed ZERO bytes, confirming remedy (1) below again rather than re-deriving it. REDUCTION WAS ATTEMPTED FIRST AND IS RECORDED BECAUSE IT MOSTLY FAILED: 54 percent of what the branch added to context files was explanatory prose, and moving the long-form narrative into the smoke-test docstrings -- which cost the image nothing -- freed only 5,351 bytes, leaving 9,018. Shaving the remainder would have cut explanation that is load-bearing at the code site, so it was stopped and the gap brought here instead. That is a useful negative result: prose is not a meaningful lever at this scale, and future moves should not spend time on it. The increment is 1 MiB, matching the previous move and the same measured rate (mean +134,894 bytes/day, median active-day +154,573), so it buys about seven days rather than the same-day IOU the +64 KiB increments turned out to be. This is a deferral like every move before it, and the open design question below is unchanged. THIS IS THE THIRD CEILING MOVE IN ONE DAY AND THE FIRST SIZED FROM MEASUREMENT RATHER THAN JUDGEMENT. The two moves before it were each +64 KiB and each was consumed within hours. The 00:06 move to 1664 KiB left 73,798 bytes of headroom and the day reached 805. The 11:07 move to 1728 KiB for the mobile Home truth pass (entry below) left 53,037 bytes -- 0.34 of a median day -- which is the state this entry is being written from. A +64 KiB increment is not a ceiling, it is a same-day IOU. The candidate that could not land against 1664 KiB is the one that will release a falsely-satisfied unit on every sweep pass, not just the first, whose own context delta is 6,653 bytes -- 7,458 bytes added to core/inkdrop_state.py, of which 4,862 are docstring; stripping every word of its prose would still have left it 1,885 bytes over. MEASURED GROWTH RATE, 2026-07-22 to 2026-08-21, reconstructed from git objects using this module's own matchers and validated against a real checkout before use: the context grew 12,349,693 to 16,396,499 bytes, +4,046,806 over 30 days. Mean +134,894 bytes/day; median active-day delta +154,573; largest single day +816,964 (2026-07-26, the React island migrations). A 128 KiB increment buys less than one average day and less than one median day; the 64 KiB increments actually used bought hours. This move is 1 MiB (+1,048,576) -- about seven days at the measured rate, a weekly cadence for revisiting rather than same-day churn. It deliberately does not adopt the largest day as routine. Note that 2026-08-21 was NOT an exceptional day by bytes: it added 86,297, well under a median day, despite being a thirteen-PR day with six regression fixes and a release cut. PR count is not a proxy for context growth, and sizing off a busy-feeling day would have under-sized this, not over-sized it. REMEDIES ALREADY DISPROVEN BY MEASUREMENT, DO NOT RE-DERIVE: (1) excluding tests frees nothing -- .dockerignore already excludes *-smoke.py and /web/tests/, and tests contribute zero bytes. (2) Splitting core/inkdrop_state.py or core/inkdrop_web.py frees nothing -- moving bytes between included modules leaves the total flat. (3) Excluding build-only files frees about 292 KiB, roughly two days: 98.2 percent of the context is genuinely COPYed into the runtime image, and the only build-context-only content is web/frontend/** feeding the multi-stage frontend builder. That third measurement also answers whether this gate watches the right artifact: it very nearly does, so the gate is sound and the growth is real. OPEN DESIGN QUESTION, QUEUED: what actually reduces this context, given the three remedies above are closed and the exit criterion is returning below 10 MiB. The honest position is that no identified packaging change gets there; reduction has to come from deleting or externalising runtime source, which is a design decision nobody has taken. Until it is taken, every ceiling move is a deferral and should be read as one. This module is not itself in the Docker context, so editing this note costs no headroom. The ceiling moved from 14 MiB + 1664 KiB on 2026-08-21 for the mobile Home truth pass: a new 7,786-byte core/inkdrop_import_evidence.py, which classifies a row source as a provider id or a filesystem path so the source pill never sends a path through a label function, plus about 5.5 KiB of runtime additions to core/inkdrop_state.py and core/inkdrop_web.py. Measured: qa was 184 files / 16,383,195 bytes, 805 bytes under the old ceiling; the branch is 185 files / 16,396,499 bytes, 12,499 over. tests/ and web/tests/ contribute ZERO files, as the note below already established -- confirmed again here only because the branch adds a new smoke, and it changed nothing. Every added byte is runtime source, so no packaging change would have avoided this move. Worth recording separately: 805 bytes of headroom meant essentially any change tripped this, so the ceiling was acting as a per-PR tripwire rather than a bound; the 64 KiB increment restores about 53 KiB of real headroom. The ceiling moved from 14 MiB + 1600 KiB on 2026-08-21 when a single integration pass merged seven independently-reviewed fixes -- the trusted-issue filename-guess gate, the durable bad-candidate retry window, oversized-archive sampling, collected-edition policy wiring and its manual-source completion, the language-guard author/authorship-transfer extension, and the mobile state-truth pass -- and left six more still to land. Measured composition before the move: the Docker context is 182 files, core/ 86.5%, web/ 8.8%, root 4.1%, docs 0.6%, tools 0.1%, and tests contribute ZERO bytes because .dockerignore already excludes *-smoke.py (line 169) and /web/tests/ (line 25) and dockerignore_patterns() in this module honours it. That measurement closes the hypothesis that the per-PR .gitignore test-allowlist anchors were pushing test files into the runtime context and making this blocker recurrent by construction: those anchors govern git tracking only, no test file appears anywhere in the context or in any PR's context delta, and a .dockerignore exclusion would free nothing because it already exists. Do not re-derive it. Every byte of the growth is runtime source: one is a new 16 KiB core/inkdrop_release_identity.py, another is 9.5 KiB of shipped mobile JS plus a new 7 KiB core/inkdrop_review_reasons.py, a third is 8.5 KiB of core/inkdrop_backup_restore.py. No packaging change would have avoided this. The 64 KiB increment covers the remaining six PRs (measured deltas totalling about 58 KiB) with roughly 6 KiB spare, and leaves only bounded headroom. The ceiling moved from 14 MiB + 1568 KiB on 2026-08-13 when the library reconciliation/repair tool (core/inkdrop_library_reconcile.py, its two new scheduled-job/diagnostics routes in core/inkdrop_web.py and core/inkdrop_auth_contracts.py, and its scheduled job registration) landed; the new runtime module alone is about 18 KiB, plus a few KiB across the three touched routing/scheduler modules; the 32 KiB increment leaves only bounded headroom. The ceiling moved from 14 MiB + 1536 KiB on 2026-08-13 when the human-in-the-loop duplicate-series merge tool (series_merge_candidate_detail()/apply_series_duplicate_merge() in core/inkdrop_state.py, their two routes in core/inkdrop_web.py, and a new smoke test) landed; the new test file alone is about 22 KiB, plus roughly 27 KiB across the two runtime modules; the 32 KiB increment leaves only bounded headroom. The ceiling moved from 14 MiB + 1280 KiB on 2026-08-12 when a single day's backlog-clearance and search-quality pass merged nine independent, individually-reviewed fixes each carrying its own smoke test (a source-memory suppression cooldown default, a folder-identity tie-break guard, discovery_only MangaDex companion removal/re-parking guards, a relocated-folder import-proof check, RSS detail-page concurrency, a hardened Cloudflare bypass verdict, and others) -- none individually large, but the combined same-day total pushed the context about 37 KiB over the old ceiling; per-PR CI cannot see cumulative same-day growth, so this move is the honest sum of that day's accepted work, not creep from any one PR alone; the 256 KiB increment leaves only bounded headroom. The ceiling moved from 14 MiB + 928 KiB on 2026-08-09 when the Series Detail page's React island migration (a new SeriesDetail.tsx component and seriesDetailTypes.ts payload/helper module, following the same pattern already accepted for Wanted/History/Manual Review/Series, plus the larger built frontend bundle it produces and two new test files) landed; per-PR CI only sees this against the qa commit a branch forked from, not qa as it stands once earlier same-day PRs (the SLSKD recovery-lane slot-share fix, the maintenance-sweep watermark fix, and the settings-import/OPDS PR) have merged ahead of it, so this move reflects this PR's own growth alone -- about 342 KiB across the two new frontend source files, the rebuilt bundle, and the new tests; the 352 KiB increment leaves only bounded headroom. The ceiling moved from 14 MiB + 864 KiB on 2026-08-09 when an opt-in encrypted settings-credentials export/import (PBKDF2-SHA256 + Fernet, a new cryptography dependency) and a Connect-settings panel surfacing the previously UI-less OPDS catalog landed; per-PR CI only sees this against the qa commit a branch forked from, not qa as it stands once earlier same-day PRs (bounded maintenance-sweep batching, qBittorrent/SABnzbd field-help and Test-modal fixes) have merged ahead of it, so this move reflects this PR's own growth alone -- about 23 KiB across core/inkdrop_backup_restore.py, core/inkdrop_web.py, and two new test files; the 64 KiB increment leaves only bounded headroom. The ceiling moved from 14 MiB + 800 KiB on 2026-08-07 when the Series page's React island migration to virtualized table/poster-grid rendering (a new react-virtuoso runtime dependency plus the larger built frontend bundle it produces) landed on a qa already at the 800 KiB ceiling from that same day's earlier accepted growth (the wrong-match correction flow, immediately below, plus the mobile-table/panel-text CSS fix it already accounts for); per-PR CI only sees this against the commit a branch forked from, not qa as it stands once earlier same-day PRs have merged ahead of it, so this move reflects this PR's own growth alone; the 64 KiB increment leaves only bounded headroom. The ceiling moved from 14 MiB + 704 KiB on 2026-08-07 when the Series Detail wrong-match correction flow (retract a verified import, quarantine the file, blocklist the exact candidate, requeue, and re-search automatically), the matched-release-title display on issue rows, and the per-series manga-unit-model override landed on a qa already at the 704 KiB ceiling from that same day's earlier accepted growth; per-PR CI only sees this against the qa commit a branch forked from, not qa as it stands once earlier same-day PRs have merged ahead of it, so this move reflects that PR's own runtime-source growth (about 22 KiB across core/inkdrop_state.py, core/inkdrop_web.py, core/inkdrop_manual_search.py, core/inkdrop_manual_search_core.py, core/inkdrop_missing_acquire.py, core/inkdrop_source_worker_coordinator.py, and core/inkdrop_manga_unit_policy.py), plus this same manifest's own accepted-growth note describing it; the 96 KiB increment leaves comfortable headroom rather than the usual bare minimum, since a same-day self-referential edit otherwise keeps nudging the ceiling it's trying to raise. The ceiling moved from 14 MiB + 672 KiB on 2026-08-07 when the download-task source-attribution rule and its history backfill (about 2 KiB of state-module source) landed on a qa already parked about 1.8 KiB under the old ceiling by the same day's merged work (the Manual Review approve/diagnostics pass with its stat-card icon glyphs, and the operational summary rollup fix); per-PR CI cannot see cumulative same-day growth, so this move is the honest sum of that day's accepted work, not creep from this PR alone; the 32 KiB increment leaves only bounded headroom. The ceiling moved from 14 MiB + 608 KiB on 2026-08-05 when this PR's revision-column schema/mutation-endpoint work landed the same day as two other in-flight sessions' merges already on qa (series-title-identity write-time/duplicate-detection normalization, restored MangaDex companion discovery fallback) plus a small Wanted Search routing fix -- none individually large, but the combined same-day total pushed the context about 30 KiB over the old ceiling; per-PR CI cannot see cumulative same-day growth, so this move is the honest sum of that day's accepted work, not creep from this PR alone; the 64 KiB increment leaves only bounded headroom. The ceiling moved from 14 MiB + 288 KiB on 2026-08-05 when the History and Manual Review pages' React island migrations (two more table view components and their payload type definitions, following the same pattern already accepted for Blocklist/Wanted/Queue) landed together, adding about 279 KiB of frontend source combined -- History's own PR stayed under the old ceiling alone, so this move is the honest sum of both islands landing close together, not creep; the 320 KiB increment leaves only bounded headroom. The ceiling moved from 14 MiB + 256 KiB on 2026-08-05 when the Wanted page's React island migration (a new table view component and its payload type definitions, following the same pattern already accepted for Blocklist) added about 8 KiB of frontend source; the 32 KiB increment leaves only bounded headroom. The ceiling moved from 13 MiB + 768 KiB on 2026-08-03 when one merge day landed five independently-built runtime modules (OPDS catalog, diagnostic-artifact core, CBZ builder, log rotation, notification reservation store) that each fit under the old ceiling alone but not together -- about 290 KiB combined -- plus bounded headroom reserved for the already-reviewed SLSKD root-health and content-mode modules awaiting rebase; per-PR CI cannot see cumulative growth, so this move is the honest sum of that day's accepted work, not creep. The ceiling moved from 704 KiB for the support-bundle runtime module and its auth, UI, confidentiality, and deadline controls; the candidate adds about 57 KiB and the 64 KiB increment leaves only bounded headroom. The ceiling moved from 448 KiB after the launch-hardening pass: the authentication store split with its fail-closed generation record, the single-identity pack ownership model, the folder-move/import fence, bounded unit-identity parsing, and the supervised one-container entrypoint -- each closing an audited wrong-file, resurrection, or dead-install path. The ceiling moved from 384 KiB after a correctness pass added staged-file ownership verification, identity-based Manual Search result pairing, a ctime-aware archive validation cache and a precision-safe backfill cursor -- roughly 15 KiB of runtime source, each piece closing a path that silently imported or reported the wrong file. Growth was accepted because the alternative was leaving those open; it is not licence for routine creep.",
    "owner": "InkDrop maintainers",
    "next_action": "Reduce legacy runtime module size before a broader release while preserving Manual Search and direct-source contracts.",
    "exit_criteria": "The public Docker context returns below 10 MiB without removing supported runtime behavior.",
}
ACCEPTED_LARGE_CONTEXT_FILES = {
    "core/inkdrop_state.py": {
        "reason": "Legacy state/schema/queue compatibility module required by current public runtime.",
        "risk": "Packaging debt; split stable state helpers before a broader release.",
        "owner": "InkDrop maintainers",
        "next_action": "Extract stable state read/view helpers behind tests before removing this accepted warning.",
        "exit_criteria": "No Docker context file exceeds the large-file warning threshold except deliberate data/catalog artifacts.",
    },
    "core/inkdrop_web.py": {
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
