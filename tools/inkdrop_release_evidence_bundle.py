#!/usr/bin/env python3
"""Create repeatable InkDrop public-release evidence artifacts."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tarfile
import time
import shlex
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 1
DEFAULT_PREFIX = "inkdrop-public-release-evidence"


def _path_variants(path):
    text = str(path)
    variants = {text, text.replace("\\", "/")}
    try:
        resolved = str(Path(path).resolve())
        variants.add(resolved)
        variants.add(resolved.replace("\\", "/"))
    except OSError:
        pass
    return variants


def redact_public_text(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value)
    replacements = []
    for marker, label in (
        (ROOT, "<WORKSPACE>"),
        (Path.home(), "<HOME>"),
        (Path(tempfile.gettempdir()), "<TEMP>"),
    ):
        for variant in _path_variants(marker):
            if variant:
                replacements.append((variant, label))
    for key, value in os.environ.items():
        if any(token in key.upper() for token in ("API", "KEY", "TOKEN", "PASSWORD", "SECRET")) and value:
            replacements.append((value, "<REDACTED>"))
    for marker, label in sorted(replacements, key=lambda item: len(item[0]), reverse=True):
        text = text.replace(marker, label)
    return text.strip()


def display_command(command):
    executable_variants = _path_variants(sys.executable)
    rendered = []
    for index, part in enumerate(command):
        text = str(part)
        if index == 0 and text in executable_variants:
            rendered.append("python")
        else:
            rendered.append(redact_public_text(text))
    return rendered


def run_json(command, *, output_path, timeout):
    started = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    stdout = redact_public_text(started.stdout)
    stderr = redact_public_text(started.stderr)
    output_path.write_text(stdout, encoding="utf-8")
    payload = None
    if stdout.strip():
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            payload = None
    return {
        "command": display_command(command),
        "returncode": started.returncode,
        "ok": started.returncode == 0,
        "stdout_path": str(output_path.relative_to(ROOT)),
        "stderr": stderr,
        "json_ok": payload is not None,
        "payload": payload,
    }


def run_text(command, *, output_path, timeout):
    started = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    stdout = redact_public_text(started.stdout)
    output_path.write_text(stdout, encoding="utf-8")
    return {
        "command": display_command(command),
        "returncode": started.returncode,
        "ok": started.returncode == 0,
        "stdout_path": str(output_path.relative_to(ROOT)),
        "stderr": redact_public_text(started.stderr),
    }


def docker_context_paths(manifest):
    paths = {item["path"] for item in manifest.get("files") or [] if item.get("path")}
    paths.update(
        {
            ".dockerignore",
            ".env.example",
            "deploy/compose.release-gate.network-none.yml",
            "deploy/compose.network.example.yml",
            "docker-compose.yml",
            "tools/inkdrop_public_release_check.py",
        }
    )
    return sorted(paths)


def create_docker_only_context(manifest, output_path):
    paths = docker_context_paths(manifest)
    with tarfile.open(output_path, "w:gz") as archive:
        for relative in paths:
            source = ROOT / relative
            if source.exists() and source.is_file():
                archive.add(source, arcname=relative)
    return {
        "path": str(output_path.relative_to(ROOT)),
        "size_bytes": output_path.stat().st_size,
        "file_count": len(paths),
    }


def docker_only_command(tar_name):
    return "\n".join(
        [
            "set -euo pipefail",
            "work=$(mktemp -d /tmp/inkdrop-docker-only-release-XXXXXX)",
            f"tar --warning=no-timestamp -xzf {tar_name} -C \"$work\"",
            "cd \"$work\"",
            "python3 -B tools/inkdrop_public_release_check.py --docker-only --json > inkdrop-docker-only-release.json",
            "cat inkdrop-docker-only-release.json",
        ]
    )


def run_remote_docker_only(*, host, tar_path, output_dir, remote_dir, timeout):
    remote_tar = f"{remote_dir.rstrip('/')}/{tar_path.name}"
    remote_result = f"{remote_dir.rstrip('/')}/inkdrop-docker-only-release.json"
    local_result = output_dir / "inkdrop-docker-only-release.json"
    subprocess.run(["ssh", host, f"mkdir -p {shlex.quote(remote_dir)}"], cwd=ROOT, check=True, timeout=timeout)
    subprocess.run(["scp", str(tar_path), f"{host}:{remote_tar}"], cwd=ROOT, check=True, timeout=timeout)
    remote_script = "\n".join(
        [
            "set -euo pipefail",
            f"remote_dir={shlex.quote(remote_dir)}",
            "work=$(mktemp -d /tmp/inkdrop-docker-only-release-run-XXXXXX)",
            "cleanup() { docker run --rm -v /tmp:/hosttmp alpine:3.20 sh -c \"rm -rf /hosttmp/$(basename \\\"$work\\\")\" >/dev/null 2>&1 || rm -rf \"$work\" >/dev/null 2>&1 || true; }",
            "trap cleanup EXIT",
            f"tar --warning=no-timestamp -xzf {shlex.quote(remote_tar)} -C \"$work\"",
            "cd \"$work\"",
            f"python3 -B tools/inkdrop_public_release_check.py --docker-only --json > {shlex.quote(remote_result)}",
            f"cat {shlex.quote(remote_result)}",
        ]
    )
    run_result = subprocess.run(
        ["ssh", host, "bash -s"],
        cwd=ROOT,
        input=remote_script.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    stdout = run_result.stdout.decode("utf-8", errors="replace")
    stderr = run_result.stderr.decode("utf-8", errors="replace")
    fetch_result = subprocess.run(
        ["scp", f"{host}:{remote_result}", str(local_result)],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    if fetch_result.returncode != 0:
        local_result.write_text(stdout, encoding="utf-8")
        stderr = "\n".join(part for part in (stderr, fetch_result.stderr.strip()) if part)
    payload = None
    result_text = local_result.read_text(encoding="utf-8") if local_result.exists() else ""
    if result_text.strip():
        try:
            payload = json.loads(result_text)
        except json.JSONDecodeError:
            payload = None
    safe_stderr = redact_public_text(stderr).replace(host, "<REMOTE_HOST>").replace(remote_dir.rstrip("/"), "<REMOTE_DIR>")
    return {
        "host": "<REMOTE_HOST>",
        "remote_dir": "<REMOTE_DIR>",
        "remote_tar": f"<REMOTE_DIR>/{tar_path.name}",
        "remote_result": "<REMOTE_DIR>/inkdrop-docker-only-release.json",
        "local_result": str(local_result.relative_to(ROOT)),
        "command": ["ssh", "<REMOTE_HOST>", "bash -s"],
        "returncode": run_result.returncode,
        "fetch_returncode": fetch_result.returncode,
        "ok": run_result.returncode == 0 and fetch_result.returncode == 0 and bool(payload and payload.get("release_ready") is True),
        "stderr": safe_stderr,
        "json_ok": payload is not None,
        "payload": payload,
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Create InkDrop public-release evidence artifacts.")
    parser.add_argument(
        "--output-dir",
        default="",
        help="Evidence output directory. Defaults to release-evidence/<timestamp>.",
    )
    parser.add_argument(
        "--skip-local-release-check",
        action="store_true",
        help="Skip the long Docker-free release runner and only create support/manifest/context artifacts.",
    )
    parser.add_argument(
        "--remote-host",
        default="",
        help="Optional SSH host that can run Docker. When set, the helper copies the Docker-only context, runs the Docker-only gate, and stores the returned JSON in the evidence directory.",
    )
    parser.add_argument(
        "--remote-dir",
        default="",
        help="Remote directory for the Docker-only context/result. Defaults to /tmp/inkdrop-release-evidence-<timestamp>.",
    )
    parser.add_argument(
        "--remote-timeout",
        type=int,
        default=1800,
        help="Timeout in seconds for each SSH/SCP remote Docker-only step.",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if args.output_dir:
        output_dir = Path(args.output_dir)
        if not output_dir.is_absolute():
            output_dir = ROOT / output_dir
    else:
        output_dir = ROOT / "release-evidence" / f"{DEFAULT_PREFIX}-{stamp}"
    output_dir.mkdir(parents=True, exist_ok=True)

    results = {}
    if not args.skip_local_release_check:
        results["local_release"] = run_json(
            [sys.executable, "-B", "tools/inkdrop_public_release_check.py", "--json"],
            output_path=output_dir / "inkdrop-local-release.json",
            timeout=240,
        )
    results["install_support"] = run_json(
        [sys.executable, "-B", "tools/inkdrop_install_support_summary.py", "--create", "--json"],
        output_path=output_dir / "inkdrop-install-support.json",
        timeout=90,
    )
    results["public_repo_export"] = run_json(
        [sys.executable, "-B", "tools/inkdrop_public_repo_export.py", "--json"],
        output_path=output_dir / "inkdrop-public-repo-export.json",
        timeout=90,
    )
    results["docker_context_manifest"] = run_json(
        [sys.executable, "-B", "tools/inkdrop_docker_context_manifest.py", "--json"],
        output_path=output_dir / "inkdrop-docker-context-manifest.json",
        timeout=90,
    )
    results["docker_context_summary"] = run_text(
        [sys.executable, "-B", "tools/inkdrop_docker_context_manifest.py", "--summary"],
        output_path=output_dir / "inkdrop-docker-context-summary.txt",
        timeout=90,
    )

    manifest = results["docker_context_manifest"].get("payload") or {}
    tar_path = output_dir / "inkdrop-docker-only-release-context.tar.gz"
    docker_context = create_docker_only_context(manifest, tar_path)
    command_path = output_dir / "remote-docker-only-command.sh"
    command_path.write_text(docker_only_command(tar_path.name) + "\n", encoding="utf-8")
    remote_result = None
    if args.remote_host:
        remote_dir = args.remote_dir or f"/tmp/inkdrop-release-evidence-{stamp}"
        remote_result = run_remote_docker_only(
            host=args.remote_host,
            tar_path=tar_path,
            output_dir=output_dir,
            remote_dir=remote_dir,
            timeout=args.remote_timeout,
        )

    local_payload = (results.get("local_release") or {}).get("payload") or {}
    public_repo_export_payload = (results.get("public_repo_export") or {}).get("payload") or {}
    docker_free_ok = None if args.skip_local_release_check else bool(local_payload.get("ok"))
    remote_docker_only_ready = None if remote_result is None else bool((remote_result.get("payload") or {}).get("release_ready"))
    split_host_release_ready = (
        None
        if args.skip_local_release_check or remote_result is None
        else bool(local_payload.get("ok")) and bool(remote_docker_only_ready)
    )
    summary = {
        "schema_version": SCHEMA_VERSION,
        "output_dir": str(output_dir.relative_to(ROOT)),
        "docker_free_release_ok": docker_free_ok,
        "docker_free_release_ready": None if args.skip_local_release_check else bool(local_payload.get("release_ready")),
        "public_repo_export_ok": bool(public_repo_export_payload.get("ok")),
        "split_host_release_ready": split_host_release_ready,
        "docker_only_context": docker_context,
        "remote_docker_only_command": str(command_path.relative_to(ROOT)),
        "remote_docker_only_result": {
            key: value
            for key, value in (remote_result or {}).items()
            if key != "payload"
        } if remote_result else None,
        "remote_docker_only_release_ready": remote_docker_only_ready,
        "results": {
            key: {
                name: value
                for name, value in result.items()
                if name != "payload"
            }
            for key, result in results.items()
        },
        "next_step": (
            f"Copy {docker_context['path']} to a Docker-capable host, extract or run {command_path.name}, "
            "then preserve inkdrop-docker-only-release.json beside the local evidence."
        ),
    }
    (output_dir / "release-evidence-summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    ok = all(result.get("ok") for result in results.values())
    if remote_result is not None:
        ok = ok and bool(remote_result.get("ok"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
