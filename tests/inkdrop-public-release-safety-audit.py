#!/usr/bin/env python3
"""Static public-release safety audit for InkDrop packaging/startup files."""

from __future__ import annotations

import fnmatch
import importlib.util
import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent

PUBLIC_FILES = (
    "README.md",
    "Dockerfile",
    "docker-compose.yml",
    ".env.example",
    ".dockerignore",
    ".gitignore",
    "requirements.txt",
    "inkdrop-logo-mark.png",
    "inkdrop_container_healthcheck.py",
    "inkdrop_container_start.py",
    "inkdrop_runtime_config.py",
    "inkdrop_preflight.py",
    "inkdrop-public-docker-runtime-smoke.py",
    "inkdrop-public-repo-export-smoke.py",
    "tools/inkdrop_public_release_check.py",
    "tools/inkdrop_public_repo_export.py",
    "docs/inkdrop/docker-first-install.md",
    "docs/inkdrop-source-candidate-catalog-20260702.json",
)

STARTUP_FILE = "inkdrop_web.py"
STARTUP_SCAN_LINES = 450
BACKEND_STARTUP_SCANS = {
    "inkdrop_state.py": 130,
    "inkdrop_source_worker_cli.py": 80,
    "inkdrop_completed_import.py": 110,
    "inkdrop_missing_acquire.py": 130,
    "inkdrop_series_autopilot.py": 120,
    "inkdrop_acquire.py": 70,
    "inkdrop_reconcile_imports.py": 90,
    "inkdrop_slskd_source_probe.py": 80,
    "inkdrop_sab_failed_cleanup.py": 80,
    "inkdrop_pack_import.py": 80,
    "inkdrop_manual_source_autoresolve.py": 90,
    "inkdrop_rss_discovery.py": 80,
    "inkdrop_comicscodes_discovery.py": 75,
    "inkdrop_mangadex_direct.py": 70,
}
FULL_FILE_SCANS = (
    "inkdrop_acquire.py",
    "inkdrop_reconcile_imports.py",
    "inkdrop_sab_failed_cleanup.py",
)

REQUIRED_DOCKERIGNORE_PATTERNS = (
    "*",
    "!/Dockerfile",
    "!/requirements.txt",
    "!/inkdrop-logo-mark.png",
    "!/inkdrop_web.py",
    "!/inkdrop_state.py",
    "!/inkdrop_runtime_config.py",
    "!/inkdrop_container_healthcheck.py",
    "!/inkdrop_container_start.py",
    "!/docs/",
    "/docs/**",
    "!/docs/inkdrop-source-candidate-catalog-20260702.json",
    "*-smoke.py",
    "*-audit.py",
    "*-operator.py",
    "*-plan.py",
    "*-repair.py",
    "*-cleanup.py",
    "*-diagnostic.py",
    "*-regression.py",
    "*.remote.py",
    "*.remote-*.py",
    "*.live.py",
    "*.inkdrop-live.py",
    "*.current.py",
    "*.inkdrop-work.py",
    "inkdrop_*_audit.py",
    "inkdrop_*_repair.py",
    "inkdrop_*_cleanup.py",
    "inkdrop_*_diagnostic.py",
    "docs/*.py",
    "inkdrop-agent-context-pack.py",
    "inkdrop-agent-coordinator-quickcheck.py",
    "inkdrop-completion-identity-audit-diff.py",
    "inkdrop_chainsaw_stale_proof_repair.py",
    "inkdrop_duplicate_manga_cleanup.py",
    "inkdrop_metadata_repair_plan.py",
    "inkdrop-prowlarr-torrentleech-comics-source.py",
    "kavita_archive_health.py",
    "kavita_archive_repair.py",
    "homelab-status-export*.py",
    "sab_rescue_server.py",
    "slskd_recovery_watchdog.py",
    "*.sqlite3",
    "*.db",
    "*.cbz",
    "backups/",
    "state/",
    ".env",
    "docs/inkdrop/AGENT_HANDOFF.md",
)

FORBIDDEN_DOCKERIGNORE_PATTERNS = (
    "!/inkdrop*.py",
    "!/kavita*.py",
)

REQUIRED_GITIGNORE_PATTERNS = (
    ".env",
    ".env.*",
    "!.env.example",
    "config/",
    "state/",
    "staging/",
    "manual-inbox/",
    "library/",
    "backups/",
    "*.sqlite3",
    "*.db",
    "*.jsonl",
    "*.cbz",
    "*.cbr",
    "*.pdf",
    "*.epub",
    "*.zip",
    "*.rar",
    "*.7z",
)

PRIVATE_HOME_MARKERS = (
    "/home/" + "curlz" + "620",
    "C:\\Users\\" + "Jar" + "ed",
)

PRIVATE_PATTERNS = (
    ("private_home_path", re.compile("|".join(re.escape(value) for value in PRIVATE_HOME_MARKERS), re.IGNORECASE)),
    ("private_drivepool_path", re.compile(re.escape("/mnt/" + "drive" + "pool"), re.IGNORECASE)),
    ("private_lan_ip", re.compile(r"\b192\.168\.\d+\.\d+\b")),
    ("comicvine_api_key_literal", re.compile(r"\b[0-9a-f]{32}\b", re.IGNORECASE)),
    ("query_api_key_literal", re.compile(r"(?:api[_-]?key|apikey)=([A-Za-z0-9][A-Za-z0-9._-]{8,})", re.IGNORECASE)),
)
TEXT_CONTEXT_SUFFIXES = {".dockerignore", ".env", ".example", ".json", ".md", ".py", ".sh", ".txt", ".yaml", ".yml"}
SECRET_ENV_KEY_PARTS = {"APIKEY", "KEY", "TOKEN", "PASSWORD", "PASS", "SECRET", "USERNAME"}

def _read(path):
    return path.read_text(encoding="utf-8", errors="replace")


def scan_file(path, *, max_lines=None):
    findings = []
    lines = _read(path).splitlines()
    if max_lines is not None:
        lines = lines[: int(max_lines)]
    for line_no, line in enumerate(lines, 1):
        for key, pattern in PRIVATE_PATTERNS:
            if pattern.search(line):
                findings.append(
                    {
                        "file": path.relative_to(ROOT).as_posix(),
                        "line": line_no,
                        "kind": key,
                        "text": line.strip()[:240],
                    }
                )
    return findings


def _dockerignore_patterns():
    dockerignore = ROOT / ".dockerignore"
    if not dockerignore.exists():
        return []
    patterns = []
    for raw in _read(dockerignore).splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(line)
    return patterns


def _pattern_matches(relative_path, pattern):
    relative = relative_path.as_posix()
    name = relative_path.name
    parts = relative_path.parts
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


def _path_is_ignored(relative_path, patterns):
    ignored = False
    for pattern in patterns:
        negate = pattern.startswith("!")
        raw_pattern = pattern[1:] if negate else pattern
        if _pattern_matches(relative_path, raw_pattern):
            ignored = not negate
    return ignored


def scan_public_image_root_private_files():
    findings = []
    manifest_path = ROOT / "tools" / "inkdrop_docker_context_manifest.py"
    spec = importlib.util.spec_from_file_location("inkdrop_docker_context_manifest", manifest_path)
    if spec is None or spec.loader is None:
        included = []
    else:
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        included = module.included_files(ROOT)
    for relative in sorted(included):
        if relative.suffix.lower() != ".py":
            continue
        path = ROOT / relative
        for finding in scan_file(path):
            finding["kind"] = f"docker_context_{finding['kind']}"
            findings.append(finding)
    return findings


def scan_public_image_context_private_text_files():
    findings = []
    patterns = _dockerignore_patterns()
    candidates = [
        ROOT / "Dockerfile",
        ROOT / "README.md",
        ROOT / "requirements.txt",
        ROOT / "docker-compose.yml",
        ROOT / ".dockerignore",
        ROOT / ".env.example",
        ROOT / "docs" / "inkdrop-source-candidate-catalog-20260702.json",
    ]
    candidates.extend(sorted(ROOT.glob("inkdrop*.py")))
    candidates.extend(sorted(ROOT.glob("kavita*.py")))
    for path in candidates:
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT)
        if _path_is_ignored(relative, patterns):
            continue
        suffix = path.suffix.lower()
        if suffix not in TEXT_CONTEXT_SUFFIXES and path.name not in {".dockerignore", ".gitignore"}:
            continue
        for finding in scan_file(path):
            finding["kind"] = f"docker_context_text_{finding['kind']}"
            findings.append(finding)
    return findings


def scan_github_workflows():
    findings = []
    workflow_root = ROOT / ".github" / "workflows"
    if not workflow_root.exists():
        return findings
    for pattern in ("*.yml", "*.yaml"):
        for path in sorted(workflow_root.glob(pattern)):
            findings.extend(scan_file(path))
    return findings


def _is_secret_env_key(key):
    parts = {part for part in re.split(r"[^A-Za-z0-9]+", key.upper()) if part}
    if "API" in parts and "KEY" in parts:
        return True
    return bool(parts & SECRET_ENV_KEY_PARTS)


def scan_env_example_secret_defaults():
    findings = []
    path = ROOT / ".env.example"
    if not path.exists():
        return findings
    for line_no, raw_line in enumerate(_read(path).splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if _is_secret_env_key(key) and value.strip():
            findings.append(
                {
                    "file": ".env.example",
                    "line": line_no,
                    "kind": "env_example_secret_default",
                    "text": f"{key} should default blank in the public example config",
                }
            )
    return findings


def main(argv=None):
    findings = []
    for name in PUBLIC_FILES:
        path = ROOT / name
        if not path.exists():
            findings.append({"file": name, "line": 0, "kind": "missing_public_file", "text": "required public file is missing"})
            continue
        findings.extend(scan_file(path))

    startup = ROOT / STARTUP_FILE
    if startup.exists():
        findings.extend(scan_file(startup, max_lines=STARTUP_SCAN_LINES))
    else:
        findings.append({"file": STARTUP_FILE, "line": 0, "kind": "missing_startup_file", "text": "web runtime is missing"})

    for name, max_lines in BACKEND_STARTUP_SCANS.items():
        path = ROOT / name
        if path.exists():
            findings.extend(scan_file(path, max_lines=max_lines))
        else:
            findings.append({"file": name, "line": 0, "kind": "missing_backend_startup_file", "text": "backend startup file is missing"})

    for name in FULL_FILE_SCANS:
        path = ROOT / name
        if path.exists():
            findings.extend(scan_file(path))
        else:
            findings.append({"file": name, "line": 0, "kind": "missing_backend_file", "text": "backend file is missing"})

    dockerignore = ROOT / ".dockerignore"
    if dockerignore.exists():
        dockerignore_text = _read(dockerignore)
        for pattern in REQUIRED_DOCKERIGNORE_PATTERNS:
            if pattern not in dockerignore_text:
                findings.append(
                    {
                        "file": ".dockerignore",
                        "line": 0,
                        "kind": "missing_dockerignore_pattern",
                        "text": f"runtime image should exclude {pattern}",
                    }
                )
        for pattern in FORBIDDEN_DOCKERIGNORE_PATTERNS:
            if pattern in dockerignore_text:
                findings.append(
                    {
                        "file": ".dockerignore",
                        "line": 0,
                        "kind": "forbidden_dockerignore_pattern",
                        "text": f"runtime image should not use broad Python context allowlist {pattern}",
                    }
                )

    gitignore = ROOT / ".gitignore"
    if gitignore.exists():
        gitignore_text = _read(gitignore)
        for pattern in REQUIRED_GITIGNORE_PATTERNS:
            if pattern not in gitignore_text:
                findings.append(
                    {
                        "file": ".gitignore",
                        "line": 0,
                        "kind": "missing_gitignore_pattern",
                        "text": f"public repo should ignore {pattern}",
                    }
                )

    dockerfile = ROOT / "Dockerfile"
    if dockerfile.exists():
        dockerfile_text = _read(dockerfile)
        if "COPY . ." in dockerfile_text:
            findings.append(
                {
                    "file": "Dockerfile",
                    "line": 0,
                    "kind": "broad_docker_context_copy",
                    "text": "Dockerfile should copy explicit InkDrop runtime files, not the whole homelab workspace",
                }
            )
        if "docs/inkdrop-source-candidate-catalog-20260702.json" not in dockerfile_text:
            findings.append(
                {
                    "file": "Dockerfile",
                    "line": 0,
                    "kind": "missing_runtime_catalog_copy",
                    "text": "Dockerfile should copy the runtime source catalog used by inkdrop_source_catalog.py",
                }
            )
        if "COPY inkdrop-logo-mark.png ./" not in dockerfile_text:
            findings.append(
                {
                    "file": "Dockerfile",
                    "line": 0,
                    "kind": "missing_runtime_logo_copy",
                    "text": "Dockerfile should copy the InkDrop logo asset served by the web UI",
                }
            )

    findings.extend(scan_public_image_root_private_files())
    findings.extend(scan_public_image_context_private_text_files())
    findings.extend(scan_github_workflows())
    findings.extend(scan_env_example_secret_defaults())

    payload = {
        "ok": not findings,
        "finding_count": len(findings),
        "startup_scan_lines": STARTUP_SCAN_LINES,
        "findings": findings,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
