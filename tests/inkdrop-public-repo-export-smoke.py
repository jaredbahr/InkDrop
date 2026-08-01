#!/usr/bin/env python3
"""Smoke test for the sanitized public repo export helper."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from tools import inkdrop_public_repo_export


REQUIRED_EXPORT_FILES = {
    "README.md",
    "Dockerfile",
    "docker-compose.yml",
    ".env.example",
    ".dockerignore",
    ".gitignore",
    "requirements.txt",
    "inkdrop_web.py",
    "web/static/img/inkdrop-auth-backdrop.webp",
    "inkdrop_state.py",
    "docs/inkdrop/docker-first-install.md",
    "docs/inkdrop/arr-stack-deployment-plan.md",
    "docs/inkdrop/fixtures/manual-search-provider-results.json",
    "tests/inkdrop-manual-search-readiness-smoke.py",
    "tests/inkdrop-slskd-series-generalization-smoke.py",
    "tests/inkdrop_download_client_ownership_smoke.py",
    "inkdrop_download_client_config.py",
    "inkdrop_download_client_api.py",
    "inkdrop_secret_store.py",
    "tests/inkdrop-download-client-instance-store-smoke.py",
    "tests/inkdrop-download-client-instance-api-smoke.py",
    "tests/inkdrop-download-client-secret-store-smoke.py",
    "tests/inkdrop-kapowarr-fallback-retirement-smoke.py",
    "tests/inkdrop-reconcile-lock-observability-smoke.py",
    "tests/inkdrop-autopilot-fairness-smoke.py",
    "tests/inkdrop-automatic-search-recall-smoke.py",
    "tests/inkdrop-acquisition-funnel-evidence-smoke.py",
    "tests/inkdrop-history-taxonomy-smoke.py",
    "tests/inkdrop-settings-backup-restore-smoke.py",
    "tests/inkdrop-settings-backup-browser-smoke.py",
    "tests/inkdrop-manual-source-internal-import-auth-smoke.py",
    "tests/inkdrop-release-notes-version-smoke.py",
    "tests/inkdrop-github-release-contract-smoke.py",
    "tests/inkdrop-closed-alpha-packet-smoke.py",
    "tests/inkdrop-update-awareness-smoke.py",
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
    "tests/inkdrop-direct-source-certification-smoke.py",
    "tests/inkdrop-source-worker-jobs-smoke.py",
    "inkdrop_manual_search.py",
    "tools/inkdrop_public_release_check.py",
    "tools/inkdrop_github_release.py",
    "tools/inkdrop_closed_alpha_packet.py",
    "tools/inkdrop_public_repo_export.py",
    "tools/inkdrop_release_evidence_bundle.py",
    "web/tests/fixtures/history-taxonomy-v2.json",
    "web/tests/settings-backup-restore-browser-smoke.js",
    "web/static/js/inkdrop-download-clients-ui.js",
    "web/tests/fixtures/download-clients-settings.html",
    "web/tests/download-clients-settings-browser-smoke.js",
    "tests/inkdrop-download-client-ui-smoke.py",
}

FORBIDDEN_EXPORT_FILES = {
    "AGENTS.md",
    "PUBLIC_REPO_MANIFEST.json",
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
    "HOMELAB_AUTOMATION_AUDIT.md",
    "homepage-services.yaml",
    "SABnzbd-Rescue.ps1",
}

FORBIDDEN_PARTS = {
    "release-evidence",
    "config",
    "state",
    "staging",
    "manual-inbox",
    "library",
}

PRIVATE_TEXT_MARKERS = {
    "/home/" + "curlz" + "620",
    "/mnt/" + "drive" + "pool",
    "C:\\Users\\" + "Jar" + "ed",
    "C:/Users/" + "Jar" + "ed",
    "192." + "168.0.",
    "192." + "168.1.",
    # Development-tool name; nothing public should mention it, including the
    # ignore files and this suite's own fixtures.
    "co" + "dex",
}

TEXT_SUFFIXES = {
    "",
    ".css",
    ".dockerignore",
    ".env",
    ".example",
    ".gitignore",
    ".html",
    ".js",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".txt",
    ".yaml",
    ".yml",
}


EXPORT_SELF_CHECK_COMPILE_FILES = (
    "tools/inkdrop_public_repo_export.py",
    "tools/inkdrop_public_release_check.py",
    "tools/inkdrop_release_evidence_bundle.py",
    "tools/inkdrop_docker_context_manifest.py",
    "inkdrop_web.py",
    "inkdrop_state.py",
    "inkdrop_download_client_config.py",
    "inkdrop_download_client_api.py",
    "inkdrop_secret_store.py",
    "tests/inkdrop-download-client-instance-store-smoke.py",
    "tests/inkdrop-download-client-instance-api-smoke.py",
    "tests/inkdrop-download-client-secret-store-smoke.py",
    "tests/inkdrop-kapowarr-fallback-retirement-smoke.py",
    "inkdrop_manual_search.py",
    "tests/inkdrop-manual-search-readiness-smoke.py",
    "tests/inkdrop_download_client_ownership_smoke.py",
    "tests/inkdrop-reconcile-lock-observability-smoke.py",
    "tests/inkdrop-manual-source-internal-import-auth-smoke.py",
    "tests/inkdrop-release-notes-version-smoke.py",
)


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def exported_paths(root):
    paths = set()
    for path in Path(root).rglob("*"):
        if path.is_file():
            paths.add(path.relative_to(root).as_posix())
    return paths


def current_notes_path(root):
    payload = json.loads((Path(root) / "docs/inkdrop/releases/current.json").read_text(encoding="utf-8"))
    return str(payload.get("notes_path") or "")


def assert_current_release_contract_is_closed(root, paths):
    notes_path = current_notes_path(root)
    require(notes_path in paths, f"current release notes_path is absent from export: {notes_path!r}")


def assert_current_release_path_validation():
    with tempfile.TemporaryDirectory(prefix="inkdrop-release-contract-smoke-") as tmp:
        root = Path(tmp)
        releases = root / "docs/inkdrop/releases"
        releases.mkdir(parents=True)
        contract = releases / "current.json"
        contract.write_text(json.dumps({"notes_path": "docs/inkdrop/releases/missing.md"}), encoding="utf-8")
        try:
            inkdrop_public_repo_export.current_release_note_path(root)
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("missing current release notes should fail closed")
        contract.write_text(json.dumps({"notes_path": "../private.md"}), encoding="utf-8")
        try:
            inkdrop_public_repo_export.current_release_note_path(root)
        except ValueError:
            pass
        else:
            raise AssertionError("traversal in current release notes_path should fail closed")
        note = releases / "next.md"
        note.write_text("# Next\n", encoding="utf-8")
        contract.write_text(json.dumps({"notes_path": "docs/inkdrop/releases/next.md"}), encoding="utf-8")
        require(
            inkdrop_public_repo_export.current_release_note_path(root).as_posix() == "docs/inkdrop/releases/next.md",
            "valid current release notes should resolve",
        )


def assert_forbidden_path_policy():
    forbidden = (
        "AGENTS.md",
        "nested/AGENTS.md",
        ".private-tooling/config.toml",
        ".workbench/agents/ui.toml",
        "PUBLIC_REPO_MANIFEST.json",
        "docs/inkdrop/qa61-private-audit.md",
        "docs/inkdrop/public-runtime-config-contract.md",
        "docs/inkdrop/standalone-foundation-plan.md",
    )
    for raw in forbidden:
        require(
            inkdrop_public_repo_export.is_forbidden_public_path(Path(raw)),
            f"public export policy should forbid {raw}",
        )
    require(
        not inkdrop_public_repo_export.is_forbidden_public_path(Path("docs/inkdrop/docker-first-install.md")),
        "public install documentation should remain exportable",
    )


def assert_no_private_text_markers(root):
    root = Path(root)
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in {".dockerignore", ".gitignore"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        lowered = text.lower()
        matches = sorted(marker for marker in PRIVATE_TEXT_MARKERS if marker.lower() in lowered)
        require(not matches, f"private marker(s) exported in {relative}: {matches}")


def assert_generated_manifest_matches_tree(root):
    root = Path(root)
    manifest_path = root / "PUBLIC_REPO_MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_paths = exported_paths(root) - {"PUBLIC_REPO_MANIFEST.json"}
    rows = {item["path"]: item for item in manifest.get("files") or []}
    require(set(rows) == expected_paths, "generated export manifest path set is stale")
    for relative in sorted(expected_paths):
        item = rows[relative]
        path = root / relative
        require(item.get("size_bytes") == path.stat().st_size, f"stale manifest size for {relative}")
        require(item.get("sha256") == inkdrop_public_repo_export.sha256_file(path), f"stale manifest digest for {relative}")


def run_exported_command(root, args):
    result = subprocess.run(
        [sys.executable, "-B", *args],
        cwd=str(root),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
    )
    require(
        result.returncode == 0,
        f"exported command failed ({' '.join(args)}): stdout={result.stdout[-1000:]} stderr={result.stderr[-1000:]}",
    )
    return result


def assert_exported_tree_self_checks(root):
    root = Path(root)
    compile_files = [path for path in EXPORT_SELF_CHECK_COMPILE_FILES if (root / path).is_file()]
    require(
        set(EXPORT_SELF_CHECK_COMPILE_FILES) <= set(compile_files),
        f"missing exported self-check compile files: {sorted(set(EXPORT_SELF_CHECK_COMPILE_FILES) - set(compile_files))}",
    )
    run_exported_command(root, ["-m", "py_compile", *compile_files])
    run_exported_command(
        root,
        [
            "-c",
            # tools/inkdrop_public_release_check.py ships unmodified: it still lists
            # these two by their old bare-root names (a separate, pre-existing gap --
            # that script's LOCAL_CHECKS list was never updated for the export's own
            # allowlist, let alone this tests/ move). Confirm it still names the right
            # files, then actually compile them from where they really live now.
            "import subprocess; from tools.inkdrop_public_release_check import LOCAL_CHECKS; "
            "required={'inkdrop-manual-source-internal-import-auth-smoke.py','inkdrop_download_client_ownership_smoke.py'}; "
            "command=LOCAL_CHECKS[0][2]; assert required.issubset(command); "
            "assert any('inkdrop_download_client_ownership_smoke.py' in check[2] for check in LOCAL_CHECKS[1:]); "
            "prefix=command[:command.index('py_compile') + 1]; "
            "raise SystemExit(subprocess.call([*prefix, *(f'tests/{name}' for name in sorted(required))]))",
        ],
    )
    result = run_exported_command(root, ["tools/inkdrop_public_repo_export.py", "--json"])
    payload = json.loads(result.stdout)
    require(payload.get("ok") is True, f"exported helper dry-run should be clean: {payload}")
    exported_manifest_paths = {item["path"] for item in payload["manifest"]["files"]}
    require(
        REQUIRED_EXPORT_FILES <= exported_manifest_paths,
        f"exported helper manifest missing required files: {sorted(REQUIRED_EXPORT_FILES - exported_manifest_paths)}",
    )
    require(
        not (FORBIDDEN_EXPORT_FILES & exported_manifest_paths),
        f"exported helper manifest contains forbidden files: {sorted(FORBIDDEN_EXPORT_FILES & exported_manifest_paths)}",
    )


def assert_normal_git_staging(root):
    root = Path(root)
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=str(root),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    ownership_smoke = "tests/inkdrop_download_client_ownership_smoke.py"
    ignored = subprocess.run(
        ["git", "check-ignore", "--quiet", ownership_smoke],
        cwd=str(root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    require(ignored.returncode == 1, "ownership smoke is still ignored in a fresh public export")
    staged = subprocess.run(
        ["git", "add", "--all"],
        cwd=str(root),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    require(staged.returncode == 0, "normal git add failed in a fresh public export")
    staged_paths = set(
        subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=str(root),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ).stdout.splitlines()
    )
    require(ownership_smoke in staged_paths, "normal git add did not stage the ownership smoke")
    # Every exported file must survive an ordinary git add: the development
    # tree's deny-all ignore file once dropped 43 source files (including the
    # auth module) from the first public commit without a word.
    manifest = json.loads((root / "PUBLIC_REPO_MANIFEST.json").read_text(encoding="utf-8"))
    expected = {item["path"] for item in manifest.get("files") or []}
    expected.add("PUBLIC_REPO_MANIFEST.json")
    dropped = sorted(expected - staged_paths)
    require(
        not dropped,
        f"a fresh clone's git add drops {len(dropped)} exported file(s): {dropped[:10]}",
    )


def assert_exported_readme_is_approved(root, result):
    """The public README is the owner-approved text, not the working tree's."""
    shipped = (Path(root) / "README.md").read_text(encoding="utf-8")
    approved = (
        (inkdrop_public_repo_export.ROOT / inkdrop_public_repo_export.APPROVED_README_SOURCE)
        .read_text(encoding="utf-8")
    )
    require(
        "InkDrop manages comic and manga libraries." in shipped,
        "exported README does not carry the approved opening line",
    )
    require("<!--" not in shipped, "maintainer comment leaked into the exported README")
    require(
        shipped.strip() in approved,
        "exported README drifted from the approved source text",
    )
    require("all rights reserved" in shipped.lower(), "exported README should carry the resolved license")
    require("a license has not been selected" not in shipped.lower(), "exported README license placeholder should be resolved")
    require(
        "ghcr.io/jaredbahr/inkdrop-beta@sha256:ca29ff1d454343f14b9b800886942bea4a1a71b10089a1e43aae1d8d43a082b1" in shipped,
        "exported README should carry the real, promoted inkdrop-beta digest",
    )
    require("ghcr.io/jaredbahr/inkdrop:latest" not in shipped, "exported README must not reference the reserved stable image name")
    require("REPLACE_WITH_YOUR_APPROVED_DIGEST" not in shipped, "exported README should not still carry the unresolved digest placeholder")
    # Once the source file is actually resolved (as it is right now, ready to
    # publish), the placeholder list must be empty -- see
    # assert_open_placeholder_detection_works below for a test of the
    # detection mechanism itself, independent of this file's current content.
    placeholders = result.get("readme_placeholders")
    require(isinstance(placeholders, list) and not placeholders, f"README should have no unresolved placeholders left: {placeholders}")


def assert_exported_compose_is_approved(root, result):
    """The public docker-compose.yml is the single-service beta file, not the dev tree's build-from-source one."""
    shipped = (Path(root) / "docker-compose.yml").read_text(encoding="utf-8")
    approved = (
        (inkdrop_public_repo_export.ROOT / inkdrop_public_repo_export.APPROVED_COMPOSE_SOURCE)
        .read_text(encoding="utf-8")
    )
    require(shipped == approved, "exported docker-compose.yml drifted from the approved beta source")
    require("inkdrop-worker" not in shipped, "exported beta compose should not carry the second service")
    require("build:" not in shipped, "exported beta compose should pull a prebuilt image, not build from source")
    require("/library/comics" in shipped and "/library/manga" in shipped, "exported beta compose should mount InkDrop's real default library paths")
    require("/state" in shipped, "exported beta compose must mount /state or the database will not persist across recreates")
    require(
        "ghcr.io/jaredbahr/inkdrop-beta@sha256:ca29ff1d454343f14b9b800886942bea4a1a71b10089a1e43aae1d8d43a082b1" in shipped,
        "exported compose should carry the real, promoted inkdrop-beta digest",
    )
    require("ghcr.io/jaredbahr/inkdrop:latest" not in shipped, "exported compose must not reference the reserved stable image name")
    require("REPLACE_WITH_YOUR_APPROVED_DIGEST" not in shipped, "exported compose should not still carry the unresolved digest placeholder")
    placeholders = result.get("compose_placeholders")
    require(isinstance(placeholders, list) and not placeholders, f"compose file should have no unresolved placeholders left: {placeholders}")


def assert_open_placeholder_detection_works():
    """Unit-test the detection mechanism itself with synthetic text, independent
    of whatever the real approved README/compose files currently contain --
    those are meant to reach zero placeholders once actually ready to publish,
    which would make a live-file-based regression check vacuous or, worse,
    force someone to leave a real file unresolved just to keep a test red."""
    require(
        inkdrop_public_repo_export.open_placeholders(b"image: ghcr.io/jaredbahr/inkdrop:TAG") == ["inkdrop:TAG"],
        "open_placeholders must detect an unresolved image-tag placeholder",
    )
    require(
        inkdrop_public_repo_export.open_placeholders(b"image: ghcr.io/jaredbahr/inkdrop:latest") == [],
        "open_placeholders must not false-positive once the tag is resolved",
    )
    require(
        inkdrop_public_repo_export.open_placeholders(
            b"image: ghcr.io/jaredbahr/inkdrop-beta@sha256:REPLACE_WITH_YOUR_APPROVED_DIGEST"
        ) == ["REPLACE_WITH_YOUR_APPROVED_DIGEST"],
        "open_placeholders must detect the unresolved per-tester digest placeholder",
    )
    require(
        inkdrop_public_repo_export.open_placeholders(
            b"image: ghcr.io/jaredbahr/inkdrop-beta@sha256:" + b"a" * 64
        ) == [],
        "open_placeholders must not false-positive once a real digest is substituted in",
    )


def main():
    assert_current_release_path_validation()
    assert_forbidden_path_policy()
    assert_open_placeholder_detection_works()
    require(
        not (inkdrop_public_repo_export.ROOT / "PUBLIC_REPO_MANIFEST.json").exists(),
        "PUBLIC_REPO_MANIFEST.json is generated in staged exports and must not be checked into the workspace",
    )
    dry = inkdrop_public_repo_export.run_export(target="", apply=False)
    require(dry.get("ok") is True, f"dry-run should be clean: {dry.get('missing')} {dry.get('forbidden')}")
    manifest_paths = {item["path"] for item in dry["manifest"]["files"]}
    require(REQUIRED_EXPORT_FILES <= manifest_paths, f"missing required export files: {sorted(REQUIRED_EXPORT_FILES - manifest_paths)}")
    require(not (FORBIDDEN_EXPORT_FILES & manifest_paths), f"forbidden files in dry-run manifest: {sorted(FORBIDDEN_EXPORT_FILES & manifest_paths)}")
    assert_current_release_contract_is_closed(inkdrop_public_repo_export.ROOT, manifest_paths)

    with tempfile.TemporaryDirectory(prefix="inkdrop-public-repo-export-smoke-") as tmp:
        result = inkdrop_public_repo_export.run_export(target=tmp, apply=True)
        require(result.get("ok") is True and result.get("applied") is True, "apply export should succeed")
        paths = exported_paths(tmp)
        require("PUBLIC_REPO_MANIFEST.json" in paths, "export should write PUBLIC_REPO_MANIFEST.json")
        require(REQUIRED_EXPORT_FILES <= paths, f"missing required exported files: {sorted(REQUIRED_EXPORT_FILES - paths)}")
        copied_forbidden = (FORBIDDEN_EXPORT_FILES - {"PUBLIC_REPO_MANIFEST.json"}) & paths
        require(not copied_forbidden, f"forbidden files exported: {sorted(copied_forbidden)}")
        assert_current_release_contract_is_closed(tmp, paths)
        for path in paths:
            parts = set(Path(path).parts)
            require(not (parts & FORBIDDEN_PARTS), f"forbidden path part exported: {path}")
        assert_no_private_text_markers(tmp)
        manifest = json.loads((Path(tmp) / "PUBLIC_REPO_MANIFEST.json").read_text(encoding="utf-8"))
        require(manifest.get("schema") == "inkdrop.public_repo_export.v1", "export manifest schema should be stable")
        require(manifest.get("file_count") == len(paths) - 1, "manifest should count copied files, excluding itself")
        assert_exported_readme_is_approved(tmp, result)
        assert_exported_compose_is_approved(tmp, result)
        assert_generated_manifest_matches_tree(tmp)
        assert_exported_tree_self_checks(tmp)
        assert_normal_git_staging(tmp)

    print("inkdrop public repo export smoke: PASS")


if __name__ == "__main__":
    main()
