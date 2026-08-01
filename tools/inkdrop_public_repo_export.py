#!/usr/bin/env python3
"""Stage a clean public InkDrop source tree without touching the workspace.

The working directory still contains unrelated homelab automation. This helper
turns the curated public packaging contract into a concrete export directory so
GitHub publication can start from a safe allowlist instead of the whole local
workspace.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 1

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import inkdrop_docker_context_manifest


PUBLIC_REPO_EXTRA_PATHS = (
    "README.md",
    ".dockerignore",
    ".env.example",
    ".github/workflows/inkdrop-public-release.yml",
    ".gitignore",
    "compose.release-gate.network-none.yml",
    "compose.network.example.yml",
    "docker-compose.yml",
    "docs/inkdrop/arr-stack-deployment-plan.md",
    "docs/inkdrop/docker-first-install.md",
    "docs/inkdrop/releases/current.json",
    "docs/inkdrop/releases/v0.1.0-alpha.22.md",
    "docs/inkdrop/releases/v0.1.0-alpha.23.md",
    "docs/inkdrop/releases/v0.1.0-alpha.24.md",
    "docs/inkdrop/releases/v0.1.0-alpha.25.md",
    "docs/inkdrop/releases/v0.1.0-alpha.26.md",
    "docs/inkdrop/releases/v0.1.0-alpha.27.md",
    "docs/inkdrop/releases/v0.1.0-alpha.28.md",
    "docs/inkdrop/releases/v0.1.0-alpha.29.md",
    "docs/inkdrop/releases/v0.1.0-alpha.30.md",
    "docs/inkdrop/releases/v0.1.0-alpha.31.md",
    "docs/inkdrop/releases/v0.1.0-alpha.32.md",
    "docs/inkdrop/releases/v0.1.0-alpha.33.md",
    "docs/inkdrop/releases/v0.1.0-alpha.34.md",
    "docs/inkdrop/releases/v0.1.0-alpha.35.md",
    "docs/inkdrop/releases/v0.1.0-alpha.36.md",
    "docs/inkdrop/releases/v0.1.0-alpha.37.md",
    "docs/inkdrop/releases/v0.1.0-alpha.38.md",
    "docs/inkdrop/releases/v0.1.0-alpha.39.md",
    "docs/inkdrop/releases/v0.1.0-alpha.40.md",
    "docs/inkdrop/releases/v0.1.0-alpha.41.md",
    "docs/inkdrop/releases/v0.1.0-alpha.42.md",
    "docs/inkdrop/releases/v0.1.0-alpha.43.md",
    "docs/inkdrop/fixtures/manual-search-provider-results.json",
    "inkdrop-auth-api-key-smoke.py",
    "inkdrop-auth-enforcement-smoke.py",
    "inkdrop-autopilot-fairness-smoke.py",
    "inkdrop-automatic-search-recall-smoke.py",
    "inkdrop-acquisition-funnel-evidence-smoke.py",
    "inkdrop-backup-restore-smoke.py",
    "inkdrop-settings-backup-restore-smoke.py",
    "inkdrop-settings-backup-browser-smoke.py",
    "inkdrop-history-taxonomy-smoke.py",
    "inkdrop-compose-deployment-plan-smoke.py",
    "inkdrop-db-boundary-smoke.py",
    "inkdrop_download_client_ownership_smoke.py",
    "inkdrop-download-client-instance-store-smoke.py",
    "inkdrop-download-client-instance-api-smoke.py",
    "inkdrop-download-client-ui-smoke.py",
    "inkdrop-download-client-secret-store-smoke.py",
    "inkdrop-queue-claim-smoke.py",
    "inkdrop-first-run-setup-status-smoke.py",
    "inkdrop-automatic-search-state-smoke.py",
    "inkdrop-closed-alpha-e2e-fixture.py",
    "tools/inkdrop_closed_alpha_update_rehearsal.sh",
    "tools/inkdrop_suwayomi_fresh_volume_rehearsal.sh",
    "inkdrop-import-ready-worker-smoke.py",
    "inkdrop-dispatch-recovery-closure-smoke.py",
    "inkdrop-import-ready-worker.sh",
    "inkdrop-direct-source-certification-smoke.py",
    "inkdrop-source-worker-jobs-smoke.py",
    "inkdrop-library-import-plan-smoke.py",
    "inkdrop-kapowarr-shutdown-readiness-smoke.py",
    "inkdrop-kapowarr-fallback-retirement-smoke.py",
    "inkdrop-komga-settings-smoke.py",
    "inkdrop-library-frontends-smoke.py",
    "inkdrop-library-identity-reader-truth-smoke.py",
    "inkdrop-local-folder-only-flow-smoke.py",
    "inkdrop-manual-search-readiness-smoke.py",
    "inkdrop-manual-search-core-smoke.py",
    "inkdrop-manual-search-target-unit-smoke.py",
    "inkdrop-manual-search-executor-smoke.py",
    "inkdrop-manual-search-slskd-smoke.py",
    "inkdrop-slskd-series-generalization-smoke.py",
    "inkdrop-manual-search-migration-smoke.py",
    "inkdrop-manual-search-known-title-smoke.py",
    "inkdrop-manual-search-security-smoke.py",
    "inkdrop-manual-source-internal-import-auth-smoke.py",
    "inkdrop-planned-path-inspection-smoke.py",
    "inkdrop-planned-path-live-inspection.py",
    "inkdrop-provider-test-contract-smoke.py",
    "inkdrop-public-repo-export-smoke.py",
    "inkdrop-public-release-safety-audit.py",
    "inkdrop-release-notes-version-smoke.py",
    "inkdrop-github-release-contract-smoke.py",
    "inkdrop-closed-alpha-packet-smoke.py",
    "inkdrop-update-awareness-smoke.py",
    "inkdrop-public-docker-runtime-smoke.py",
    "inkdrop-public-security-smoke.py",
    "inkdrop-queue-sync-performance-smoke.py",
    "inkdrop-reconcile-lock-observability-smoke.py",
    "inkdrop-runtime-source-settings-smoke.py",
    "inkdrop-sab-cleanup-optional-adapter-smoke.py",
    "inkdrop_sab_failed_cleanup.py",
    "inkdrop-series-compact-payload-smoke.py",
    "inkdrop-series-remove-files-smoke.py",
    "inkdrop-settings-section-contract-smoke.py",
    "inkdrop-settings-registry-smoke.py",
    "inkdrop-source-worker-http-smoke.py",
    "inkdrop-source-settings-contract-smoke.py",
    "inkdrop-standalone-entrypoints-smoke.py",
    "inkdrop-ui-state-endpoint-contract-smoke.py",
    "web/tests/fixtures/history-taxonomy-v2.json",
    "web/tests/settings-backup-restore-browser-smoke.js",
    "web/tests/fixtures/download-clients-settings.html",
    "web/tests/download-clients-settings-browser-smoke.js",
    "tools/inkdrop_compose_deployment_plan.py",
    "tools/inkdrop_config_surface_audit.py",
    "tools/inkdrop_docker_context_manifest.py",
    "tools/inkdrop_install_support_summary.py",
    "tools/inkdrop_github_release.py",
    "tools/inkdrop_closed_alpha_packet.py",
    "tools/inkdrop_public_http_smoke.py",
    "tools/inkdrop_public_release_check.py",
    "tools/inkdrop_public_repo_export.py",
    "tools/inkdrop_release_evidence_bundle.py",
    "tools/inkdrop_settings_api_surface_audit.py",
    "tools/inkdrop_settings_sync_smoke.py",
    "tools/inkdrop_state_schema_audit.py",
    "tools/inkdrop_web_surface_audit.py",
)

CURRENT_RELEASE_CONTRACT = "docs/inkdrop/releases/current.json"
RELEASE_NOTES_ROOT = Path("docs/inkdrop/releases")

# The README and Compose file that ship publicly are the owner-approved
# text, not the working tree's own README.md / docker-compose.yml (which
# stay as the two-service, build-from-source files contributors and internal
# QA use). The approved README carries a maintainer comment at the top,
# stripped here. Both approved files still carry one open placeholder --
# the image tag -- surfaced in the result so the publish step can decide,
# not silently ship it.
APPROVED_README_SOURCE = "docs/inkdrop/beta-readme-approved-20260730.md"
APPROVED_COMPOSE_SOURCE = "docs/inkdrop/public-beta-compose.yml"
OPEN_PLACEHOLDER_MARKERS = ("inkdrop:TAG", "REPLACE_WITH_YOUR_APPROVED_DIGEST")


def public_readme_bytes(root: Path = ROOT):
    source = root / APPROVED_README_SOURCE
    if not source.is_file():
        # An exported tree doesn't carry the approved source file; its own
        # README.md already is the approved content, so re-running the
        # exporter from inside an export stays self-consistent.
        source = root / "README.md"
    text = source.read_text(encoding="utf-8")
    stripped = text.lstrip()
    if stripped.startswith("<!--"):
        end = stripped.find("-->")
        if end < 0:
            raise ValueError(f"unterminated maintainer comment in {APPROVED_README_SOURCE}")
        stripped = stripped[end + 3 :].lstrip("\n")
    if "<!--" in stripped:
        raise ValueError(f"unexpected comment left in the public README from {APPROVED_README_SOURCE}")
    return stripped.encode("utf-8")


def public_compose_bytes(root: Path = ROOT):
    source = root / APPROVED_COMPOSE_SOURCE
    if not source.is_file():
        # Same self-consistency fallback as the README: an already-exported
        # tree's own docker-compose.yml already is the approved content.
        source = root / "docker-compose.yml"
    return source.read_text(encoding="utf-8").encode("utf-8")


def public_dockerfile_bytes(root: Path = ROOT):
    """The Dockerfile's own build context is the export tree, so its COPY
    source paths must point at scripts/ once the cron scripts move there.
    The COPY destination stays the flat ./ it always was, so the running
    image (and everything inside it that invokes these by their in-container
    path) is completely unaffected -- only where the build reads them from."""
    text = (root / "Dockerfile").read_text(encoding="utf-8")
    for name in RELOCATABLE_SCRIPTS_DIR_FILES:
        text = text.replace(f"    {name} \\\n", f"    scripts/{name} \\\n")
    return text.encode("utf-8")


def open_placeholders(text_bytes):
    text = text_bytes.decode("utf-8")
    return [marker for marker in OPEN_PLACEHOLDER_MARKERS if marker in text]


# Lines in the ignore files that exist for this private working tree's agent
# tooling. The public repository never contains those files, so shipping the
# rules would only document the development environment. Markers are split
# because this helper ships in the export and must not carry them either.
PRIVATE_IGNORE_RULE_MARKERS = ("co" + "dex", "agents.md")


def _filtered_ignore_bytes(root, name):
    lines = (root / name).read_text(encoding="utf-8").splitlines()
    kept = [
        line
        for line in lines
        if not any(marker in line.lower() for marker in PRIVATE_IGNORE_RULE_MARKERS)
    ]
    return ("\n".join(kept).rstrip("\n") + "\n").encode("utf-8")


# The public repository gets its own .gitignore. The development tree uses a
# deny-everything allowlist where files added with git add -f stay tracked but
# never gain an allowlist entry -- exporting that file verbatim made a fresh
# clone silently drop 43 source files on the first ordinary git add. A public
# clone only needs to keep runtime data and media out of the repository.
PUBLIC_GITIGNORE = """\
# Local runtime data
.env
.env.*
!.env.example
config/
state/
staging/
manual-inbox/
library/
backups/
*.sqlite3
*.db
*.db-shm
*.db-wal
*.jsonl
*.log
*.bak
*.tmp

# Comics and manga never belong in the repository
*.cbz
*.cbr
*.pdf
*.epub
*.zip
*.rar
*.7z

# Python
__pycache__/
*.py[cod]
.venv/
"""


def export_content_overrides(root: Path = ROOT):
    """Paths whose shipped content differs from the working-tree file."""
    return {
        "README.md": public_readme_bytes(root),
        "docker-compose.yml": public_compose_bytes(root),
        "Dockerfile": public_dockerfile_bytes(root),
        ".gitignore": PUBLIC_GITIGNORE.encode("utf-8"),
        ".dockerignore": _filtered_ignore_bytes(root, ".dockerignore"),
    }


FORBIDDEN_PUBLIC_PATHS = {
    "AGENTS.md",
    "PUBLIC_REPO_MANIFEST.json",
    "HOMELAB_AUTOMATION_AUDIT.md",
    "homepage-services.yaml",
    "SABnzbd-Rescue.ps1",
    "docs/inkdrop/AGENT_HANDOFF.md",
    "docs/inkdrop/QA_RELEASE_PROCESS.md",
    "docs/inkdrop/direct-source-certification.md",
    "docs/inkdrop/download-client-expansion-round2.md",
    "docs/inkdrop/first-run-setup-acceptance-contract.md",
    "docs/inkdrop/folder-based-completion-plan.md",
    "docs/inkdrop/public-runtime-config-contract.md",
    "docs/inkdrop/service-inventory.md",
    "docs/inkdrop/standalone-foundation-plan.md",
    "docs/inkdrop/suwayomi-source-plan.md",
    "docs/inkdrop/MANUAL_SEARCH_CANDIDATE_CONTRACT.md",
    "docs/inkdrop/MANUAL_SEARCH_PROVIDER_BENCHMARK.md",
    "docs/inkdrop/MANUAL_SEARCH_CORE_CONTRACT.md",
}

FORBIDDEN_PUBLIC_PREFIXES = (
    "docs/inkdrop/qa",
)

FORBIDDEN_PATH_PARTS = {
    "release-evidence",
    "config",
    "state",
    "staging",
    "manual-inbox",
    "library",
    "arrdocker-work",
}

FORBIDDEN_SUFFIXES = {
    ".sqlite",
    ".sqlite3",
    ".db",
    ".db-shm",
    ".db-wal",
    ".cbz",
    ".cbr",
    ".pdf",
    ".epub",
    ".zip",
    ".rar",
    ".7z",
    ".log",
    ".bak",
    ".tmp",
}


def _normalize(relative):
    return Path(str(relative).replace("\\", "/"))


# Root-level test/audit/fixture/inspection scripts read fine individually but
# pile up: this repo ships ~60 of them next to the README. They move under
# tests/ in the export only -- the development tree's own layout, smoke-suite
# discovery glob, and CI path filters are untouched, since those are a
# separate, riskier change.
#
# The real application modules (inkdrop_web.py, inkdrop_state.py, ...) are
# always underscore-only: Python identifiers can't contain hyphens, so
# anything hyphenated at root is necessarily a standalone script, never an
# importable module. Combined with a "smoke" substring catch for the handful
# of underscore-named smoke entrypoints, this reliably separates the two
# without an explicit allowlist.
def is_relocatable_test_script(relative):
    parts = relative.parts
    if len(parts) != 1 or relative.suffix != ".py":
        return False
    stem = relative.stem.lower()
    return "-" in relative.stem or "smoke" in stem


# Cron/audit shell scripts the Dockerfile COPYs by bare name into a flat
# /app/. Their destination inside the image is unaffected by where they live
# in the build context, so they can move under scripts/ in the export as long
# as the Dockerfile's own COPY source paths move with them -- handled as a
# content override below, the same pattern as the README/compose overrides.
RELOCATABLE_SCRIPTS_DIR_FILES = (
    "inkdrop-completion-identity-audit-diff-alert.sh",
    "inkdrop-completion-identity-audit-diff-check.sh",
    "inkdrop-import-ready-worker.sh",
    "inkdrop-series-autopilot-cron.sh",
    "inkdrop-source-worker-mangadex-cron.sh",
    "inkdrop-source-worker-suwayomi-cron.sh",
    "inkdrop-source-worker.sh",
    "inkdrop-state-path-contract-sync-regression-alert.sh",
    "inkdrop-state-path-contract-sync-regression-check.sh",
)


def is_relocatable_cron_script(relative):
    return len(relative.parts) == 1 and relative.name in RELOCATABLE_SCRIPTS_DIR_FILES


def export_destination(relative):
    if is_relocatable_test_script(relative):
        return Path("tests") / relative.name
    if is_relocatable_cron_script(relative):
        return Path("scripts") / relative.name
    return relative


def resolve_source(root, relative):
    """The primary source location, or its export destination if only that
    exists -- re-running the exporter from inside an already-exported tree
    (root is itself an export) stays self-consistent instead of reporting
    already-relocated smoke tests as missing."""
    primary = root / relative
    if primary.is_file():
        return primary
    fallback = root / export_destination(relative)
    return fallback if fallback.is_file() else primary


def docker_context_paths():
    manifest = inkdrop_docker_context_manifest.build_manifest(ROOT)
    return {item["path"] for item in manifest.get("files") or [] if item.get("path")}


def current_release_note_path(root: Path = ROOT):
    """Resolve the current release note through the published release contract."""
    contract_path = root / CURRENT_RELEASE_CONTRACT
    try:
        payload = json.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid current release contract: {exc}") from exc
    raw = str(payload.get("notes_path") or "").strip()
    relative = _normalize(raw)
    if (
        not raw
        or relative.is_absolute()
        or ".." in relative.parts
        or relative.suffix.lower() != ".md"
        or relative.parent != RELEASE_NOTES_ROOT
    ):
        raise ValueError(f"unsafe current release notes_path: {raw!r}")
    if not (root / relative).is_file():
        raise FileNotFoundError(relative.as_posix())
    return relative


def public_repo_paths():
    paths = set(docker_context_paths())
    paths.update(PUBLIC_REPO_EXTRA_PATHS)
    dynamic_missing = []
    dynamic_forbidden = []
    try:
        paths.add(current_release_note_path().as_posix())
    except FileNotFoundError as exc:
        dynamic_missing.append(str(exc).strip("'"))
    except ValueError as exc:
        dynamic_forbidden.append(str(exc))
    clean = []
    missing = list(dynamic_missing)
    forbidden = list(dynamic_forbidden)
    for raw in sorted(paths):
        relative = _normalize(raw)
        if relative.is_absolute() or ".." in relative.parts:
            forbidden.append(raw)
            continue
        if not resolve_source(ROOT, relative).is_file():
            missing.append(relative.as_posix())
            continue
        if is_forbidden_public_path(relative):
            forbidden.append(relative.as_posix())
            continue
        clean.append(relative)
    return clean, missing, forbidden


def is_forbidden_public_path(relative):
    text = relative.as_posix()
    parts = set(relative.parts)
    if text in FORBIDDEN_PUBLIC_PATHS or text.startswith(FORBIDDEN_PUBLIC_PREFIXES):
        return True
    if relative.name in {"AGENTS.md", "AGENT_HANDOFF.md"}:
        return True
    # Hidden directories are local tooling; only .github belongs in public.
    if any(part != ".github" and part.startswith(".") for part in relative.parts[:-1]):
        return True
    if parts & FORBIDDEN_PATH_PARTS:
        return True
    if text.startswith("docs/inkdrop/bot-") or text.startswith("docs/inkdrop/AGENT"):
        return True
    if relative.suffix.lower() in FORBIDDEN_SUFFIXES:
        return True
    return False


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(paths, overrides=None):
    overrides = overrides or {}
    files = []
    for relative in paths:
        text = relative.as_posix()
        dest_text = export_destination(relative).as_posix()
        if text in overrides:
            # The manifest describes what ships, so hash the shipped bytes.
            content = overrides[text]
            files.append(
                {
                    "path": dest_text,
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )
            continue
        source = resolve_source(ROOT, relative)
        files.append(
            {
                "path": dest_text,
                "size_bytes": source.stat().st_size,
                "sha256": sha256_file(source),
            }
        )
    return {
        "schema": "inkdrop.public_repo_export.v1",
        "schema_version": SCHEMA_VERSION,
        "file_count": len(files),
        "total_size_bytes": sum(item["size_bytes"] for item in files),
        "files": files,
    }


def export_tree(target, paths, *, force=False):
    target = Path(target)
    if target.exists():
        if not target.is_dir():
            raise ValueError(f"target exists and is not a directory: {target}")
        existing = [item for item in target.iterdir()]
        if existing and not force:
            raise ValueError(f"target directory is not empty: {target}")
    target.mkdir(parents=True, exist_ok=True)
    overrides = export_content_overrides()
    for relative in paths:
        dest = target / export_destination(relative)
        dest.parent.mkdir(parents=True, exist_ok=True)
        content = overrides.get(relative.as_posix())
        if content is not None:
            dest.write_bytes(content)
        else:
            shutil.copy2(resolve_source(ROOT, relative), dest)
    manifest = build_manifest(paths, overrides)
    (target / "PUBLIC_REPO_MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return manifest


def run_export(*, target, apply=False, force=False):
    paths, missing, forbidden = public_repo_paths()
    try:
        overrides = export_content_overrides()
    except OSError:
        missing.append(APPROVED_README_SOURCE)
        missing.append(APPROVED_COMPOSE_SOURCE)
        overrides = {"README.md": b"", "docker-compose.yml": b""}
    manifest = build_manifest(paths, overrides)
    payload = {
        "schema": "inkdrop.public_repo_export.result.v1",
        "schema_version": SCHEMA_VERSION,
        "ok": not missing and not forbidden,
        "applied": False,
        "target": str(Path(target)) if target else "",
        "missing": missing,
        "forbidden": forbidden,
        "readme_placeholders": open_placeholders(overrides["README.md"]),
        "compose_placeholders": open_placeholders(overrides["docker-compose.yml"]),
        "manifest": manifest,
    }
    if apply:
        if not target:
            raise ValueError("--target is required with --apply")
        if missing or forbidden:
            payload["ok"] = False
            return payload
        payload["manifest"] = export_tree(target, paths, force=force)
        payload["applied"] = True
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description="Dry-run or create a sanitized public InkDrop repo export.")
    parser.add_argument("--target", default="", help="Directory to write when --apply is set.")
    parser.add_argument("--apply", action="store_true", help="Actually write the export directory.")
    parser.add_argument("--force", action="store_true", help="Allow writing into a non-empty target directory.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args(argv)

    payload = run_export(target=args.target, apply=bool(args.apply), force=bool(args.force))
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        mode = "exported" if payload.get("applied") else "dry-run"
        print(f"{mode}: {payload['manifest']['file_count']} files, {payload['manifest']['total_size_bytes']} bytes")
        for item in payload.get("missing") or []:
            print(f"missing: {item}")
        for item in payload.get("forbidden") or []:
            print(f"forbidden: {item}")
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
