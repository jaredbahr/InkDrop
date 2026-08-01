#!/usr/bin/env python3
"""Static smoke checks for the public Docker/runtime root contract."""

from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import py_compile
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import inkdrop_runtime_config
import inkdrop_preflight
import importlib.util
import re
import yaml


PRIVATE_TOKENS = (
    "/home/" + "private-user",
    "C:\\Users\\" + "PrivateUser",
    "C:/Users/" + "PrivateUser",
    "/mnt/" + "private-media",
    "192.168." + "200.",
    "192.168." + "201.",
)

IMPORT_PACKAGE_NAMES = {
    "bs4": "beautifulsoup4",
    "lxml": "lxml",
    "PIL": "Pillow",
    "py7zr": "py7zr",
    "rarfile": "rarfile",
    "requests": "requests",
    "yaml": "PyYAML",
}


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def assert_no_private_tokens(value, label="payload"):
    if isinstance(value, dict):
        for key, item in value.items():
            assert_no_private_tokens(item, f"{label}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            assert_no_private_tokens(item, f"{label}[{index}]")
        return
    if not isinstance(value, str):
        return
    for token in PRIVATE_TOKENS:
        require(token not in value, f"{label} should not expose private token {token!r}")


def path_text(path):
    return str(path).replace("\\", "/")


def assert_runtime_defaults():
    env = {}
    roots = inkdrop_runtime_config.runtime_roots(env)
    expected = {
        "config_dir": "/config",
        "state_dir": "/state",
        "log_dir": "/state/logs",
        "cache_dir": "/state/cache",
        "backup_dir": "/state/backups",
        "staging_dir": "/staging",
        "manual_inbox_dir": "/manual-inbox",
        "quarantine_dir": "/state/quarantine",
    }
    for key, value in expected.items():
        require(path_text(roots[key]) == value, f"{key} default should be {value}, got {roots[key]}")
    require(path_text(inkdrop_runtime_config.state_db_path(env)) == "/state/inkdrop-state.sqlite3", "state DB uses /state")
    require(inkdrop_runtime_config.web_host(env) == "0.0.0.0", "default web host should bind all container interfaces")
    require(inkdrop_runtime_config.web_port(env) == 8796, "default web port should be 8796")


def assert_runtime_env_overrides():
    env = {
        inkdrop_runtime_config.ENV_CONFIG_DIR: "/tmp/inkdrop-config",
        inkdrop_runtime_config.ENV_STATE_DIR: "/tmp/inkdrop-state",
        inkdrop_runtime_config.ENV_LOG_DIR: "/tmp/inkdrop-logs",
        inkdrop_runtime_config.ENV_CACHE_DIR: "/tmp/inkdrop-cache",
        inkdrop_runtime_config.ENV_BACKUP_DIR: "/tmp/inkdrop-backups",
        inkdrop_runtime_config.ENV_STAGING_DIR: "/tmp/inkdrop-staging",
        inkdrop_runtime_config.ENV_MANUAL_INBOX_DIR: "/tmp/inkdrop-inbox",
        inkdrop_runtime_config.ENV_QUARANTINE_DIR: "/tmp/inkdrop-quarantine",
    }
    roots = inkdrop_runtime_config.runtime_roots(env)
    for key, value in (
        ("config_dir", "/tmp/inkdrop-config"),
        ("state_dir", "/tmp/inkdrop-state"),
        ("log_dir", "/tmp/inkdrop-logs"),
        ("cache_dir", "/tmp/inkdrop-cache"),
        ("backup_dir", "/tmp/inkdrop-backups"),
        ("staging_dir", "/tmp/inkdrop-staging"),
        ("manual_inbox_dir", "/tmp/inkdrop-inbox"),
        ("quarantine_dir", "/tmp/inkdrop-quarantine"),
    ):
        require(path_text(roots[key]) == value, f"{key} env override should be {value}, got {roots[key]}")


def assert_runtime_state_only_uses_state_as_config():
    env = {
        inkdrop_runtime_config.ENV_STATE_DIR: "/tmp/inkdrop-state-only",
    }
    roots = inkdrop_runtime_config.runtime_roots(env)
    require(
        path_text(roots["config_dir"]) == "/tmp/inkdrop-state-only",
        "state-only direct runtime should use state dir as config dir",
    )
    require(
        path_text(inkdrop_runtime_config.kavita_db_path(env)) == "/tmp/inkdrop-state-only/kavita/kavita.db",
        "state-only direct runtime adapter defaults should stay under writable state dir",
    )


def assert_packaging_files():
    root = Path(__file__).resolve().parent
    for name in ("README.md", "Dockerfile", "docker-compose.yml", ".env.example", ".dockerignore", ".gitignore", "requirements.txt", "inkdrop-logo-mark.png", "inkdrop_container_healthcheck.py", "inkdrop_container_start.py", "inkdrop_preflight.py", "inkdrop-public-release-safety-audit.py"):
        path = root / name
        require(path.exists(), f"missing {name}")
        text = path.read_text(encoding="utf-8", errors="ignore")
        if name not in {"inkdrop-logo-mark.png", "inkdrop-public-docker-runtime-smoke.py", "inkdrop-public-release-safety-audit.py"}:
            for token in PRIVATE_TOKENS:
                require(token not in text, f"{name} contains private token {token}")
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (root / ".dockerignore").read_text(encoding="utf-8")
    gitignore = (root / ".gitignore").read_text(encoding="utf-8")
    container_start = (root / "inkdrop_container_start.py").read_text(encoding="utf-8")
    container_healthcheck = (root / "inkdrop_container_healthcheck.py").read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")
    env_example = (root / ".env.example").read_text(encoding="utf-8")
    install_doc = (root / "docs" / "inkdrop" / "docker-first-install.md").read_text(encoding="utf-8")
    requirements = (root / "requirements.txt").read_text(encoding="utf-8")
    require("/config" in compose and "/state" in compose, "compose mounts config/state volumes")
    require("${INKDROP_HOST_PORT:-${INKDROP_PORT:-8796}}:${INKDROP_PORT:-8796}" in compose, "compose maps the configurable host port to the internal web port")
    require("env_file" not in compose, "compose should not require a .env file to exist")
    require("INKDROP_COMIC_ROOT" in compose and "INKDROP_COMIC_ROOT" in (root / ".env.example").read_text(encoding="utf-8"), "compose/env expose media roots")
    for ignored in (".env", "config/", "state/", "staging/", "manual-inbox/", "library/", "*.sqlite3", "*.db", "*.cbz", "*.cbr", "*.pdf", "*.epub"):
        require(ignored in gitignore, f".gitignore should exclude {ignored}")
    require("INKDROP_UNRAR_PATH: ${INKDROP_UNRAR_PATH:-/usr/bin/unrar-free}" in compose, "compose should default to the unrar-free binary installed by the image")
    require(re.search(r"^INKDROP_UNRAR_PATH=/usr/bin/unrar-free$", env_example, flags=re.MULTILINE), ".env.example should default to the unrar-free binary installed by the image")
    compose_keys = {
        match.group(1)
        for match in re.finditer(r"\b(INKDROP_[A-Z0-9_]+)\s*:", compose)
    } | {
        match.group(1)
        for match in re.finditer(r"\$\{(INKDROP_[A-Z0-9_]+)(?::-[^}]*)?\}", compose)
    }
    env_keys = {
        match.group(1)
        for match in re.finditer(r"^(INKDROP_[A-Z0-9_]+)=", env_example, flags=re.MULTILINE)
    }
    missing_env_examples = sorted(key for key in compose_keys if key not in env_keys)
    require(not missing_env_examples, f"compose keys missing from .env.example: {missing_env_examples}")
    missing_compose_entries = sorted(key for key in env_keys if key not in compose_keys)
    require(not missing_compose_entries, f".env.example keys missing from compose environment: {missing_compose_entries}")
    require("INKDROP_SLSKD_WEB_URL" in compose_keys, "compose passes SLSKD web URL through to the container")
    require("INKDROP_SUWAYOMI_API_BASE_URL" in compose_keys, "compose passes Suwayomi API base URL through to the container")
    require("INKDROP_KAVITA_URL" in compose_keys, "compose passes Kavita URL through to the container")
    require("INKDROP_KOMGA_URL" in compose_keys, "compose passes Komga URL through to the container")
    require("INKDROP_MANUAL_SOURCE_IMPORT_API_URL" in compose_keys, "compose passes manual-source import callback URL through to the container")
    require("${INKDROP_SOURCE_WORKER_PROWLARR_ALLOWED_HOSTS:-}" in compose, "source-worker Prowlarr allowed hosts should default blank in compose")
    require("${INKDROP_TRUSTED_PROWLARR_HOSTS:-}" in compose, "trusted Prowlarr hosts should default blank in compose")
    require("${INKDROP_MANUAL_SOURCE_IMPORT_API_URL:-}" in compose, "manual-source import callback override should default blank in compose")
    require("${INKDROP_MARK_WAITING_API_URL:-}" in compose, "manual-source mark-waiting callback override should default blank in compose")
    require("${INKDROP_WEB_BASE_URL:-}" in compose, "web base URL should default blank in compose to avoid stale custom-port callbacks")
    require(re.search(r"^INKDROP_SOURCE_WORKER_PROWLARR_ALLOWED_HOSTS=$", env_example, flags=re.MULTILINE), "source-worker Prowlarr allowed hosts should default blank in .env.example")
    require(re.search(r"^INKDROP_TRUSTED_PROWLARR_HOSTS=$", env_example, flags=re.MULTILINE), "trusted Prowlarr hosts should default blank in .env.example")
    require(re.search(r"^INKDROP_MANUAL_SOURCE_IMPORT_API_URL=$", env_example, flags=re.MULTILINE), "manual-source import callback override should default blank in .env.example")
    require(re.search(r"^INKDROP_MARK_WAITING_API_URL=$", env_example, flags=re.MULTILINE), "manual-source mark-waiting callback override should default blank in .env.example")
    require(re.search(r"^INKDROP_WEB_BASE_URL=$", env_example, flags=re.MULTILINE), "web base URL should default blank in .env.example")
    require("inkdrop_container_healthcheck.py" in compose and "/status.json" not in compose, "compose healthcheck should delegate to the InkDrop healthcheck helper")
    require('org.opencontainers.image.title="InkDrop"' in dockerfile, "Dockerfile should label the image title")
    require('org.opencontainers.image.source="https://github.com/jaredbahr/inkdrop-dev"' in dockerfile, "Dockerfile should label the public source repository")
    require("inkdrop_container_healthcheck.py" in dockerfile and "HEALTHCHECK" in dockerfile, "Dockerfile healthcheck should use the InkDrop healthcheck helper")
    require("COPY . ." not in dockerfile, "Dockerfile should not copy the whole homelab workspace into the public image")
    require("COPY inkdrop-logo-mark.png ./" in dockerfile, "Dockerfile should copy the InkDrop logo asset explicitly")
    require("COPY web/static/img/inkdrop-auth-backdrop.webp ./web/static/img/inkdrop-auth-backdrop.webp" in dockerfile, "Dockerfile should copy the project-owned auth backdrop explicitly")
    require("COPY inkdrop*.py" not in dockerfile and "kavita*.py" not in dockerfile, "Dockerfile should not use broad runtime Python COPY globs")
    docker_context_root_py = [
        line[2:]
        for line in dockerignore.splitlines()
        if line.startswith("!/")
        and line.endswith(".py")
        and "/" not in line[2:]
    ]
    require(docker_context_root_py, ".dockerignore should allowlist runtime Python modules by exact filename")
    for module_name in docker_context_root_py:
        require(module_name in dockerfile, f"Dockerfile should explicitly copy {module_name}")
    require(
        "!/inkdrop_process_lifecycle.py" in dockerignore
        and "inkdrop_process_lifecycle.py" in dockerfile,
        "web/worker image context should include the shared child lifecycle module",
    )
    require(
        "!/inkdrop_library_identity.py" in dockerignore
        and "inkdrop_library_identity.py" in dockerfile,
        "runtime image should package the canonical library identity module",
    )
    require(
        "import inkdrop_process_lifecycle" in (root / "inkdrop_web.py").read_text(encoding="utf-8")
        and "import inkdrop_process_lifecycle" in (root / "inkdrop_container_scheduler.py").read_text(encoding="utf-8"),
        "both web and worker entrypoints should import the packaged child lifecycle module",
    )
    require("COPY docs/inkdrop-source-candidate-catalog-20260702.json ./docs/inkdrop-source-candidate-catalog-20260702.json" in dockerfile, "Dockerfile should copy the runtime source catalog explicitly")
    require("COPY tools/inkdrop_install_support_summary.py ./tools/inkdrop_install_support_summary.py" in dockerfile, "Dockerfile should copy the install support summary helper explicitly")
    require("tools/inkdrop_public_release_check.py ./tools/inkdrop_public_release_check.py" not in dockerfile, "Dockerfile should not copy the local release runner into the runtime image")
    require(
        'org.opencontainers.image.description="InkDrop comics and manga acquisition automation"' in dockerfile,
        "Dockerfile should use the neutral InkDrop OCI description",
    )
    require("p7zip-full" in dockerfile, "Dockerfile should install 7z archive tooling")
    require("unrar-free" in dockerfile, "Dockerfile should install unrar fallback tooling")
    require('CMD ["python", "-B", "inkdrop_container_start.py"]' in dockerfile, "Dockerfile should start through the public InkDrop container shim")
    require("inkdrop_preflight.run_preflight(create=True, strict_dependencies=True, strict_runtime_tools=True)" in container_start, "container start shim should run strict preflight first")
    require("inkdrop_preflight.run_preflight(create=True, strict_dependencies=True, strict_runtime_tools=True)" in container_healthcheck, "container healthcheck should run strict preflight")
    require('conn.request("GET", "/status.json"' in container_healthcheck, "container healthcheck should probe /status.json")
    require("inkdrop_runtime_config.web_port(strict=True)" in container_healthcheck, "container healthcheck should use configured web port")
    require("healthcheck_schema_version" in container_healthcheck, "container healthcheck should emit machine-readable JSON")
    require("import inkdrop_public_contracts" in container_start, "container start shim should use shared public contract constants")
    require("inkdrop_public_contracts.PREFLIGHT_SCHEMA_VERSION" in container_start, "container start shim should report the shared preflight schema version on shim-owned failures")
    require("json.dumps(payload" in container_start and "file=sys.stderr" in container_start, "container start shim should emit JSON preflight diagnostics on failure")
    require("os.execvp" in container_start and "inkdrop_web.py" in container_start, "container start shim should exec the public InkDrop web entrypoint")
    for package in ("requests", "PyYAML", "beautifulsoup4", "lxml", "py7zr", "rarfile"):
        require(re.search(rf"^{re.escape(package)}(?:[<>=!~ ]|$)", requirements, flags=re.MULTILINE | re.IGNORECASE), f"requirements.txt should include {package}")
    require("docker compose up -d --build" in readme, "README contains Docker quick start")
    require("A `.env` file is not required" in readme, "README should state .env is optional for clean startup")
    require("closed alpha" in readme.lower() and "not a broader" in readme.lower(), "README should state the closed-alpha channel near the landing content")
    require("default `main` branch is project history" in readme and "not the current QA install" in readme, "README should not present main as the current closed-alpha install channel")
    require("<validated-commit-from-github-prerelease>" in readme and "github.com/jaredbahr/inkdrop-dev/releases" in readme, "README should direct invited testers to validated prerelease evidence")
    require("same InkDrop image as two services" in readme and "`inkdrop-worker`" in readme, "README should explain the two-service single-image layout")
    require("does **not** install updates automatically" in readme, "README should not imply a built-in automatic updater")
    require(readme.count("inkdrop-logo-mark.png") >= 2 and "web/static/img/inkdrop-auth-backdrop.webp" not in readme, "README visuals should use only the logo already published on main")
    require("private qa screenshots are intentionally excluded" in readme.lower(), "README should distinguish published visuals from private QA screenshots")
    require("A `.env` file is not required" in install_doc, "Docker install docs should state .env is optional for clean startup")
    require("Optional: copy to .env" in env_example, ".env.example should be framed as optional, not mandatory")
    require("tools/inkdrop_public_release_check.py" in readme, "README documents the local public-release check runner")
    require("tools/inkdrop_release_evidence_bundle.py" in readme, "README documents the release evidence bundle helper")
    require("--remote-host" in readme, "README documents optional remote Docker evidence execution")
    require("inkdrop_preflight.py --create --json" in readme, "README documents preflight")
    require("docker compose build inkdrop" in readme and "Docker-capable host" in readme, "README documents the remaining Docker-capable release gate")
    for secret_key in (
        "INKDROP_COMICVINE_API_KEY",
        "INKDROP_PROWLARR_API_KEY",
        "INKDROP_SABNZBD_API_KEY",
        "INKDROP_QBITTORRENT_PASSWORD",
        "INKDROP_SUWAYOMI_API_BASE_URL",
        "INKDROP_KAVITA_URL",
        "INKDROP_KOMGA_URL",
    ):
        require(re.search(rf"^{secret_key}=$", env_example, flags=re.MULTILINE), f"{secret_key} should default blank in .env.example")
        require(f"${{{secret_key}:-}}" in compose, f"{secret_key} should default blank in compose")
    for ignored in (
        "*",
        "!/Dockerfile",
        "!/requirements.txt",
        "!/inkdrop-logo-mark.png",
        "!/inkdrop_acquire.py",
        "!/inkdrop_public_contracts.py",
        "!/inkdrop_web.py",
        "!/inkdrop_state.py",
        "!/inkdrop_runtime_config.py",
        "!/inkdrop_container_start.py",
        "!/docs/",
        "/docs/**",
        "!/docs/inkdrop-source-candidate-catalog-20260702.json",
        "!/tools/",
        "/tools/**",
        "!/tools/inkdrop_install_support_summary.py",
        "*.sqlite3",
        "*.cbz",
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
        "backups/",
        "state/",
        ".env",
        "docs/inkdrop/AGENT_HANDOFF.md",
    ):
        require(ignored in dockerignore, f".dockerignore should exclude {ignored}")
    require("!/inkdrop*.py" not in dockerignore, ".dockerignore should not use broad InkDrop Python context globs")
    require("!/kavita*.py" not in dockerignore, ".dockerignore should not use broad Kavita Python context globs")


def assert_compose_yaml_contract():
    root = Path(__file__).resolve().parent
    compose = yaml.safe_load((root / "docker-compose.yml").read_text(encoding="utf-8"))
    require(isinstance(compose, dict), "compose should parse as a YAML mapping")
    services = compose.get("services")
    require(isinstance(services, dict), "compose should define services")
    inkdrop = services.get("inkdrop")
    require(isinstance(inkdrop, dict), "compose should define the inkdrop service")
    build = inkdrop.get("build")
    require(isinstance(build, dict) and build.get("context") == ".", "inkdrop service should build from repository root")
    build_args = build.get("args") if isinstance(build.get("args"), dict) else {}
    for key in ("INKDROP_VERSION", "INKDROP_COMMIT_SHA", "INKDROP_BUILD_DATE", "INKDROP_RELEASE_CHANNEL"):
        require(key in build_args, f"compose should inject {key} into the image build")
    require("container_name" not in inkdrop, "compose should not pin a global Docker container name")
    require(inkdrop.get("restart") == "unless-stopped", "inkdrop service should restart unless stopped")
    require("network_mode" not in inkdrop, "compose should not use host networking for the public install")
    require(inkdrop.get("privileged") is not True, "compose should not run the public install as privileged")
    require("pid" not in inkdrop, "compose should not share the host PID namespace")
    require("ipc" not in inkdrop, "compose should not share the host IPC namespace")
    require("devices" not in inkdrop, "compose should not require host devices for the public install")
    require("cap_add" not in inkdrop, "compose should not add Linux capabilities for the public install")
    require(inkdrop.get("ports") == ["${INKDROP_HOST_PORT:-${INKDROP_PORT:-8796}}:${INKDROP_PORT:-8796}"], "compose should expose only the configured host port and map it to the internal container port")
    volumes = inkdrop.get("volumes") or []
    for volume in ("./config:/config", "./state:/state", "./staging:/staging", "./manual-inbox:/manual-inbox", "./library:/library"):
        require(volume in volumes, f"compose should mount {volume}")
    environment = inkdrop.get("environment")
    require(isinstance(environment, dict), "compose should use a mapping for environment")
    for key in ("INKDROP_CONFIG_DIR", "INKDROP_STATE_DIR", "INKDROP_STAGING_DIR", "INKDROP_MANUAL_INBOX_DIR", "INKDROP_COMIC_ROOT", "INKDROP_MANGA_ROOT"):
        require(key in environment, f"compose should pass {key}")
    healthcheck = inkdrop.get("healthcheck") or {}
    require(healthcheck.get("test") == ["CMD", "python", "-B", "inkdrop_container_healthcheck.py", "--timeout", "5"], "compose healthcheck should probe strict preflight and /status.json")
    worker = services.get("inkdrop-worker") or {}
    worker_build = worker.get("build") if isinstance(worker.get("build"), dict) else {}
    require(worker_build.get("context") == ".", "worker should build from repository root")
    require(worker_build.get("args") == build_args, "web and worker should share identical build metadata")


def _env_example_values(root):
    values = {}
    for raw in (root / ".env.example").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _compose_interpolate(value, env):
    if isinstance(value, str):
        previous = None
        while value != previous and "${" in value:
            previous = value
            value = re.sub(
                r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(?:([^{}]*)|(\$\{[^{}]+\})))?\}",
                lambda match: str(env.get(match.group(1)))
                if env.get(match.group(1)) not in (None, "")
                else str(match.group(2) or match.group(3) or ""),
                value,
            )
        return value
    if isinstance(value, list):
        return [_compose_interpolate(item, env) for item in value]
    if isinstance(value, dict):
        return {key: _compose_interpolate(item, env) for key, item in value.items()}
    return value


def assert_compose_env_interpolation_contract():
    root = Path(__file__).resolve().parent
    compose = yaml.safe_load((root / "docker-compose.yml").read_text(encoding="utf-8"))
    env_example = _env_example_values(root)
    resolved_default = _compose_interpolate(compose, env_example)
    inkdrop_default = resolved_default["services"]["inkdrop"]
    require(inkdrop_default["ports"] == ["8796:8796"], "default compose interpolation should expose 8796:8796")
    require(inkdrop_default["environment"]["INKDROP_PORT"] == "8796", "default compose interpolation should pass INKDROP_PORT=8796")
    require(inkdrop_default["environment"]["INKDROP_HOST_PORT"] == "8796", "default compose interpolation should pass INKDROP_HOST_PORT=8796")
    require(inkdrop_default["environment"]["INKDROP_CONTAINER_WEB_BASE_URL"] == "http://inkdrop:8796", "default worker callback should use the internal container port")
    require(inkdrop_default["environment"]["INKDROP_WEB_BASE_URL"] == "", "default compose interpolation should keep INKDROP_WEB_BASE_URL blank")
    require(inkdrop_default["environment"]["INKDROP_KAPOWARR_DB"] == "", "default compose interpolation should keep Kapowarr DB blank")
    require(inkdrop_default["environment"]["INKDROP_KAVITA_DB"] == "", "default compose interpolation should keep Kavita DB blank")
    require(inkdrop_default["environment"]["INKDROP_SAB_PATH_MAPPINGS"] == "", "default compose interpolation should keep SAB path mappings blank")
    with tempfile.TemporaryDirectory(prefix="inkdrop-compose-default-preflight-") as tmp:
        tmp_root = Path(tmp)
        preflight_env = dict(inkdrop_default["environment"])
        preflight_env.update(
            {
                inkdrop_runtime_config.ENV_CONFIG_DIR: str(tmp_root / "config"),
                inkdrop_runtime_config.ENV_STATE_DIR: str(tmp_root / "state"),
                inkdrop_runtime_config.ENV_LOG_DIR: str(tmp_root / "logs"),
                inkdrop_runtime_config.ENV_CACHE_DIR: str(tmp_root / "cache"),
                inkdrop_runtime_config.ENV_BACKUP_DIR: str(tmp_root / "backups"),
                inkdrop_runtime_config.ENV_STAGING_DIR: str(tmp_root / "staging"),
                inkdrop_runtime_config.ENV_MANUAL_INBOX_DIR: str(tmp_root / "manual-inbox"),
                inkdrop_runtime_config.ENV_QUARANTINE_DIR: str(tmp_root / "quarantine"),
            }
        )
        compose_preflight = inkdrop_preflight.run_preflight(preflight_env, create=True)
        require(compose_preflight["web"]["callback_base_url"] == "http://inkdrop:8796", "default Compose worker callback should use service DNS")
        require(compose_preflight["web"]["callback_base_source"] == "INKDROP_CONTAINER_WEB_BASE_URL", "default Compose worker callback should report the container source")
        require(compose_preflight["configured_adapters"]["kapowarr"]["configured"] is False, "default Compose env should not configure Kapowarr adapter")
        require(compose_preflight["configured_adapters"]["kapowarr"]["existing_path_missing_keys"] == [], "default Compose env should not report missing Kapowarr DB path")
        require(compose_preflight["configured_adapters"]["kavita"]["configured"] is False, "default Compose env should not configure Kavita adapter")
        require(compose_preflight["configured_adapters"]["kavita"]["existing_path_missing_keys"] == [], "default Compose env should not report missing Kavita DB path")
    custom_env = dict(env_example)
    custom_env.update(
        {
            "INKDROP_PORT": "8899",
            "INKDROP_HOST_PORT": "9876",
            "INKDROP_WEB_BASE_URL": "https://inkdrop.example.test",
            "INKDROP_SAB_PATH_MAPPINGS": "//server/share=/staging/downloads",
        }
    )
    resolved_custom = _compose_interpolate(compose, custom_env)
    inkdrop_custom = resolved_custom["services"]["inkdrop"]
    require(inkdrop_custom["ports"] == ["9876:8899"], "custom compose interpolation should map the custom host port to the custom container port")
    require(inkdrop_custom["environment"]["INKDROP_PORT"] == "8899", "custom compose interpolation should pass custom INKDROP_PORT")
    require(inkdrop_custom["environment"]["INKDROP_HOST_PORT"] == "9876", "custom compose interpolation should pass custom INKDROP_HOST_PORT")
    require(inkdrop_custom["environment"]["INKDROP_CONTAINER_WEB_BASE_URL"] == "http://inkdrop:8899", "custom worker callback should follow the internal container port")
    require(inkdrop_custom["environment"]["INKDROP_WEB_BASE_URL"] == "https://inkdrop.example.test", "custom compose interpolation should pass explicit web base URL")
    require(inkdrop_custom["environment"]["INKDROP_SAB_PATH_MAPPINGS"] == "//server/share=/staging/downloads", "custom compose interpolation should pass SAB path mappings")
    legacy_env = dict(env_example)
    legacy_env.pop("INKDROP_HOST_PORT", None)
    legacy_env["INKDROP_PORT"] = "8899"
    inkdrop_legacy = _compose_interpolate(compose, legacy_env)["services"]["inkdrop"]
    require(inkdrop_legacy["ports"] == ["8899:8899"], "existing INKDROP_PORT-only installs should keep the prior host/container mapping")


def assert_public_release_workflow_contract():
    root = Path(__file__).resolve().parent
    path = root / ".github" / "workflows" / "inkdrop-public-release.yml"
    require(path.exists(), "public-release GitHub Actions workflow should exist")
    workflow_text = path.read_text(encoding="utf-8")
    workflow = yaml.safe_load(workflow_text)
    require(isinstance(workflow, dict), "public-release workflow should parse as YAML")
    require("workflow_dispatch" in (workflow.get(True) or workflow.get("on") or {}), "public-release workflow should support manual dispatch")
    require("pull_request" in (workflow.get(True) or workflow.get("on") or {}), "public-release workflow should run on pull requests")
    jobs = workflow.get("jobs") or {}
    public_release_job = jobs.get("public_release") or {}
    require(public_release_job.get("timeout-minutes") == 60, "public-release workflow should allow its measured release gates while bounding total runtime")
    step_timeouts = {
        step.get("name"): step.get("timeout-minutes")
        for step in public_release_job.get("steps") or []
        if isinstance(step, dict) and step.get("name")
    }
    expected_step_timeouts = {
        "Install Python dependencies": 5,
        "Run static public-release checks": 15,
        "Summarize Docker context manifest": 2,
        "Validate Docker Compose config": 2,
        "Build Docker image": 30,
        "Run strict container preflight": 5,
        "Verify container install support summary": 5,
        "Verify release-ready JSON gate": 35,
    }
    for step_name, timeout_minutes in expected_step_timeouts.items():
        require(step_timeouts.get(step_name) == timeout_minutes, f"public-release workflow should bound {step_name}")
    for needle in (
        "python -B tools/inkdrop_public_release_check.py --strict-host-dependencies",
        "python -B tools/inkdrop_public_http_smoke.py --json > inkdrop-public-http-smoke.json",
        "python -B tools/inkdrop_docker_context_manifest.py --summary",
        "python -B tools/inkdrop_docker_context_manifest.py --json > docker-context-manifest.json",
        "docker-context-manifest-summary.txt",
        "docker-context-manifest.json",
        "GITHUB_STEP_SUMMARY",
        "docker compose config --quiet",
        "docker compose build inkdrop",
        "docker compose run --rm inkdrop python -B inkdrop_preflight.py --create --quiet --strict-dependencies --strict-runtime-tools",
        "docker compose run --rm inkdrop python -B inkdrop_container_healthcheck.py --preflight-only",
        "docker compose run --rm inkdrop python -B tools/inkdrop_install_support_summary.py --create --json",
        "inkdrop-install-support-summary.json",
        "inkdrop-public-http-smoke.json",
        'assert payload["install_support_schema_version"] == 1',
        'assert payload["preflight_schema_version"] == 1',
        'assert payload["preflight"]["ok"] is True',
        'assert "effective_config" not in payload["preflight"]',
        "python -B tools/inkdrop_public_release_check.py --docker --require-docker --skip-docker-build --json",
        "inkdrop-release-check.json",
        "actions/upload-artifact@v4",
        "inkdrop-public-release-evidence",
        "if-no-files-found: ignore",
        "retention-days: 14",
        'assert payload["release_check_schema_version"] == 1',
        'assert payload["release_ready"] is True',
        'assert payload["docker_build_skipped"] is True',
        'assert payload["docker_gate_status"] == "passed"',
        'assert build["skipped"] is True',
        "tools/inkdrop_public_release_check.py",
        "tools/inkdrop_install_support_summary.py",
        "tools/inkdrop_public_http_smoke.py",
        "tools/inkdrop_docker_context_manifest.py",
        ".gitignore",
        "inkdrop*.py",
        "kavita*.py",
        "inkdrop-logo-mark.png",
        "docs/inkdrop-source-candidate-catalog-20260702.json",
    ):
        require(needle in workflow_text, f"public-release workflow missing command: {needle}")


def assert_public_release_runner_contract():
    root = Path(__file__).resolve().parent
    runner = (root / "tools" / "inkdrop_public_release_check.py").read_text(encoding="utf-8")
    evidence_helper = (root / "tools" / "inkdrop_release_evidence_bundle.py").read_text(encoding="utf-8")
    require("tools/inkdrop_state_schema_audit.py" in runner, "release runner should include the static state schema audit")
    require("tools/inkdrop_web_surface_audit.py" in runner, "release runner should include the static web surface audit")
    require("tools/inkdrop_public_http_smoke.py" in runner, "release runner should include the live public HTTP smoke under strict host checks")
    require("def run_public_http_smoke" in runner, "release runner should expose a live public HTTP smoke helper")
    require('"public_http_smoke"' in runner, "release runner should name the public HTTP smoke result")
    require("tools/inkdrop_docker_context_manifest.py" in runner, "release runner should include the Docker context manifest check")
    require('"docker_context_manifest", 60' in runner, "release runner should bound Docker context manifest runtime")
    require('"docker_context_manifest"' in runner, "release runner should name the Docker context manifest result")
    require("if strict_host_dependencies:" in runner, "release runner should only require live HTTP smoke after strict host dependencies are enabled")
    require("tools/inkdrop_settings_api_surface_audit.py" in runner, "release runner should include the settings API surface audit")
    require('"settings_api_surface_audit", 60' in runner, "release runner should bound settings API surface audit runtime")
    require("inkdrop-settings-section-contract-smoke.py" in runner, "release runner should include the settings section contract smoke")
    require('"settings_section_contract_smoke", 60' in runner, "release runner should bound settings section contract smoke runtime")
    require("tools/inkdrop_settings_sync_smoke.py" in runner, "release runner should include the settings sync smoke")
    require('"settings_sync_smoke", 60' in runner, "release runner should bound settings sync smoke runtime")
    require("inkdrop-provider-test-contract-smoke.py" in runner, "release runner should include the provider test contract smoke")
    require('"provider_test_contract_smoke", 60' in runner, "release runner should bound provider test contract smoke runtime")
    require("inkdrop_public_contracts.RELEASE_CHECK_SCHEMA_VERSION" in runner, "release runner should use shared release-check JSON schema version")
    require('"release_check_schema_version": inkdrop_public_contracts.RELEASE_CHECK_SCHEMA_VERSION' in runner, "release runner should expose schema version in JSON")
    require("RELEASE_BLOCKER_SCHEMA_VERSION = 1" in runner, "release runner should version the release-blocker JSON contract")
    require('"release_blocker_schema_version": RELEASE_BLOCKER_SCHEMA_VERSION' in runner, "release runner should expose release-blocker schema version")
    require('"release_blockers": release_blockers' in runner, "release runner should expose machine-readable release blockers")
    require("def build_release_blockers" in runner, "release runner should build a named release-blocker summary")
    require("release_ready = bool(ok and docker_gate_status == \"passed\")" in runner, "release runner should distinguish local ok from release-ready Docker gate")
    require('"docker_gate_status": docker_gate_status' in runner, "release runner should report Docker gate status in JSON")
    require("release gate: incomplete" in runner, "release runner should print an incomplete release-gate message when Docker gate has not passed")
    require("release gate: passed" in runner, "release runner should print an explicit passed release-gate message")
    require("subprocess.TimeoutExpired" in runner, "release runner should convert stalled commands into bounded failures")
    require('"timed_out": True' in runner, "release runner should report timed-out checks in JSON")
    require('"timeout_seconds": timeout' in runner, "release runner should report timeout bounds in JSON")
    require('"docker_compose_build", 1800' in runner, "release runner should give Docker image builds a bounded timeout")
    require('"docker_container_healthcheck_preflight"' in runner, "release runner should validate the container healthcheck helper in the built image")
    require("inkdrop_container_healthcheck.py" in runner and "--preflight-only" in runner, "release runner should run container healthcheck preflight mode")
    require("INKDROP_RELEASE_CHECK_SELFTEST_SKIP_RUNTIME_SMOKE" in runner, "release runner should expose a self-test-only runtime-smoke recursion guard")
    require("--skip-docker-build" in runner, "release runner should support reusing a previously built Docker image")
    require("--docker-only" in runner, "release runner should support Docker-only gate validation on a Docker-capable host")
    require('"docker_only": bool(docker_only)' in runner, "release runner JSON should report Docker-only mode")
    require("docker_only=args.docker_only" in runner, "release runner should pass Docker-only mode into run_checks")
    require('"docker_build_skipped": bool(include_docker and skip_docker_build)' in runner, "release runner JSON should report skipped Docker build mode")
    require("def run_install_support_summary" in runner, "release runner should execute the redacted install support summary")
    require('"install_support_summary"' in runner, "release runner should name the install support summary result")
    require(
        '("public_docker_runtime_smoke", 600,' in runner,
        "release runner should allow the complete public runtime smoke its measured execution budget",
    )
    require("tools/inkdrop_install_support_summary.py" in runner, "release runner should call the install support summary helper")
    require("inkdrop-release-notes-version-smoke.py" in runner, "release runner should include release-note/version alignment")
    require('("release_notes_version_smoke", 60' in runner, "release runner should bound release-note/version alignment runtime")
    require("tools/inkdrop_release_evidence_bundle.py" in runner, "release runner should compile the release evidence bundle helper")
    for label, text in (
        ("release runner", runner),
        ("release evidence helper", evidence_helper),
    ):
        require("def redact_public_text" in text, f"{label} should redact public JSON/text output")
        require("def display_command" in text, f"{label} should normalize serialized command paths")
        require("<WORKSPACE>" in text and "<HOME>" in text and "<TEMP>" in text, f"{label} should use stable path placeholders")


def assert_public_release_runner_json_contract():
    root = Path(__file__).resolve().parent
    env = dict(os.environ)
    env["INKDROP_RELEASE_CHECK_SELFTEST_SKIP_RUNTIME_SMOKE"] = "1"
    contract_probe = r'''
import tools.inkdrop_public_release_check as release_check

local_names = {name for name, _timeout, _command in release_check.LOCAL_CHECKS}
run_command = release_check.run_command

def run_contract_command(name, command, *, env=None, timeout=None):
    if name not in local_names:
        return run_command(name, command, env=env, timeout=timeout)
    return {
        "name": name,
        "command": release_check.display_command(command),
        "ok": True,
        "returncode": 0,
        "timed_out": False,
        "timeout_seconds": timeout,
        "stdout": "validated by the outer release check; JSON contract probe only",
        "stderr": "",
    }

release_check.run_command = run_contract_command
raise SystemExit(release_check.main(["--json"]))
'''
    with tempfile.TemporaryDirectory(prefix="inkdrop-release-runner-contract-") as tmp:
        runtime_root = Path(tmp)
        runtime_paths = {
            "INKDROP_CONFIG_DIR": runtime_root / "config",
            "INKDROP_STATE_DIR": runtime_root / "state",
            "INKDROP_LOCK_DIR": runtime_root / "state" / "locks",
            "INKDROP_LOG_DIR": runtime_root / "state" / "logs",
            "INKDROP_CACHE_DIR": runtime_root / "state" / "cache",
            "INKDROP_BACKUP_DIR": runtime_root / "state" / "backups",
            "INKDROP_STAGING_DIR": runtime_root / "staging",
            "INKDROP_MANUAL_INBOX_DIR": runtime_root / "manual-inbox",
            "INKDROP_QUARANTINE_DIR": runtime_root / "state" / "quarantine",
        }
        for key, path in runtime_paths.items():
            path.mkdir(parents=True, exist_ok=True)
            env[key] = str(path)
        result = subprocess.run(
            [sys.executable, "-B", "-c", contract_probe],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
            timeout=300,
        )
    failure_output = (result.stderr or result.stdout)[-4000:]
    try:
        payload = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise AssertionError(f"release check --json should emit valid JSON, got exit {result.returncode}: {failure_output}") from exc
    require(result.returncode == 0, f"release check --json self-test should exit 0, got {result.returncode}: {failure_output}")
    require(not result.stderr.strip(), "release check --json should not write stderr when Docker-free checks pass")
    assert_no_private_tokens(payload, "release_check_json")
    assert_no_private_tokens(result.stdout, "release_check_stdout")
    require(payload.get("release_check_schema_version") == 1, "release check --json should report schema version 1")
    require(payload.get("release_blocker_schema_version") == 1, "release check --json should report release-blocker schema version 1")
    results = payload.get("results") or []
    failed_results = [item.get("name") for item in results if item.get("ok") is not True]
    require(payload.get("ok") is True, f"release check --json should report ok=true for passing local non-Docker gates; failed results: {failed_results}")
    require(payload.get("release_ready") is False, "release check --json should keep release_ready=false when Docker gate is not requested")
    require(payload.get("docker_gate_status") == "not_requested", "release check --json should report Docker gate not_requested by default")
    require(payload.get("docker_requested") is False, "release check --json should report docker_requested=false by default")
    require(payload.get("docker_required") is False, "release check --json should report docker_required=false by default")
    require(payload.get("docker_only") is False, "release check --json should report docker_only=false by default")
    require(payload.get("docker_build_skipped") is False, "release check --json should report docker_build_skipped=false by default")
    require(not failed_results, f"release check --json Docker-free results should all pass: {failed_results}")
    names = {item.get("name") for item in results}
    for name in (
        "py_compile_public_release",
        "public_docker_runtime_smoke",
        "public_release_safety_audit",
        "release_notes_version_smoke",
        "docker_context_manifest",
        "settings_api_surface_audit",
        "settings_section_contract_smoke",
        "settings_sync_smoke",
        "provider_test_contract_smoke",
        "state_schema_audit",
        "web_surface_audit",
        "host_preflight",
        "install_support_summary",
    ):
        require(name in names, f"release check --json should include {name}")
    for item in results:
        require("ok" in item and "returncode" in item and "timed_out" in item, f"release check result should expose stable machine fields for {item.get('name')}")
        command = item.get("command") or []
        require(not command or command[0] != sys.executable, f"release check result should not serialize host Python path for {item.get('name')}")
    runtime_smoke = next(item for item in results if item.get("name") == "public_docker_runtime_smoke")
    require(runtime_smoke.get("skipped") is True, "release check self-test should explicitly mark runtime smoke skipped to avoid recursion")
    require("INKDROP_RELEASE_CHECK_SELFTEST_SKIP_RUNTIME_SMOKE" in runtime_smoke.get("stdout", ""), "release check self-test skipped result should explain why")
    blockers = payload.get("release_blockers") or {}
    require(blockers.get("schema_version") == 1, "release check --json should expose release-blocker schema version")
    require(blockers.get("ready") is False, "release blockers should not be ready when Docker gate is not requested")
    blocker_items = blockers.get("items") or []
    blocker_ids = {item.get("id") for item in blocker_items}
    for blocker_id in (
        "local_release_checks",
        "public_release_safety_audit",
        "install_support_summary",
        "docker_context_manifest",
        "docker_checks_requested",
        "docker_compose_build",
        "docker_container_strict_preflight",
        "docker_container_healthcheck_preflight",
    ):
        require(blocker_id in blocker_ids, f"release blockers should include {blocker_id}")
    docker_requested = next(item for item in blocker_items if item.get("id") == "docker_checks_requested")
    require(docker_requested.get("status") == "not_requested", "release blockers should explain Docker gate was not requested")
    require(docker_requested.get("ok") is False, "release blockers should treat missing Docker gate as not release-ready")
    local_blocker = next(item for item in blocker_items if item.get("id") == "local_release_checks")
    require(local_blocker.get("status") == "passed", "release blockers should report local checks as passed")
    require(local_blocker.get("ok") is True, "release blockers should mark passing local checks ok")
    support_summary = next(item for item in results if item.get("name") == "install_support_summary")
    support_payload = json.loads(support_summary.get("stdout") or "{}")
    require(support_payload.get("install_support_schema_version") == 1, "release runner support summary should report schema version 1")
    require(support_payload.get("preflight_schema_version") == 1, "release runner support summary should report preflight schema version 1")
    require(support_payload.get("release_check_schema_version") == 1, "release runner support summary should report release-check schema version 1")
    require(support_payload.get("preflight", {}).get("ok") is True, "release runner support summary should pass with temp roots")
    require("effective_config" not in support_payload.get("preflight", {}), "release runner support summary should stay redacted")
    release_gate = support_payload.get("release_gate") or {}
    require(release_gate.get("ready") is False, "release runner support summary should not mark the release gate ready")
    require(
        release_gate.get("required_command") == "python -B tools/inkdrop_public_release_check.py --docker --require-docker",
        "release runner support summary should name the required Docker release command",
    )
    require("docker_compose_build" in (release_gate.get("required_checks") or []), "release runner support summary should list Docker build as a required release gate")
    if support_payload.get("docker_available") is False:
        require(release_gate.get("status") == "docker_unavailable", "release runner support summary should classify missing Docker as docker_unavailable")
    else:
        require(release_gate.get("status") == "docker_checks_required", "release runner support summary should require Docker checks on a Docker-capable host")
    clean_guidance = support_payload.get("setup_guidance") or []
    if support_payload.get("docker_available") is False:
        require(any(item.get("area") == "docker" and item.get("severity") == "warning" for item in clean_guidance), "release runner support summary should explain missing Docker CLI")
    require(any(item.get("area") == "download_sources" for item in clean_guidance), "release runner support summary should explain clean installs with no download adapters")
    require(support_payload["preflight"]["configured_adapters"]["comicvine"]["next_step"].startswith("Set INKDROP_COMICVINE_API_KEY"), "release runner support summary should include metadata next steps")


def assert_public_release_runner_docker_unavailable_json_contract():
    if shutil.which("docker") is not None:
        return
    root = Path(__file__).resolve().parent
    env = dict(os.environ)
    env["INKDROP_RELEASE_CHECK_SELFTEST_SKIP_RUNTIME_SMOKE"] = "1"
    result = subprocess.run(
        [sys.executable, "-B", "tools/inkdrop_public_release_check.py", "--require-docker", "--json"],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=300,
    )
    require(result.returncode == 1, f"release check --require-docker --json should exit 1 when Docker is unavailable, got {result.returncode}")
    require(not result.stderr.strip(), "release check --require-docker --json should keep unavailable Docker details in JSON stdout")
    payload = json.loads(result.stdout)
    assert_no_private_tokens(payload, "release_check_require_docker_json")
    assert_no_private_tokens(result.stdout, "release_check_require_docker_stdout")
    require(payload.get("release_check_schema_version") == 1, "Docker-unavailable release JSON should report schema version 1")
    require(payload.get("release_blocker_schema_version") == 1, "Docker-unavailable release JSON should report release-blocker schema version 1")
    require(payload.get("ok") is False, "Docker-unavailable release JSON should report ok=false")
    require(payload.get("release_ready") is False, "Docker-unavailable release JSON should report release_ready=false")
    require(payload.get("docker_available") is False, "Docker-unavailable release JSON should report docker_available=false")
    require(payload.get("docker_requested") is True, "Docker-unavailable release JSON should report docker_requested=true")
    require(payload.get("docker_required") is True, "Docker-unavailable release JSON should report docker_required=true")
    require(payload.get("docker_only") is False, "Docker-unavailable release JSON should report docker_only=false for --require-docker")
    require(payload.get("docker_build_skipped") is False, "Docker-unavailable release JSON should report docker_build_skipped=false by default")
    require(payload.get("docker_gate_status") == "unavailable", "Docker-unavailable release JSON should report docker_gate_status=unavailable")
    docker_result = next((item for item in payload.get("results") or [] if item.get("name") == "docker_checks"), None)
    require(isinstance(docker_result, dict), "Docker-unavailable release JSON should include docker_checks result")
    require(docker_result.get("ok") is False, "required Docker-unavailable result should be failing")
    require(docker_result.get("skipped") is False, "required Docker-unavailable result should not be marked skipped")
    require(docker_result.get("returncode") == 1, "required Docker-unavailable result should use returncode 1")
    require(docker_result.get("timed_out") is False, "Docker-unavailable result should not be a timeout")
    require(docker_result.get("command") == ["docker"], "Docker-unavailable result should identify docker command")
    require("Docker CLI is not available" in docker_result.get("stderr", ""), "Docker-unavailable result should explain missing Docker CLI")
    blockers = payload.get("release_blockers") or {}
    require(blockers.get("ready") is False, "Docker-unavailable release blockers should not be ready")
    blocker_items = blockers.get("items") or []
    docker_available = next((item for item in blocker_items if item.get("id") == "docker_available"), None)
    require(isinstance(docker_available, dict), "Docker-unavailable release blockers should include docker_available")
    require(docker_available.get("status") == "unavailable", "Docker-unavailable release blockers should explain Docker is unavailable")
    require(docker_available.get("ok") is False, "Docker-unavailable release blockers should fail Docker availability")


def assert_public_release_runner_docker_only_unavailable_json_contract():
    if shutil.which("docker") is not None:
        return
    root = Path(__file__).resolve().parent
    result = subprocess.run(
        [sys.executable, "-B", "tools/inkdrop_public_release_check.py", "--docker-only", "--json"],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=120,
    )
    require(result.returncode == 1, f"release check --docker-only --json should exit 1 when Docker is unavailable, got {result.returncode}")
    require(not result.stderr.strip(), "release check --docker-only --json should keep unavailable Docker details in JSON stdout")
    payload = json.loads(result.stdout)
    assert_no_private_tokens(payload, "release_check_docker_only_json")
    assert_no_private_tokens(result.stdout, "release_check_docker_only_stdout")
    require(payload.get("ok") is False, "Docker-only unavailable JSON should report ok=false")
    require(payload.get("release_ready") is False, "Docker-only unavailable JSON should report release_ready=false")
    require(payload.get("docker_available") is False, "Docker-only unavailable JSON should report docker_available=false")
    require(payload.get("docker_requested") is True, "Docker-only JSON should report docker_requested=true")
    require(payload.get("docker_required") is True, "Docker-only JSON should report docker_required=true")
    require(payload.get("docker_only") is True, "Docker-only JSON should report docker_only=true")
    names = {item.get("name") for item in payload.get("results") or []}
    require("docker_checks" in names, "Docker-only unavailable JSON should include docker_checks result")
    require("py_compile_public_release" not in names, "Docker-only mode should not rerun Docker-free local checks")
    blockers = payload.get("release_blockers") or {}
    local_blocker = next((item for item in blockers.get("items") or [] if item.get("id") == "local_release_checks"), None)
    require(isinstance(local_blocker, dict), "Docker-only blockers should include local_release_checks")
    require(local_blocker.get("status") == "provided_separately", "Docker-only blockers should mark Docker-free checks as provided separately")
    require(local_blocker.get("ok") is True, "Docker-only blockers should not fail local checks that are intentionally out of scope")


def assert_public_release_docs_contract():
    root = Path(__file__).resolve().parent
    readme = (root / "README.md").read_text(encoding="utf-8")
    install_doc = (root / "docs" / "inkdrop" / "docker-first-install.md").read_text(encoding="utf-8")
    arr_stack_doc = (root / "docs" / "inkdrop" / "arr-stack-deployment-plan.md").read_text(encoding="utf-8")
    network_override = (root / "compose.network.example.yml").read_text(encoding="utf-8")
    evidence_helper = (root / "tools" / "inkdrop_release_evidence_bundle.py").read_text(encoding="utf-8")
    compose_plan_helper = (root / "tools" / "inkdrop_compose_deployment_plan.py").read_text(encoding="utf-8")
    require("INKDROP_EXTERNAL_NETWORK" in network_override, "network override should require an explicit external network name")
    require("external: true" in network_override, "network override should join an existing external network")
    require("name: ${INKDROP_EXTERNAL_NETWORK:?" in network_override, "network override should fail closed without a network name")
    for private_hint in ("arr-docker", "192.168.", "private-user", "/mnt/private-media"):
        require(private_hint not in network_override, f"network override should not contain private hint {private_hint}")
    require("split_host_release_ready" in evidence_helper, "release evidence helper should emit split-host readiness")
    require('"compose.network.example.yml"' in evidence_helper, "release evidence helper should include the optional network override")
    require("--output" in compose_plan_helper and "write_overlay_if_requested" in compose_plan_helper, "compose deployment helper should support explicit overlay output")
    require("Output file already exists; pass --force" in compose_plan_helper, "compose deployment helper should refuse accidental overwrite")
    require("Refusing to write overlay because service" in compose_plan_helper, "compose deployment helper should refuse duplicate service overlay")
    require("proposed_service_block" in compose_plan_helper, "compose deployment helper should emit a proposed service block")
    require("arr-stack-deployment-plan.md" in readme, "README should link the existing Arr stack deployment plan")
    require("arr-stack-deployment-plan.md" in install_doc, "Docker install docs should link the existing Arr stack deployment plan")
    require("INKDROP_PROWLARR_URL: ${INKDROP_PROWLARR_URL:-}" in arr_stack_doc, "Arr stack plan should leave Prowlarr URL blank by default")
    require("INKDROP_SABNZBD_URL: ${INKDROP_SABNZBD_URL:-}" in arr_stack_doc, "Arr stack plan should leave SABnzbd URL blank by default")
    require("INKDROP_QBITTORRENT_URL: ${INKDROP_QBITTORRENT_URL:-}" in arr_stack_doc, "Arr stack plan should leave qBittorrent URL blank by default")
    require("INKDROP_SLSKD_API_BASE_URL: ${INKDROP_SLSKD_API_BASE_URL:-}" in arr_stack_doc, "Arr stack plan should leave SLSKD URL blank by default")
    require("INKDROP_KAVITA_URL: ${INKDROP_KAVITA_URL:-}" in arr_stack_doc, "Arr stack plan should leave Kavita URL blank by default")
    require("INKDROP_KOMGA_URL: ${INKDROP_KOMGA_URL:-}" in arr_stack_doc, "Arr stack plan should leave Komga URL blank by default")
    require("docker compose run --rm inkdrop python -B inkdrop_preflight.py --create --quiet --strict-dependencies --strict-runtime-tools" in arr_stack_doc, "Arr stack plan should require strict preflight")
    require("Rollback" in arr_stack_doc and "compose.yaml.bak" in arr_stack_doc, "Arr stack plan should include rollback guidance")
    upgrade_needles = (
        "## Upgrade And Image-Version Rollback",
        "image: ${INKDROP_IMAGE:?Set INKDROP_IMAGE to a pinned tag or digest}",
        "INKDROP_VERSION: ${INKDROP_EXPECTED_VERSION:?Set the version from the image label}",
        'INKDROP_CONTAINER_SCHEDULER_ENABLED: "0"',
        'INKDROP_CONTAINER_SCHEDULER_ENABLED: "1"',
        "reads the immutable OCI labels directly",
        "export INKDROP_PREVIOUS_IMAGE=\"$(docker inspect",
        "printf '%s\\n' \"$INKDROP_PREVIOUS_IMAGE\" > .inkdrop-previous-image",
        'export INKDROP_IMAGE="$INKDROP_PREVIOUS_IMAGE"',
        "export COMPOSE_FILE='docker-compose.yml:compose.image.yml'",
        "org.opencontainers.image.revision",
        "inkdrop_backup_restore.py backup --label pre-upgrade",
        "export INKDROP_IMAGE='ghcr.io/jaredbahr/inkdrop@sha256:<new-image-digest>'",
        "docker compose pull inkdrop inkdrop-worker",
        "docker compose stop inkdrop-worker",
        "docker compose up -d --no-deps --force-recreate inkdrop",
        "docker compose port inkdrop",
        "for attempt in $(seq 1 60)",
        "/api/system/version",
        "inkdrop_container_healthcheck.py --json --wait-seconds 60",
        "select value from schema_meta",
        "pragma quick_check",
        "pragma foreign_key_check",
        "docker compose up -d --no-deps --force-recreate inkdrop-worker",
        "inkdrop_container_healthcheck.py --worker --json --wait-seconds 90",
        'image_digest=""',
        'candidate_manifest_status="missing"',
        "export INKDROP_IMAGE=\"$(cat .inkdrop-previous-image)\"",
        "Restore the pre-upgrade state only when",
        "--target-config-dir /config --target-state-dir /state --apply",
    )
    upgrade_start = install_doc.index("## Upgrade And Image-Version Rollback")
    upgrade_end = install_doc.index("\n## Authentication", upgrade_start)
    upgrade_doc = install_doc[upgrade_start:upgrade_end]

    def require_in_order(text, needles, label):
        cursor = 0
        for needle in needles:
            position = text.find(needle, cursor)
            require(position >= 0, f"{label} should preserve required sequence item: {needle}")
            cursor = position + len(needle)

    require_in_order(upgrade_doc, upgrade_needles, "Docker install upgrade contract")
    rollback_doc = upgrade_doc[upgrade_doc.index('export INKDROP_IMAGE="$(cat .inkdrop-previous-image)"'):]
    require_in_order(
        rollback_doc,
        (
            'export INKDROP_IMAGE="$(cat .inkdrop-previous-image)"',
            "docker compose pull inkdrop inkdrop-worker",
            "docker compose stop inkdrop-worker",
            "docker compose up -d --no-deps --force-recreate inkdrop",
            "docker compose port inkdrop",
            "for attempt in $(seq 1 60)",
            "inkdrop_container_healthcheck.py --json --wait-seconds 60",
            "select value from schema_meta",
            "pragma quick_check",
            "pragma foreign_key_check",
            "docker compose up -d --no-deps --force-recreate inkdrop-worker",
            "inkdrop_container_healthcheck.py --worker --json --wait-seconds 90",
            "docker compose ps",
            "Restore the pre-upgrade state only when",
            "docker compose stop inkdrop-worker inkdrop",
            "cp -a ./state",
            "--target-config-dir /state/restore-preview-config --target-state-dir /state/restore-preview-state",
            "--target-config-dir /config --target-state-dir /state --apply",
            "docker compose up -d --no-deps --force-recreate inkdrop",
            "docker compose port inkdrop",
            "for attempt in $(seq 1 60)",
            "inkdrop_container_healthcheck.py --json --wait-seconds 60",
            "select value from schema_meta",
            "pragma quick_check",
            "pragma foreign_key_check",
            "docker compose up -d --no-deps --force-recreate inkdrop-worker",
            "inkdrop_container_healthcheck.py --worker --json --wait-seconds 90",
        ),
        "Docker install rollback contract",
    )
    require("mode `0644`" in install_doc and "mode `0600`" in install_doc, "Docker install docs should distinguish restored-file and backup-archive modes")
    require("docker-first-install.md#upgrade-and-image-version-rollback" in arr_stack_doc, "Arr stack plan should link the pinned image rollback procedure")
    executable_install_blocks = "\n".join(re.findall(r"```bash\s+(.*?)```", install_doc, flags=re.DOTALL))
    executable_arr_blocks = "\n".join(re.findall(r"```bash\s+(.*?)```", arr_stack_doc, flags=re.DOTALL))
    for destructive in (
        "docker compose down -v",
        "docker compose rm -v",
        "docker volume rm",
        "rm -rf ./state",
        "rm -rf ./config",
    ):
        require(destructive not in executable_install_blocks, f"Docker install commands must not perform destructive mount removal: {destructive}")
        require(destructive not in executable_arr_blocks, f"Arr stack commands must not perform destructive mount removal: {destructive}")
    doc_pairs = (
        ("README", readme),
        ("Docker install docs", install_doc),
    )
    required_needles = (
        "python -B tools/inkdrop_public_release_check.py",
        "python -B tools/inkdrop_docker_context_manifest.py --summary",
        "python -B tools/inkdrop_docker_context_manifest.py --json",
        "python -B tools/inkdrop_install_support_summary.py --json",
        "python -B tools/inkdrop_release_evidence_bundle.py",
        "python -B tools/inkdrop_release_evidence_bundle.py --remote-host",
        "python -B tools/inkdrop_compose_deployment_plan.py",
        "--output inkdrop.override.yaml",
        "docker compose -f /path/to/compose.yaml -f inkdrop.override.yaml config",
        "docker compose -f docker-compose.yml -f compose.network.example.yml up -d --build",
        "python -B tools/inkdrop_public_release_check.py --docker --require-docker",
        "python -B tools/inkdrop_public_release_check.py --docker-only --require-docker",
        "python -B tools/inkdrop_public_release_check.py --docker --require-docker --skip-docker-build",
        "python -B tools/inkdrop_public_release_check.py --docker-only --skip-docker-build",
        "python -B tools/inkdrop_public_release_check.py --strict-host-dependencies",
        "docker compose build inkdrop",
        "docker compose run --rm inkdrop python -B inkdrop_preflight.py --create --quiet --strict-dependencies --strict-runtime-tools",
        "Docker-capable host",
    )
    for label, text in doc_pairs:
        for needle in required_needles:
            require(needle in text, f"{label} should document release gate: {needle}")
        require("release_ready" in text, f"{label} should explain the release_ready runner field")
        require("release_check_schema_version" in text, f"{label} should explain the release_check_schema_version runner field")
        require("release_blockers" in text, f"{label} should explain the release_blockers runner field")
        require("release_blocker_schema_version" in text, f"{label} should explain the release_blocker_schema_version runner field")
        require("docker_gate_status" in text, f"{label} should explain the docker_gate_status runner field")
        require("docker_build_skipped" in text, f"{label} should explain the docker_build_skipped runner field")
        require("split_host_release_ready" in text, f"{label} should document split-host release evidence readiness")
        require("install_support_schema_version" in text, f"{label} should explain the install support summary field")
        require("docker compose ps" in text, f"{label} should document Docker health inspection")
        require('docker inspect "$(docker compose ps -q inkdrop)" --format' in text, f"{label} should document Compose-scoped Docker health details")
        require("docker compose logs inkdrop" in text, f"{label} should document Docker log inspection")
        require("docker compose exec inkdrop python -B inkdrop_container_healthcheck.py --json" in text, f"{label} should document manual healthcheck execution")
        require("phase=preflight" in text and "phase=http" in text, f"{label} should explain healthcheck phases")
        require("First-Run Checklist" in text, f"{label} should include a first-run checklist")
        require("Required for a clean Docker start" in text, f"{label} should separate required first-run steps")
        require("Optional before enabling automation" in text, f"{label} should separate optional automation setup")
        require("Expected clean-install warnings" in text, f"{label} should document expected clean-install warnings")
        require("optional_adapters_unconfigured" in text, f"{label} should explain clean-install adapter warnings")
        require("python_dependencies_missing" in text and "runtime_tools_missing" in text, f"{label} should explain direct-host clean-install warnings")
        require("install_defaults" in text, f"{label} should document install defaults for first-run packaging")
        require("enabled defaults" in text or "active config" in text, f"{label} should warn that service-name suggestions are not active adapter config")
        require("Existing Arr Stack Install" in text, f"{label} should document installing beside an existing Arr stack")
        require("separate Compose project" in text, f"{label} should recommend a separate Compose project for first Arr-stack installs")
        require("service DNS names" in text, f"{label} should explain same-network service DNS adapter URLs")
        require("Broader Release Blockers" in text, f"{label} should document broader-release blockers")
        require("Do not publish beyond the closed-alpha channel" in text, f"{label} should frame broader-release blockers as mandatory")
        require("release_ready=true" in text, f"{label} should require release_ready=true before a broader release")
        require("inkdrop-public-release-evidence" in text, f"{label} should require release evidence artifact")
        require("finding_count=0" in text, f"{label} should require clean release safety audit")
        require("only accepted large-context" in text, f"{label} should identify accepted Docker context warnings")
        require("large modules `inkdrop_state.py` and" in text and "`inkdrop_web.py`" in text, f"{label} should identify known Docker context warning files")
        require("any new unaccepted large context file fails" in text, f"{label} should explain that unexpected context bloat fails checks")
    for key in (
        "INKDROP_WEB_BASE_URL",
        "INKDROP_PORT",
        "INKDROP_SAB_PATH_MAPPINGS",
        "INKDROP_UNC_PATH_MAPPINGS",
    ):
        require(key in install_doc, f"Docker install docs should explain {key}")


def assert_web_surface_audit_contract():
    root = Path(__file__).resolve().parent
    audit_path = root / "tools" / "inkdrop_web_surface_audit.py"
    require(audit_path.exists(), "web surface audit tool should exist")
    text = audit_path.read_text(encoding="utf-8")
    for needle in (
        "REQUIRED_GET_PATHS",
        "REQUIRED_POST_PATHS",
        "REQUIRED_WORKER_REFERENCES",
        "REQUIRED_STATUS_FIELDS",
        "REQUIRED_STATUS_FUNCTIONS",
        "/api/inkdrop-state",
        "/api/inkdrop-settings/provider/update",
        "/api/inkdrop-settings/provider/test",
        "/api/inkdrop-settings/app/update",
        "/api/manual-review/approve",
        "source_health",
        "system_health",
        "status_cache",
        "status_partial",
        "ThreadingHTTPServer((HOST, PORT), Handler)",
    ):
        require(needle in text, f"web surface audit should guard {needle}")


def assert_install_support_summary_contract():
    root = Path(__file__).resolve().parent
    tool_path = root / "tools" / "inkdrop_install_support_summary.py"
    require(tool_path.exists(), "install support summary tool should exist")
    text = tool_path.read_text(encoding="utf-8")
    for needle in (
        "inkdrop_public_contracts.INSTALL_SUPPORT_SCHEMA_VERSION",
        "inkdrop_preflight.run_preflight",
        "inkdrop_public_contracts.RELEASE_CHECK_SCHEMA_VERSION",
        "_root_summary",
        "_adapter_summary",
        "_path_mapping_summary",
        "ADAPTER_GUIDANCE",
        "_setup_guidance",
        "_release_gate_summary",
        "_install_defaults",
        "install_defaults",
        "compose_service_suggestions",
        "secret_policy",
        "setup_guidance",
        "release_gate",
        "RELEASE_GATE_COMMAND",
        "docker_compose_build",
        "Docker CLI is not available",
        "next_step",
    ):
        require(needle in text, f"install support summary should guard {needle}")

    with tempfile.TemporaryDirectory(prefix="inkdrop-support-summary-smoke-") as tmp:
        tmp_root = Path(tmp)
        env = dict(os.environ)
        env.update(
            {
                inkdrop_runtime_config.ENV_CONFIG_DIR: str(tmp_root / "config"),
                inkdrop_runtime_config.ENV_STATE_DIR: str(tmp_root / "state"),
                inkdrop_runtime_config.ENV_LOG_DIR: str(tmp_root / "logs"),
                inkdrop_runtime_config.ENV_CACHE_DIR: str(tmp_root / "cache"),
                inkdrop_runtime_config.ENV_BACKUP_DIR: str(tmp_root / "backups"),
                inkdrop_runtime_config.ENV_STAGING_DIR: str(tmp_root / "staging"),
                inkdrop_runtime_config.ENV_MANUAL_INBOX_DIR: str(tmp_root / "manual-inbox"),
                inkdrop_runtime_config.ENV_QUARANTINE_DIR: str(tmp_root / "quarantine"),
                "INKDROP_COMICVINE_API_KEY": "super-secret-comicvine",
                "INKDROP_QBITTORRENT_URL": "http://user:super-secret-qbit@qbittorrent:8080",
                "INKDROP_SAB_PATH_MAPPINGS": r"/host/downloads=C:\secret-downloads",
            }
        )
        result = subprocess.run(
            [sys.executable, "-B", "tools/inkdrop_install_support_summary.py", "--create", "--json"],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
            timeout=60,
        )
        require(result.returncode == 1, f"support summary should exit 1 when preflight detects malformed config, got {result.returncode}")
        require(not result.stderr.strip(), "support summary --json should keep diagnostics in stdout")
        payload = json.loads(result.stdout)
        require(payload.get("install_support_schema_version") == 1, "support summary reports schema version 1")
        require(payload.get("preflight_schema_version") == 1, "support summary reports preflight schema version")
        require(payload.get("release_check_schema_version") == 1, "support summary reports release-check schema version")
        require(payload.get("preflight", {}).get("ok") is False, "support summary reports failing preflight state")
        release_gate = payload.get("release_gate") or {}
        require(release_gate.get("ready") is False, "support summary should not mark release gate ready")
        require(
            release_gate.get("required_command") == "python -B tools/inkdrop_public_release_check.py --docker --require-docker",
            "support summary should name the required Docker release command",
        )
        require("docker_container_strict_preflight" in (release_gate.get("required_checks") or []), "support summary should list strict container preflight as a required release gate")
        install_defaults = payload.get("install_defaults") or {}
        default_paths = install_defaults.get("paths") or {}
        require(default_paths.get("config_dir") == "/config", "support summary should expose public default config dir")
        require(default_paths.get("comic_root") == "/library/comics", "support summary should expose public default comic root")
        require(default_paths.get("slskd_download_root") == "/staging/slskd", "support summary should expose public default SLSKD staging root")
        for key, expected in {
            "pack_temp_download_root": "/staging/temp/downloads/comics",
            "source_worker_staging_root": "/staging/source-worker",
            "unmatched_quarantine_root": "/state/quarantine/unmatched",
            "managed_duplicate_quarantine_root": "/state/quarantine/managed-duplicate-files",
            "pack_duplicate_quarantine_root": "/state/quarantine/pack-duplicates",
            "pack_review_quarantine_root": "/state/quarantine/pack-review",
            "comic_incoming_root": "/library/comics/_Incoming",
            "ebook_incoming_root": "/library/ebooks/_Incoming",
            "qbittorrent_download_root": "/staging/downloads",
            "download_staging_root": "/staging",
        }.items():
            require(default_paths.get(key) == expected, f"support summary should expose public default {key}")
        automation_defaults = install_defaults.get("automation") or {}
        require(automation_defaults.get("debug_active_requests") == "0", "support summary should keep active request diagnostics disabled by default")
        for key, expected in {
            "queue_runner_import_priority_ready_imports": "1",
            "autopilot_runtime_hard_grace_seconds": "90",
            "import_ready_batch_timeout_seconds": "600",
            "reconciled_import_sync_budget_seconds": "20",
            "manga_completion_backfill_limit": "50",
        }.items():
            require(automation_defaults.get(key) == expected, f"support summary should expose automation default {key}")
        optional_defaults = install_defaults.get("optional_adapter_defaults") or {}
        for key in ("comicvine_api_key", "prowlarr_url", "sabnzbd_url", "qbittorrent_url", "slskd_api_base_url"):
            require(optional_defaults.get(key) == "", f"public optional adapter default must stay blank: {key}")
        suggestions = install_defaults.get("compose_service_suggestions") or {}
        require(suggestions.get("slskd_api_base_url") == "http://slskd:5030/api/v0", "support summary should provide a non-active SLSKD service-name suggestion")
        require("must not enable adapters" in (install_defaults.get("secret_policy") or ""), "support summary should state adapter defaults are suggestions, not active config")
        if payload.get("docker_available") is False:
            require(release_gate.get("status") == "docker_unavailable", "support summary should classify missing Docker as docker_unavailable")
        require("effective_config" not in payload.get("preflight", {}), "support summary should not include full effective config")
        require("path" not in str(payload.get("preflight", {}).get("roots", {})).lower(), "support summary root block should not echo configured paths")
        require(payload["preflight"]["path_mappings"]["INKDROP_SAB_PATH_MAPPINGS"]["entry_count"] == 1, "support summary reports path mapping counts")
        adapters = payload["preflight"]["configured_adapters"]
        require(adapters["prowlarr"]["next_step"].startswith("Set INKDROP_PROWLARR_URL"), "support summary should give adapter next steps")
        require(adapters["prowlarr"]["impact"], "support summary should explain unconfigured adapter impact")
        require(adapters["qbittorrent"]["configured"] is True, "configured qBittorrent URL should still report configured")
        require(adapters["qbittorrent"]["next_step"] == "", "configured adapters should not ask for setup next steps")
        guidance = payload.get("setup_guidance") or []
        if payload.get("docker_available") is False:
            require(any(item.get("area") == "docker" and item.get("severity") == "warning" for item in guidance), "support summary should explain missing Docker CLI")
        require(any(item.get("area") == "preflight_errors" and item.get("severity") == "error" for item in guidance), "support summary should include preflight error guidance")
        require(any(item.get("area") == "download_sources" for item in guidance) is False, "configured qBittorrent should count as a download adapter")
        for secret in ("super-secret", r"C:\secret-downloads", "/host/downloads"):
            require(secret not in result.stdout, "support summary should not leak secrets or host paths")


def assert_settings_api_surface_audit_contract():
    root = Path(__file__).resolve().parent
    audit_path = root / "tools" / "inkdrop_settings_api_surface_audit.py"
    require(audit_path.exists(), "settings API surface audit tool should exist")
    text = audit_path.read_text(encoding="utf-8")
    for needle in (
        "REQUIRED_GET_ROUTES",
        "REQUIRED_POST_ROUTES",
        "REQUIRED_WEB_HELPER_SNIPPETS",
        "REQUIRED_STATE_SNIPPETS",
        "/api/inkdrop-settings",
        "/api/inkdrop-settings/sync",
        "/api/inkdrop-settings/provider/add",
        "/api/inkdrop-settings/provider/claim",
        "/api/inkdrop-settings/provider/update",
        "/api/inkdrop-settings/provider/recommendation/apply",
        "/api/inkdrop-settings/provider/test",
        "/api/inkdrop-settings/app/update",
        "inkdrop_settings_public(sync=False",
        "inkdrop_settings_public(sync=True",
        "area=area",
        "inkdrop_state.record_provider_test(INKDROP_STATE_DB, result)",
        'allowed = {"enabled", "base_url", "secret_ref", "settings"}',
    ):
        require(needle in text, f"settings API surface audit should guard {needle}")


def assert_state_schema_audit_contract():
    root = Path(__file__).resolve().parent
    audit_path = root / "tools" / "inkdrop_state_schema_audit.py"
    require(audit_path.exists(), "state schema audit tool should exist")
    text = audit_path.read_text(encoding="utf-8")
    for needle in (
        "REQUIRED_TABLE_COLUMNS",
        "REQUIRED_INDEX_NAMES",
        "REQUIRED_PUBLIC_STATE_FUNCTIONS",
        "REQUIRED_STATE_SUMMARY_FIELDS",
        "REQUIRED_STATE_SECTIONS_FIELDS",
        "STATE_DB_NAME = \"inkdrop-state.sqlite3\"",
        "SCHEMA_VERSION = 18",
        "runtime_schema_version",
        "runtime_integrity",
        "runtime_foreign_key_violations",
        "schema_17_upgrade",
        "runtime_table_count",
        "runtime_index_count",
        "migration_fixture_preserved_keys",
        "migration_public_provider_contract",
        "migration_query_plan_checks",
        "required_public_state_function_count",
        "required_state_summary_field_count",
        "required_state_sections_field_count",
        "legacy_app_setting",
        "public_provider",
        "public_app_setting",
        "secret_storage",
        "candidate_attempt",
        "candidate_task",
        "idx_source_attempts_candidate_identity",
        "idx_source_attempts_provider_id_recent",
        "idx_source_attempts_outcome_recent",
        "idx_source_attempts_display_phase_recent",
        "idx_download_tasks_candidate_identity",
        "idx_download_tasks_provider_id",
        "manga_companion_links",
        "manga_companion_reconcile_attempts",
        "idx_manga_companion_active_comicvine",
        "idx_manga_companion_active_mangadex",
        "migration_table_count",
        "migration_index_count",
        "inkdrop_state.ensure_schema",
        "queue_items",
        "queue_claims",
        "idx_queue_claims_expires",
        "source_attempts",
        "download_tasks",
        "import_results",
        "media_files",
        "provider_configs",
        "state_summary",
        "state_view",
        "state_sections_shell",
        "queue_by_display_active_state",
        "standalone_foundation",
        "section_links",
    ):
        require(needle in text, f"state schema audit should guard {needle}")


def assert_preflight_config_key_contract():
    root = Path(__file__).resolve().parent
    compose = yaml.safe_load((root / "docker-compose.yml").read_text(encoding="utf-8"))
    compose_env = set((compose.get("services") or {}).get("inkdrop", {}).get("environment") or {})
    env_example = {
        match.group(1)
        for match in re.finditer(r"^(INKDROP_[A-Z0-9_]+)=", (root / ".env.example").read_text(encoding="utf-8"), flags=re.MULTILINE)
    }
    public_effective_config_keys = set(inkdrop_preflight.CONFIG_ENV_KEYS)
    public_effective_config_keys.update({key for keys in inkdrop_preflight.OPTIONAL_ADAPTER_ENV.values() for key in keys})
    compatibility_config_keys = set(inkdrop_preflight.COMPATIBILITY_ENV_KEYS)
    effective_config_keys = public_effective_config_keys | compatibility_config_keys
    missing_from_compose = sorted(effective_config_keys - compose_env)
    missing_from_env_example = sorted(public_effective_config_keys - env_example)
    unreported_compose_keys = sorted(compose_env - effective_config_keys)
    unreported_env_example_keys = sorted(env_example - public_effective_config_keys)
    env_example_only_keys = sorted(env_example - compose_env)
    require(not missing_from_compose, f"preflight effective-config keys missing from compose: {missing_from_compose}")
    require(not missing_from_env_example, f"preflight effective-config keys missing from .env.example: {missing_from_env_example}")
    require(not unreported_compose_keys, f"compose INKDROP keys missing from preflight effective-config contract: {unreported_compose_keys}")
    require(not unreported_env_example_keys, f".env.example INKDROP keys missing from preflight effective-config contract: {unreported_env_example_keys}")
    require(not env_example_only_keys, f".env.example INKDROP keys missing from compose environment: {env_example_only_keys}")
    require(
        compatibility_config_keys == {"KAVITA_ACQUIRE_STATE_DIR"},
        f"unexpected preflight compatibility aliases: {sorted(compatibility_config_keys)}",
    )


def assert_public_operator_knobs_are_visible():
    root = Path(__file__).resolve().parent
    compose = yaml.safe_load((root / "docker-compose.yml").read_text(encoding="utf-8"))
    compose_env = set((compose.get("services") or {}).get("inkdrop", {}).get("environment") or {})
    env_example = {
        match.group(1)
        for match in re.finditer(r"^(INKDROP_[A-Z0-9_]+)=", (root / ".env.example").read_text(encoding="utf-8"), flags=re.MULTILINE)
    }
    public_knobs = {
        "INKDROP_MISSING_RECOVERY_ENABLED",
        "INKDROP_MISSING_RECOVERY_MAX_PER_COHORT",
        "INKDROP_MISSING_RECOVERY_MAX_HANDOFFS_PER_HOUR",
        "INKDROP_MISSING_RECOVERY_MAX_BYTES_PER_HOUR",
        "INKDROP_MISSING_RECOVERY_MAX_BYTES_PER_DAY",
        "INKDROP_MISSING_RECOVERY_MIN_STAGING_FREE_BYTES",
        "INKDROP_MISSING_RECOVERY_PAUSE_AFTER_FAILURES",
        "INKDROP_MISSING_RECOVERY_QUIET_HOURS",
        "INKDROP_MANAGED_DUPLICATE_QUARANTINE_ROOT",
        "INKDROP_PROTOCOL_ORDER",
        "INKDROP_QUEUE_RUNNER_IMPORT_PRIORITY_READY_IMPORTS",
        "INKDROP_AUTOPILOT_RUNTIME_HARD_GRACE_SECONDS",
        "INKDROP_IMPORT_READY_QUEUE_ONLY",
        "INKDROP_IMPORT_READY_IMPORT_TIMEOUT_SECONDS",
        "INKDROP_IMPORT_READY_BATCH_TIMEOUT_SECONDS",
        "INKDROP_PACK_MANIFEST_CACHE_SECONDS",
        "INKDROP_RECONCILED_IMPORT_SYNC_BUDGET_SECONDS",
        "INKDROP_MANGA_COMPLETION_BACKFILL_LIMIT",
        "INKDROP_PACK_PROBE_SCAN_SECONDS",
        "INKDROP_PACK_PROBE_SCAN_ENTRIES",
        "INKDROP_DEBUG_ACTIVE_REQUESTS",
    }
    missing = sorted(
        key
        for key in public_knobs
        if key not in compose_env or key not in env_example or key not in inkdrop_preflight.CONFIG_ENV_KEYS
    )
    require(not missing, f"public operator knobs should be visible in compose, .env.example, and preflight: {missing}")
    docs = (root / "docs" / "inkdrop" / "docker-first-install.md").read_text(encoding="utf-8")
    for key in (
        "INKDROP_PROTOCOL_ORDER",
        "INKDROP_QUEUE_RUNNER_IMPORT_PRIORITY_READY_IMPORTS",
        "INKDROP_IMPORT_READY_QUEUE_ONLY",
        "INKDROP_PACK_PROBE_SCAN_SECONDS",
        "INKDROP_PACK_PROBE_SCAN_ENTRIES",
        "INKDROP_DEBUG_ACTIVE_REQUESTS",
    ):
        require(key in docs, f"Docker install docs should explain {key}")


def assert_manual_source_callback_derivation_contract():
    root = Path(__file__).resolve().parent
    text = (root / "inkdrop_manual_source_autoresolve.py").read_text(encoding="utf-8")
    require("def _inkdrop_web_endpoint" in text, "manual-source worker should derive callback endpoints from a shared helper")
    require("inkdrop_runtime_config.worker_web_base_url()" in text, "manual-source callback fallback should use the shared container-aware resolver")
    require('API_URL = _inkdrop_web_endpoint("INKDROP_MANUAL_SOURCE_IMPORT_API_URL", "/api/manual-source/import-detected")' in text, "manual-source import callback should derive from the worker callback base when unset")
    require('MARK_WAITING_API_URL = _inkdrop_web_endpoint("INKDROP_MARK_WAITING_API_URL", "/api/manual-source/mark-waiting")' in text, "manual-source mark-waiting callback should derive from the worker callback base when unset")
    require("inkdrop_internal_jobs.run_manual_source_import" in text, "default container callback should stay on the trusted in-process boundary")
    require('"http://127.0.0.1:8796"' not in text, "manual-source callback fallback should not hardcode the default InkDrop port")


def assert_worker_callback_runtime_contract():
    import importlib
    import inkdrop_manual_source_autoresolve
    import inkdrop_missing_acquire
    import inkdrop_series_autopilot
    import inkdrop_slskd_source_probe

    env_keys = (
        "INKDROP_PORT",
        "INKDROP_WEB_BASE_URL",
        "INKDROP_CONTAINER_WEB_BASE_URL",
        "INKDROP_WORKER_API_KEY",
        "INKDROP_MANUAL_SOURCE_IMPORT_API_URL",
        "INKDROP_MARK_WAITING_API_URL",
    )
    saved = {key: os.environ.get(key) for key in env_keys}
    cases = (
        ({"INKDROP_PORT": "8796"}, "http://127.0.0.1:8796", "local_port"),
        (
            {"INKDROP_PORT": "8899", "INKDROP_CONTAINER_WEB_BASE_URL": "http://inkdrop:8899"},
            "http://inkdrop:8899",
            "INKDROP_CONTAINER_WEB_BASE_URL",
        ),
        (
            {
                "INKDROP_PORT": "8899",
                "INKDROP_WEB_BASE_URL": "https://operator.example/inkdrop",
                "INKDROP_CONTAINER_WEB_BASE_URL": "http://inkdrop-internal:8899/",
            },
            "http://inkdrop-internal:8899",
            "INKDROP_CONTAINER_WEB_BASE_URL",
        ),
        (
            {"INKDROP_PORT": "8899", "INKDROP_WEB_BASE_URL": "https://operator.example/inkdrop/"},
            "https://operator.example/inkdrop",
            "INKDROP_WEB_BASE_URL",
        ),
    )
    try:
        for configured, expected_base, expected_source in cases:
            for key in env_keys:
                os.environ.pop(key, None)
            os.environ.update(configured)
            autopilot = importlib.reload(inkdrop_series_autopilot)
            slskd = importlib.reload(inkdrop_slskd_source_probe)
            autoresolve = importlib.reload(inkdrop_manual_source_autoresolve)
            missing = importlib.reload(inkdrop_missing_acquire)
            require(inkdrop_runtime_config.worker_web_base_url() == expected_base, f"worker callback base should resolve {expected_base}")
            require(inkdrop_runtime_config.worker_web_base_url_source() == expected_source, f"worker callback source should resolve {expected_source}")
            require(autopilot.WEB_BASE_URL == expected_base, "autopilot should use the effective worker callback base")
            require(slskd.MARK_WAITING_API_URL == expected_base + "/api/manual-source/mark-waiting", "SLSKD probe should use the effective worker callback base")
            require(autoresolve.API_URL == expected_base + "/api/manual-source/import-detected", "manual autoresolve import should use the effective worker callback base")
            require(autoresolve.MARK_WAITING_API_URL == expected_base + "/api/manual-source/mark-waiting", "manual autoresolve mark-waiting should use the effective worker callback base")
            require(missing.inkdrop_web_api_url("/api/example") == expected_base + "/api/example", "missing acquire should use the effective worker callback base")
            if expected_source == "INKDROP_CONTAINER_WEB_BASE_URL":
                require("127.0.0.1" not in autopilot.WEB_BASE_URL, "container worker callbacks must not fall back to loopback")
    finally:
        for key in env_keys:
            if saved[key] is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = saved[key]
        importlib.reload(inkdrop_series_autopilot)
        importlib.reload(inkdrop_slskd_source_probe)
        importlib.reload(inkdrop_manual_source_autoresolve)
        importlib.reload(inkdrop_missing_acquire)

    source_contracts = {
        "autopilot": Path("inkdrop_series_autopilot.py").read_text(encoding="utf-8"),
        "slskd": Path("inkdrop_slskd_source_probe.py").read_text(encoding="utf-8"),
        "manual": Path("inkdrop_manual_source_autoresolve.py").read_text(encoding="utf-8"),
        "missing": Path("inkdrop_missing_acquire.py").read_text(encoding="utf-8"),
    }
    for label, source in source_contracts.items():
        require("worker_auth_headers(required=True)" in source, f"{label} HTTP callback must attach the supported worker API-key header")
    require("run_manual_source_import" in source_contracts["manual"], "default manual-source callback should retain trusted in-process dispatch")
    require("run_manual_source_mark_waiting" in source_contracts["slskd"], "default SLSKD callback should retain trusted in-process dispatch")
    require("run_autopilot_web_job" in source_contracts["autopilot"], "default autopilot callback should retain trusted in-process dispatch")
    require("run_pack_review_state" in source_contracts["missing"], "default pack-state callback should retain trusted in-process dispatch")


def _dockerignore_patterns(root):
    patterns = []
    for raw in (root / ".dockerignore").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(line)
    return patterns


def _dockerignore_pattern_matches(relative_path, pattern):
    import fnmatch

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


def _dockerignore_matches(relative_path, patterns):
    ignored = False
    for pattern in patterns:
        negate = pattern.startswith("!")
        raw_pattern = pattern[1:] if negate else pattern
        if _dockerignore_pattern_matches(relative_path, raw_pattern):
            ignored = not negate
    return ignored


def assert_image_defaults_live_under_documented_mounts():
    """Every default path the image bakes in must sit under a mount the
    install documentation tells people to create. The database, accounts,
    backups, and quarantine once defaulted to an unmounted /state, so
    recreating the documented container destroyed a tester's everything."""
    documented_mounts = ("/config", "/data/comics", "/data/manga", "/downloads")
    dockerfile = (Path(__file__).parent / "Dockerfile").read_text(encoding="utf-8")
    import re as _re

    for name, value in _re.findall(r"(INKDROP_[A-Z_]+)=(/[^\s\\]+)", dockerfile):
        if name in ("INKDROP_HOST",):
            continue
        require(
            any(value == mount or value.startswith(mount + "/") for mount in documented_mounts),
            f"image default {name}={value} lives outside the documented mounts {documented_mounts} -- "
            "recreating the documented container would lose it",
        )


def assert_docker_python_allowlist_import_closure():
    root = Path(__file__).resolve().parent
    patterns = _dockerignore_patterns(root)
    included_paths = [
        path
        for path in sorted(list(root.glob("inkdrop*.py")) + list(root.glob("kavita*.py")))
        if not _dockerignore_matches(path.relative_to(root), patterns)
    ]
    included_modules = {path.stem for path in included_paths}
    non_importable_names = sorted(path.name for path in included_paths if "-" in path.stem)
    require(not non_importable_names, "Docker runtime Python allowlist should not include non-importable hyphenated scripts: " + ", ".join(non_importable_names))
    local_modules = {
        path.stem: path
        for path in root.glob("*.py")
        if path.stem.startswith(("inkdrop", "kavita"))
    }
    missing = []
    for path in included_paths:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module.split(".")[0]]
            for module in modules:
                if module.startswith(("inkdrop", "kavita")) and module in local_modules and module not in included_modules:
                    missing.append(f"{path.name} imports excluded local module {module} ({local_modules[module].name})")
    require(not missing, "Docker runtime Python allowlist has missing local imports: " + "; ".join(missing[:12]))
    require("inkdrop_web" in included_modules, "Docker runtime allowlist includes inkdrop_web entrypoint")
    require((root / "docs" / "inkdrop-source-candidate-catalog-20260702.json").exists(), "runtime source catalog exists for Docker copy")


def assert_docker_context_manifest_contract():
    root = Path(__file__).resolve().parent
    attributes = (root / ".gitattributes").read_text(encoding="utf-8")
    require(
        "web/static/css/inkdrop.css text eol=lf" in attributes,
        "production stylesheet should remain LF-stable so Windows Docker contexts match Git/Linux byte size",
    )
    manifest_path = root / "tools" / "inkdrop_docker_context_manifest.py"
    require(manifest_path.exists(), "Docker context manifest tool should exist")
    spec = importlib.util.spec_from_file_location("inkdrop_docker_context_manifest", manifest_path)
    manifest_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(manifest_module)
    manifest = manifest_module.build_manifest(root)
    warnings = manifest_module.size_warnings(manifest)
    paths = {item["path"] for item in manifest.get("files") or []}
    require(manifest.get("schema") == "inkdrop.docker_context_manifest.v1", "Docker context manifest should expose a stable schema")
    require(manifest.get("file_count") == len(paths), "Docker context manifest file_count should match files")
    require(isinstance(warnings, list), "Docker context manifest should expose non-blocking size warnings")
    require(any(item.get("kind") == "large_context_file" and item.get("path") == "inkdrop_web.py" for item in warnings), "Docker context size warnings should identify the oversized web implementation")
    accepted_large = {
        item.get("path")
        for item in warnings
        if item.get("kind") == "large_context_file" and item.get("accepted") is True
    }
    require({"inkdrop_state.py", "inkdrop_web.py"}.issubset(accepted_large), "known oversized implementation modules should be explicitly accepted packaging debt")
    total_context = next((item for item in warnings if item.get("kind") == "total_context_size"), None)
    if total_context is not None:
        require(total_context.get("accepted") is True, "bounded closed-alpha runtime context growth should be explicitly accepted")
    bounded_total_warnings = manifest_module.size_warnings(
        {"total_size_bytes": manifest_module.DEFAULT_TOTAL_SIZE_WARN_BYTES + 1, "files": []}
    )
    require(
        len(bounded_total_warnings) == 1 and bounded_total_warnings[0].get("accepted") is True,
        "bounded context growth should remain explicitly accepted even when the current platform stays below 10 MiB",
    )
    require(
        not manifest_module.warnings_ok(
            manifest_module.size_warnings(
                {"total_size_bytes": manifest_module.ACCEPTED_TOTAL_SIZE_BYTES + 1, "files": []}
            )
        ),
        "Docker context growth beyond the accepted bounded ceiling should fail",
    )
    require(manifest_module.warnings_ok(warnings), "Docker context warnings should fail if new unaccepted bloat enters the context")
    for item in warnings:
        if item.get("accepted"):
            require(item.get("reason") and item.get("risk"), f"accepted warning for {item.get('path')} should include reason and risk")
            require(item.get("owner") == "InkDrop maintainers", f"accepted warning for {item.get('path')} should use neutral maintainer ownership")
            require(item.get("next_action"), f"accepted warning for {item.get('path')} should include a next action")
            require(item.get("exit_criteria"), f"accepted warning for {item.get('path')} should include exit criteria")
    for required in (
        "Dockerfile",
        "requirements.txt",
        "inkdrop_public_contracts.py",
        "inkdrop_acquire_adapter.py",
        "inkdrop_acquire.py",
        "inkdrop_container_start.py",
        "inkdrop_web.py",
        "inkdrop_acquire_adapter.py",
        "docs/inkdrop-source-candidate-catalog-20260702.json",
        "tools/inkdrop_install_support_summary.py",
    ):
        require(required in paths, f"Docker context manifest should include {required}")
    for forbidden in (
        ".env",
        "docs/inkdrop/AGENT_HANDOFF.md",
        "web/tests/about-release-notes-browser-smoke.js",
        "web/tests/fixtures/system-mobile.html",
        "tools/inkdrop_public_release_check.py",
        "tools/inkdrop_settings_api_surface_audit.py",
        "tools/inkdrop_settings_sync_smoke.py",
        "tools/inkdrop_state_schema_audit.py",
        "tools/inkdrop_web_surface_audit.py",
        "inkdrop-source-worker-cli.py",
        "inkdrop-source-worker-service.py",
    ):
        require(forbidden not in paths, f"Docker context manifest should exclude {forbidden}")
    for item in manifest.get("files") or []:
        require(len(item.get("sha256") or "") == 64, f"Docker context manifest should include sha256 for {item.get('path')}")


def assert_docker_core_worker_scripts_included():
    root = Path(__file__).resolve().parent
    patterns = _dockerignore_patterns(root)
    required_scripts = (
        "inkdrop_acquire.py",
        "inkdrop_web.py",
        "inkdrop_missing_acquire.py",
        "inkdrop_completed_import.py",
        "inkdrop_reconcile_imports.py",
        "inkdrop_pack_import.py",
        "inkdrop_series_autopilot.py",
        "inkdrop_slskd_source_probe.py",
        "inkdrop_manual_source_autoresolve.py",
        "inkdrop_source_worker_service.py",
        "inkdrop_source_worker_cli.py",
    )
    missing = []
    excluded = []
    for name in required_scripts:
        path = root / name
        if not path.exists():
            missing.append(name)
        elif _dockerignore_matches(path.relative_to(root), patterns):
            excluded.append(name)
    require(not missing, "Docker core worker script(s) missing from checkout: " + ", ".join(missing))
    require(not excluded, "Docker core worker script(s) excluded from build context: " + ", ".join(excluded))


def assert_runtime_source_catalog_contract():
    root = Path(__file__).resolve().parent
    catalog_path = root / "docs" / "inkdrop-source-candidate-catalog-20260702.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    require(isinstance(catalog.get("catalog_version"), int), "source catalog should have an integer catalog_version")
    providers = catalog.get("provider_candidates")
    require(isinstance(providers, list) and providers, "source catalog should contain provider candidates")
    implementation_order = catalog.get("implementation_order") or []
    mode_definitions = catalog.get("mode_definitions") or {}
    allowed_modes = set(mode_definitions) or {"auto", "assist", "manual_review", "metadata_only", "disabled"}
    provider_ids = []
    issues = []
    for index, provider in enumerate(providers):
        if not isinstance(provider, dict):
            issues.append(f"provider #{index} is not an object")
            continue
        provider_id = str(provider.get("id") or "").strip()
        provider_ids.append(provider_id)
        for key in ("id", "display_name", "source_kind", "default_mode", "policy"):
            if key not in provider:
                issues.append(f"{provider_id or index} missing {key}")
        if provider.get("default_mode") not in allowed_modes:
            issues.append(f"{provider_id} has unsupported default_mode {provider.get('default_mode')!r}")
        implementation_status = provider.get("implementation_status") or "planned"
        if implementation_status not in {"planned", "implemented"}:
            issues.append(f"{provider_id} has unsupported implementation_status {provider.get('implementation_status')!r}")
        policy = provider.get("policy") if isinstance(provider.get("policy"), dict) else {}
        policy_text = json.dumps(policy, sort_keys=True)
        for private_token in PRIVATE_TOKENS:
            if private_token in policy_text:
                issues.append(f"{provider_id} policy contains private token {private_token}")
        for key, value in policy.items():
            if key.endswith("_root") and isinstance(value, str) and value:
                normalized = value.replace("\\", "/")
                if not normalized.startswith(("/staging", "/state", "/manual-inbox", "/library", "/config")):
                    issues.append(f"{provider_id} {key} should use a neutral container path, got {value!r}")
    duplicates = sorted({item for item in provider_ids if provider_ids.count(item) > 1})
    missing_order = sorted(set(implementation_order) - set(provider_ids))
    require(not duplicates, "source catalog provider IDs should be unique: " + ", ".join(duplicates))
    require(not missing_order, "source catalog implementation_order references missing providers: " + ", ".join(missing_order[:12]))
    require(not issues, "source catalog contract failed: " + "; ".join(issues[:12]))


def assert_docker_runtime_python_files_compile():
    root = Path(__file__).resolve().parent
    patterns = _dockerignore_patterns(root)
    runtime_files = [
        path
        for path in sorted(list(root.glob("inkdrop*.py")) + list(root.glob("kavita*.py")))
        if not _dockerignore_matches(path.relative_to(root), patterns)
    ]
    require(runtime_files, "Docker runtime Python allowlist should include app modules")
    failures = []
    with tempfile.TemporaryDirectory(prefix="inkdrop-runtime-pycompile-") as tmp:
        pycache_dir = Path(tmp)
        for path in runtime_files:
            try:
                py_compile.compile(str(path), cfile=str(pycache_dir / f"{path.stem}.pyc"), doraise=True)
            except py_compile.PyCompileError as exc:
                failures.append(f"{path.name}: {exc.msg}")
    require(not failures, "Docker runtime Python files should compile: " + "; ".join(failures[:8]))


def assert_runtime_third_party_imports_are_declared():
    root = Path(__file__).resolve().parent
    patterns = _dockerignore_patterns(root)
    runtime_files = [
        path
        for path in sorted(list(root.glob("inkdrop*.py")) + list(root.glob("kavita*.py")))
        if not _dockerignore_matches(path.relative_to(root), patterns)
    ]
    local_modules = {
        path.stem
        for path in root.glob("*.py")
        if path.stem.startswith(("inkdrop", "kavita"))
    }
    try:
        stdlib_modules = set(__import__("sys").stdlib_module_names)
    except AttributeError:
        stdlib_modules = set()
    third_party = {}
    for path in runtime_files:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module.split(".")[0]]
            for module in modules:
                if (
                    not module
                    or module.startswith("_")
                    or module in local_modules
                    or module in stdlib_modules
                    or module in {"typing_extensions"}
                ):
                    continue
                third_party.setdefault(module, set()).add(path.name)

    requirements = (root / "requirements.txt").read_text(encoding="utf-8")
    missing = []
    for module, files in sorted(third_party.items()):
        package = IMPORT_PACKAGE_NAMES.get(module)
        if not package:
            missing.append(f"{module} imported by {', '.join(sorted(files)[:4])} has no package mapping")
            continue
        if not re.search(rf"^{re.escape(package)}(?:[<>=!~ ]|$)", requirements, flags=re.MULTILINE | re.IGNORECASE):
            missing.append(f"{module} imports require package {package} in requirements.txt")
        if module not in inkdrop_preflight.REQUIRED_PYTHON_MODULES:
            missing.append(f"{module} should be checked by inkdrop_preflight.REQUIRED_PYTHON_MODULES")
    require(not missing, "Runtime third-party import dependency contract failed: " + "; ".join(missing[:12]))


def assert_dockerignore_allowlist_shape():
    root = Path(__file__).resolve().parent
    patterns = _dockerignore_patterns(root)
    for relative in (
        Path("Dockerfile"),
        Path("requirements.txt"),
        Path("inkdrop-logo-mark.png"),
        Path("web/static/img/inkdrop-auth-backdrop.webp"),
        Path("inkdrop_public_contracts.py"),
        Path("inkdrop_sab_failed_cleanup.py"),
        Path("inkdrop_acquire.py"),
        Path("inkdrop_web.py"),
        Path("docs/inkdrop-source-candidate-catalog-20260702.json"),
        Path("tools/inkdrop_install_support_summary.py"),
    ):
        require(not _dockerignore_matches(relative, patterns), f"{relative.as_posix()} should be included in Docker build context")
    for relative in (
        Path("README.md"),
        Path("docs/inkdrop/docker-first-install.md"),
        Path("docs/inkdrop/arr-stack-deployment-plan.md"),
        Path("web/tests/about-release-notes-browser-smoke.js"),
        Path("web/tests/fixtures/system-mobile.html"),
        Path("inkdrop-public-docker-runtime-smoke.py"),
        Path("inkdrop_download_client_ownership_smoke.py"),
        Path("inkdrop-public-release-safety-audit.py"),
        Path("tools/inkdrop_public_release_check.py"),
        Path("tools/inkdrop_release_evidence_bundle.py"),
        Path("tools/inkdrop_compose_deployment_plan.py"),
        Path("tools/inkdrop_settings_api_surface_audit.py"),
        Path("tools/inkdrop_settings_sync_smoke.py"),
        Path("tools/inkdrop_state_schema_audit.py"),
        Path("tools/inkdrop_web_surface_audit.py"),
        Path("kavita-collection-work-20260614-210622/inkdrop_web.py"),
        Path("manga-completion-work/inkdrop_web.py"),
    ):
        require(_dockerignore_matches(relative, patterns), f"{relative.as_posix()} should be excluded from Docker build context")


def assert_preflight_contract():
    with tempfile.TemporaryDirectory(prefix="inkdrop-preflight-smoke-") as tmp:
        root = Path(tmp)
        env = {
            inkdrop_runtime_config.ENV_CONFIG_DIR: str(root / "config"),
            inkdrop_runtime_config.ENV_STATE_DIR: str(root / "state"),
            inkdrop_runtime_config.ENV_LOG_DIR: str(root / "logs"),
            inkdrop_runtime_config.ENV_CACHE_DIR: str(root / "cache"),
            inkdrop_runtime_config.ENV_BACKUP_DIR: str(root / "backups"),
            inkdrop_runtime_config.ENV_STAGING_DIR: str(root / "staging"),
            inkdrop_runtime_config.ENV_MANUAL_INBOX_DIR: str(root / "manual-inbox"),
            inkdrop_runtime_config.ENV_QUARANTINE_DIR: str(root / "quarantine"),
        }
        missing = inkdrop_preflight.run_preflight(env, create=False)
        require(not missing["ok"], "preflight should fail before missing dirs are created")
        require(missing["roots"]["config_dir"]["required"] is True, "preflight marks config_dir required")
        require(missing["roots"]["state_dir"]["required"] is True, "preflight marks state_dir required")
        require(missing["roots"]["staging_dir"]["required"] is False, "preflight marks staging_dir optional")
        require(missing["roots"]["manual_inbox_dir"]["required"] is False, "preflight marks manual_inbox_dir optional")
        require(missing["roots"]["quarantine_dir"]["required"] is False, "preflight marks quarantine_dir optional")
        require(any("staging_dir does not exist" in warning for warning in missing["warnings"]), "optional missing staging root should warn")
        created = inkdrop_preflight.run_preflight(env, create=True)
        require(created["ok"], "preflight should pass after creating runtime dirs")
        require(created["preflight_schema_version"] == 1, "preflight reports schema version 1")
        require(created["state_db_path"].endswith("/state/inkdrop-state.sqlite3"), "preflight reports state DB path")
        require(created["web"]["bind_port"] == 8796, "preflight reports default web bind port")
        require(created["web"]["host_port"] == 8796, "preflight reports the default published host port")
        require(created["web"]["host_port_source"] == "INKDROP_PORT", "preflight reports the backward-compatible host-port fallback")
        require(created["web"]["callback_base_source"] == "local_port", "preflight reports local-port callback base when web base URL is blank")
        require(created["web"]["callback_base_url"] == "http://127.0.0.1:8796", "preflight derives callback base from configured local port")
        require("path_mappings" in created, "preflight reports path mapping validation")
        require(created["path_mappings"]["INKDROP_SAB_PATH_MAPPINGS"]["configured"] is False, "blank SAB path mappings are accepted")
        require("warning_summary" in created, "preflight reports a structured warning summary")
        require(created["warning_summary"]["optional_adapters_unconfigured"] == ["comicvine", "prowlarr", "qbittorrent", "sabnzbd", "slskd", "suwayomi"], "clean preflight has stable optional-adapter warnings")
        require("kapowarr" not in created["warning_summary"]["optional_adapters_unconfigured"], "Kapowarr should not be a clean-install warning")
        require("kavita" not in created["warning_summary"]["optional_adapters_unconfigured"], "Kavita should not be a clean-install warning")
        require(isinstance(created["warning_summary"]["runtime_tools_missing"], list), "preflight summarizes missing runtime tools")
        require(isinstance(created["warning_summary"]["python_dependencies_missing"], list), "preflight summarizes missing Python dependencies")
        require(created["configured_adapters"]["comicvine"]["configured"] is False, "blank ComicVine is reported as unconfigured")
        require(created["configured_adapters"]["comicvine"]["configured_by"] == "", "blank ComicVine reports no configuration source")
        require(created["configured_adapters"]["comicvine"]["missing_required_keys"] == ["INKDROP_COMICVINE_API_KEY"], "blank ComicVine reports missing API key")
        require(created["configured_adapters"]["comicvine"]["reason"] == "missing required settings", "blank ComicVine reports a clear reason")
        require(created["configured_adapters"]["kapowarr"]["configured"] is False, "missing default Kapowarr DB is reported as unconfigured")
        require(created["configured_adapters"]["kapowarr"]["existing_path_missing_keys"] == [], "blank Kapowarr DB env has no missing explicit path")
        require(created["configured_adapters"]["kavita"]["configured"] is False, "missing default Kavita DB is reported as unconfigured")
        require(created["configured_adapters"]["komga"]["configured"] is False, "blank Komga URL is reported as unconfigured")
        require(created["configured_adapters"]["slskd"]["configured"] is False, "blank SLSKD API URL is reported as unconfigured")
        require(created["configured_adapters"]["suwayomi"]["configured"] is False, "blank Suwayomi API URL is reported as unconfigured")
        require("required_keys" in created["configured_adapters"]["prowlarr"], "preflight reports adapter required keys")
        require("existing_path_keys" in created["configured_adapters"]["kapowarr"], "preflight reports adapter path keys")
        require("existing_path_exists_keys" in created["configured_adapters"]["kapowarr"], "preflight reports adapter existing path evidence")
        require("reason" in created["configured_adapters"]["kapowarr"], "preflight reports adapter readiness reason")
        require("archive_tools" in created, "preflight reports archive tool availability")
        require("seven_zip" in created["archive_tools"], "preflight reports 7z availability")
        require("unrar" in created["archive_tools"], "preflight reports unrar fallback availability")
        strict_tools = inkdrop_preflight.run_preflight(env, create=True, strict_runtime_tools=True)
        missing_runtime_tools = set(created["warning_summary"]["runtime_tools_missing"])
        if missing_runtime_tools:
            require(not strict_tools["ok"], "strict runtime-tool preflight should fail when archive tools are missing")
            if "seven_zip" in missing_runtime_tools:
                require(any("7z is not available" in error for error in strict_tools["errors"]), "strict runtime-tool preflight reports missing 7z")
            if "unrar" in missing_runtime_tools:
                require(any("unrar is not available" in error for error in strict_tools["errors"]), "strict runtime-tool preflight reports missing unrar")
        else:
            require(strict_tools["ok"], "strict runtime-tool preflight should pass when required archive tools are available")
        require("python_dependencies" in created, "preflight reports Python dependency availability")
        for module_name in ("requests", "yaml", "bs4", "lxml", "py7zr", "rarfile"):
            require(module_name in created["python_dependencies"], f"preflight reports {module_name} dependency availability")

        secret_env = dict(env)
        secret_env.update(
            {
                "INKDROP_COMICVINE_API_KEY": "super-secret-comicvine",
                "INKDROP_PROWLARR_API_KEY": "super-secret-prowlarr",
                "INKDROP_SABNZBD_API_KEY": "super-secret-sab",
                "INKDROP_QBITTORRENT_USERNAME": "super-secret-user",
                "INKDROP_QBITTORRENT_PASSWORD": "super-secret-qbit",
                "INKDROP_WORKER_API_KEY": "super-secret-worker",
                "INKDROP_QBITTORRENT_URL": "http://user:super-secret-url-password@qbittorrent:8080",
                "INKDROP_PROWLARR_INTERNAL_BASE_URLS": "http://user:super-secret-prowlarr-url@prowlarr:9696",
            }
        )
        redacted = inkdrop_preflight.run_preflight(secret_env, create=True)
        effective_config = redacted.get("effective_config") or {}
        require("runtime_roots" in effective_config, "preflight reports effective runtime roots")
        require("env" in effective_config, "preflight reports redacted effective env")
        effective_env_json = str(effective_config.get("env"))
        require("super-secret" not in effective_env_json, "effective config should not leak secret values")
        require(effective_config["env"]["INKDROP_COMICVINE_API_KEY"] == "<set>", "effective config reports set secret state")
        require(effective_config["env"]["INKDROP_PROWLARR_API_KEY"] == "<set>", "effective config reports set Prowlarr secret state")
        require(effective_config["env"]["INKDROP_SABNZBD_API_KEY"] == "<set>", "effective config reports set SAB secret state")
        require(effective_config["env"]["INKDROP_QBITTORRENT_USERNAME"] == "<set>", "effective config reports set qBit username state")
        require(effective_config["env"]["INKDROP_QBITTORRENT_PASSWORD"] == "<set>", "effective config reports set qBit secret state")
        require(effective_config["env"]["INKDROP_WORKER_API_KEY"] == "<set>", "effective config redacts the worker API key")
        require(effective_config["env"]["INKDROP_QBITTORRENT_URL"] == "http://<redacted>@qbittorrent:8080", "effective config redacts URL user-info")
        require(effective_config["env"]["INKDROP_PROWLARR_INTERNAL_BASE_URLS"] == "http://<redacted>@prowlarr:9696", "effective config redacts URL-list user-info")

        custom_web_env = dict(env)
        custom_web_env["INKDROP_PORT"] = "8899"
        custom_web_env["INKDROP_HOST_PORT"] = "9876"
        custom_web = inkdrop_preflight.run_preflight(custom_web_env, create=True)
        require(custom_web["web"]["bind_port"] == 8899, "preflight reports custom web bind port")
        require(custom_web["web"]["host_port"] == 9876, "preflight reports a separate custom host port")
        require(custom_web["web"]["host_port_source"] == "INKDROP_HOST_PORT", "preflight identifies the explicit host-port source")
        require(custom_web["web"]["callback_base_url"] == "http://127.0.0.1:8899", "blank web base derives from custom local port")
        explicit_web_env = dict(custom_web_env)
        explicit_web_env["INKDROP_WEB_BASE_URL"] = "https://inkdrop.example.test/"
        explicit_web = inkdrop_preflight.run_preflight(explicit_web_env, create=True)
        require(explicit_web["web"]["callback_base_url"] == "https://inkdrop.example.test", "preflight reports explicit callback base URL without trailing slash")
        require(explicit_web["web"]["callback_base_source"] == "INKDROP_WEB_BASE_URL", "preflight reports explicit callback base source")
        invalid_host_env = dict(env)
        invalid_host_env["INKDROP_HOST"] = "http://0.0.0.0:8796"
        invalid_host = inkdrop_preflight.run_preflight(invalid_host_env, create=True)
        require(not invalid_host["ok"], "preflight should fail when INKDROP_HOST is accidentally configured as a URL")
        require(any("INKDROP_HOST must be a bind host/address" in error for error in invalid_host["errors"]), "invalid host error should be clear")
        invalid_base_env = dict(env)
        invalid_base_env["INKDROP_WEB_BASE_URL"] = "inkdrop.example.test"
        invalid_base = inkdrop_preflight.run_preflight(invalid_base_env, create=True)
        require(not invalid_base["ok"], "preflight should fail when INKDROP_WEB_BASE_URL is not a full URL")
        require(any("INKDROP_WEB_BASE_URL must be a full http(s) URL" in error for error in invalid_base["errors"]), "invalid web base URL error should be clear")
        missing_worker_key_env = dict(env)
        missing_worker_key_env["INKDROP_WEB_BASE_URL"] = "https://inkdrop.example.test"
        missing_worker_key = inkdrop_preflight.run_preflight(missing_worker_key_env, create=True)
        require(not missing_worker_key["ok"], "explicit worker HTTP base should require a worker API key")
        require(any("INKDROP_WORKER_API_KEY" in error for error in missing_worker_key["errors"]), "missing worker key preflight error should be actionable")
        configured_worker_key_env = dict(missing_worker_key_env)
        configured_worker_key_env["INKDROP_WORKER_API_KEY"] = "ik_test_worker_key"
        configured_worker_key = inkdrop_preflight.run_preflight(configured_worker_key_env, create=True)
        require(configured_worker_key["ok"], "explicit worker HTTP base with a configured key should pass preflight")
        valid_url_env = dict(env)
        valid_url_env["INKDROP_QBITTORRENT_URL"] = "http://qbittorrent:8080"
        valid_url_env["INKDROP_PROWLARR_INTERNAL_BASE_URLS"] = "http://prowlarr:9696,https://prowlarr.example.test"
        valid_urls = inkdrop_preflight.run_preflight(valid_url_env, create=True)
        require(valid_urls["ok"], "preflight should accept full http(s) adapter URLs")
        localhost_url_env = dict(env)
        localhost_url_env["INKDROP_PROWLARR_URL"] = "http://localhost:9696"
        localhost_url_env["INKDROP_PROWLARR_INTERNAL_BASE_URLS"] = "http://127.0.0.1:9696,http://prowlarr:9696"
        localhost_urls = inkdrop_preflight.run_preflight(localhost_url_env, create=True)
        require(localhost_urls["ok"], "preflight should warn but not fail for loopback adapter URLs")
        require(any("INKDROP_PROWLARR_URL points at localhost" in warning for warning in localhost_urls["warnings"]), "preflight should warn when adapter URL points at localhost")
        require(any("INKDROP_PROWLARR_INTERNAL_BASE_URLS entry 1 points at 127.0.0.1" in warning for warning in localhost_urls["warnings"]), "preflight should warn when URL-list adapter entry points at loopback")
        invalid_url_env = dict(env)
        invalid_url_env["INKDROP_QBITTORRENT_URL"] = "qbittorrent:8080"
        invalid_url = inkdrop_preflight.run_preflight(invalid_url_env, create=True)
        require(not invalid_url["ok"], "preflight should fail for malformed adapter URLs")
        require(any("INKDROP_QBITTORRENT_URL must be a full http(s) URL" in error for error in invalid_url["errors"]), "invalid adapter URL error should identify the env key")
        invalid_url_list_env = dict(env)
        invalid_url_list_env["INKDROP_PROWLARR_INTERNAL_BASE_URLS"] = "http://prowlarr:9696,prowlarr.local"
        invalid_url_list = inkdrop_preflight.run_preflight(invalid_url_list_env, create=True)
        require(not invalid_url_list["ok"], "preflight should fail for malformed URL list entries")
        require(any("INKDROP_PROWLARR_INTERNAL_BASE_URLS entry 2" in error for error in invalid_url_list["errors"]), "invalid URL list error should identify the entry")
        protocol_env = dict(env)
        protocol_env["INKDROP_PROTOCOL_ORDER"] = "torrent,usenet,direct"
        protocol_order = inkdrop_preflight.run_preflight(protocol_env, create=True)
        require(protocol_order["ok"], "preflight should accept direct in INKDROP_PROTOCOL_ORDER")
        invalid_protocol_env = dict(env)
        invalid_protocol_env["INKDROP_PROTOCOL_ORDER"] = "usenet,magic"
        invalid_protocol = inkdrop_preflight.run_preflight(invalid_protocol_env, create=True)
        require(not invalid_protocol["ok"], "preflight should fail for unsupported protocol-order values")
        require(any("INKDROP_PROTOCOL_ORDER contains unsupported value" in error for error in invalid_protocol["errors"]), "invalid protocol-order error should be clear")
        invalid_bool_env = dict(env)
        invalid_bool_env["INKDROP_IMPORT_READY_QUEUE_ONLY"] = "maybe"
        invalid_bool = inkdrop_preflight.run_preflight(invalid_bool_env, create=True)
        require(not invalid_bool["ok"], "preflight should fail for invalid boolean operator knobs")
        require(any("INKDROP_IMPORT_READY_QUEUE_ONLY must be a boolean value" in error for error in invalid_bool["errors"]), "invalid boolean knob error should be clear")
        invalid_debug_bool_env = dict(env)
        invalid_debug_bool_env["INKDROP_DEBUG_ACTIVE_REQUESTS"] = "maybe"
        invalid_debug_bool = inkdrop_preflight.run_preflight(invalid_debug_bool_env, create=True)
        require(not invalid_debug_bool["ok"], "preflight should fail for invalid debug boolean knobs")
        require(any("INKDROP_DEBUG_ACTIVE_REQUESTS must be a boolean value" in error for error in invalid_debug_bool["errors"]), "invalid debug boolean knob error should be clear")
        invalid_int_env = dict(env)
        invalid_int_env["INKDROP_PACK_PROBE_SCAN_ENTRIES"] = "many"
        invalid_int = inkdrop_preflight.run_preflight(invalid_int_env, create=True)
        require(not invalid_int["ok"], "preflight should fail for invalid integer operator knobs")
        require(any("INKDROP_PACK_PROBE_SCAN_ENTRIES must be an integer" in error for error in invalid_int["errors"]), "invalid integer knob error should be clear")
        invalid_recovery_bool_env = dict(env)
        invalid_recovery_bool_env["INKDROP_MISSING_RECOVERY_ENABLED"] = "sometimes"
        invalid_recovery_bool = inkdrop_preflight.run_preflight(invalid_recovery_bool_env, create=True)
        require(not invalid_recovery_bool["ok"], "preflight should fail for invalid recovery enablement")
        require(any("INKDROP_MISSING_RECOVERY_ENABLED must be a boolean value" in error for error in invalid_recovery_bool["errors"]), "invalid recovery enablement error should be clear")
        invalid_recovery_limit_env = dict(env)
        invalid_recovery_limit_env["INKDROP_MISSING_RECOVERY_MAX_HANDOFFS_PER_HOUR"] = "unlimited"
        invalid_recovery_limit = inkdrop_preflight.run_preflight(invalid_recovery_limit_env, create=True)
        require(not invalid_recovery_limit["ok"], "preflight should fail for invalid recovery resource limits")
        require(any("INKDROP_MISSING_RECOVERY_MAX_HANDOFFS_PER_HOUR must be an integer" in error for error in invalid_recovery_limit["errors"]), "invalid recovery limit error should be clear")
        invalid_quiet_hours_env = dict(env)
        invalid_quiet_hours_env["INKDROP_MISSING_RECOVERY_QUIET_HOURS"] = "overnight"
        invalid_quiet_hours = inkdrop_preflight.run_preflight(invalid_quiet_hours_env, create=True)
        require(not invalid_quiet_hours["ok"], "preflight should fail for malformed recovery quiet hours")
        require(any("INKDROP_MISSING_RECOVERY_QUIET_HOURS must be" in error for error in invalid_quiet_hours["errors"]), "invalid recovery quiet-hours error should be clear")

        mapping_env = dict(env)
        mapping_env["INKDROP_SAB_PATH_MAPPINGS"] = "//server/share=/staging/downloads,/downloads=/staging/downloads"
        mapping_env["INKDROP_UNC_PATH_MAPPINGS"] = r"\\server\\comics=/library/comics"
        mapping = inkdrop_preflight.run_preflight(mapping_env, create=True)
        require(mapping["ok"], "valid path mappings should pass preflight")
        require(mapping["path_mappings"]["INKDROP_SAB_PATH_MAPPINGS"]["entries"][0]["source"] == "<source-path>", "preflight redacts path mapping source")
        require(mapping["path_mappings"]["INKDROP_UNC_PATH_MAPPINGS"]["entries"][0]["target"] == "/library/comics", "preflight reports safe container mapping target")
        require(mapping["effective_config"]["env"]["INKDROP_SAB_PATH_MAPPINGS"] == "<2 mapping(s) configured>", "effective config summarizes SAB path mappings")
        require(mapping["effective_config"]["env"]["INKDROP_UNC_PATH_MAPPINGS"] == "<1 mapping(s) configured>", "effective config summarizes UNC path mappings")
        custom_mapping_env = dict(env)
        custom_mapping_env["INKDROP_DOWNLOAD_STAGING_ROOT"] = "/downloads"
        custom_mapping_env["INKDROP_SAB_PATH_MAPPINGS"] = "/host/downloads=/downloads/comics"
        custom_mapping = inkdrop_preflight.run_preflight(custom_mapping_env, create=True)
        require(custom_mapping["ok"], "path mapping targets may use explicitly configured InkDrop container roots")
        invalid_mapping_env = dict(env)
        invalid_mapping_env["INKDROP_SAB_PATH_MAPPINGS"] = "/downloads"
        invalid_mapping = inkdrop_preflight.run_preflight(invalid_mapping_env, create=True)
        require(not invalid_mapping["ok"], "invalid path mapping syntax should fail preflight")
        require(any("INKDROP_SAB_PATH_MAPPINGS entry 1" in error for error in invalid_mapping["errors"]), "invalid path mapping error should identify env key and entry")
        invalid_mapping_target_env = dict(env)
        invalid_mapping_target_env["INKDROP_SAB_PATH_MAPPINGS"] = "/host/downloads=/downloads"
        invalid_mapping_target = inkdrop_preflight.run_preflight(invalid_mapping_target_env, create=True)
        require(not invalid_mapping_target["ok"], "unmounted/default-unknown mapping targets should fail preflight")
        require(any("target must be under an InkDrop container root" in error for error in invalid_mapping_target["errors"]), "invalid mapping target error should explain allowed roots")
        invalid_windows_mapping_env = dict(env)
        invalid_windows_mapping_env["INKDROP_SAB_PATH_MAPPINGS"] = r"/host/downloads=C:\Downloads"
        invalid_windows_mapping = inkdrop_preflight.run_preflight(invalid_windows_mapping_env, create=True)
        require(not invalid_windows_mapping["ok"], "Windows drive mapping targets should fail preflight")
        require(any("not a Windows drive path" in error for error in invalid_windows_mapping["errors"]), "Windows mapping target error should be clear")

        existing_path_env = dict(env)
        existing_path_env["INKDROP_KAPOWARR_DB"] = str(root / "config" / "kapowarr.db")
        Path(existing_path_env["INKDROP_KAPOWARR_DB"]).parent.mkdir(parents=True, exist_ok=True)
        Path(existing_path_env["INKDROP_KAPOWARR_DB"]).write_text("", encoding="utf-8")
        path_configured = inkdrop_preflight.run_preflight(existing_path_env, create=True)
        require(path_configured["configured_adapters"]["kapowarr"]["configured"] is True, "existing Kapowarr DB path can configure adapter")
        require(path_configured["configured_adapters"]["kapowarr"]["configured_by"] == "existing_path", "existing Kapowarr DB reports path-based configuration")
        require(path_configured["configured_adapters"]["kapowarr"]["existing_path_exists_keys"] == ["INKDROP_KAPOWARR_DB"], "existing Kapowarr DB reports matching path key")
        require(path_configured["configured_adapters"]["kapowarr"]["reason"] == "adapter path exists", "existing Kapowarr DB reports a clear reason")

        invalid_port_env = dict(env)
        invalid_port_env["INKDROP_PORT"] = "not-a-port"
        invalid_port = inkdrop_preflight.run_preflight(invalid_port_env, create=True)
        require(not invalid_port["ok"], "preflight should fail for invalid INKDROP_PORT")
        require(any("INKDROP_PORT must be an integer" in error for error in invalid_port["errors"]), "invalid port error should be clear")

        out_of_range_port_env = dict(env)
        out_of_range_port_env["INKDROP_PORT"] = "70000"
        out_of_range_port = inkdrop_preflight.run_preflight(out_of_range_port_env, create=True)
        require(not out_of_range_port["ok"], "preflight should fail for out-of-range INKDROP_PORT")
        require(any("INKDROP_PORT must be between" in error for error in out_of_range_port["errors"]), "out-of-range port error should be clear")
        for boundary in ("1", "65535"):
            boundary_env = dict(env)
            boundary_env["INKDROP_HOST_PORT"] = boundary
            boundary_result = inkdrop_preflight.run_preflight(boundary_env, create=True)
            require(boundary_result["ok"], f"preflight should accept INKDROP_HOST_PORT={boundary}")
            require(boundary_result["web"]["host_port"] == int(boundary), f"preflight should report INKDROP_HOST_PORT={boundary}")
        invalid_host_port_env = dict(env)
        invalid_host_port_env["INKDROP_HOST_PORT"] = "not-a-port"
        invalid_host_port = inkdrop_preflight.run_preflight(invalid_host_port_env, create=True)
        require(not invalid_host_port["ok"], "preflight should fail for invalid INKDROP_HOST_PORT")
        require(any("INKDROP_HOST_PORT must be an integer" in error for error in invalid_host_port["errors"]), "invalid host port error should be clear")
        out_of_range_host_port_env = dict(env)
        out_of_range_host_port_env["INKDROP_HOST_PORT"] = "0"
        out_of_range_host_port = inkdrop_preflight.run_preflight(out_of_range_host_port_env, create=True)
        require(not out_of_range_host_port["ok"], "preflight should fail for out-of-range INKDROP_HOST_PORT")
        require(any("INKDROP_HOST_PORT must be between" in error for error in out_of_range_host_port["errors"]), "out-of-range host port error should be clear")


def assert_preflight_cli_json_contract():
    root = Path(__file__).resolve().parent

    def base_env(tmp_root):
        env = dict(os.environ)
        env.update(
            {
                inkdrop_runtime_config.ENV_CONFIG_DIR: str(tmp_root / "config"),
                inkdrop_runtime_config.ENV_STATE_DIR: str(tmp_root / "state"),
                inkdrop_runtime_config.ENV_LOG_DIR: str(tmp_root / "logs"),
                inkdrop_runtime_config.ENV_CACHE_DIR: str(tmp_root / "cache"),
                inkdrop_runtime_config.ENV_BACKUP_DIR: str(tmp_root / "backups"),
                inkdrop_runtime_config.ENV_STAGING_DIR: str(tmp_root / "staging"),
                inkdrop_runtime_config.ENV_MANUAL_INBOX_DIR: str(tmp_root / "manual-inbox"),
                inkdrop_runtime_config.ENV_QUARANTINE_DIR: str(tmp_root / "quarantine"),
                "INKDROP_COMICVINE_API_KEY": "",
                "INKDROP_PROWLARR_API_KEY": "",
                "INKDROP_SABNZBD_API_KEY": "",
                "INKDROP_QBITTORRENT_PASSWORD": "",
            }
        )
        return env

    with tempfile.TemporaryDirectory(prefix="inkdrop-preflight-cli-smoke-") as tmp:
        tmp_root = Path(tmp)
        clean = subprocess.run(
            [sys.executable, "-B", "inkdrop_preflight.py", "--create", "--json"],
            cwd=root,
            env=base_env(tmp_root),
            text=True,
            capture_output=True,
            timeout=60,
        )
        require(clean.returncode == 0, f"clean preflight CLI should exit 0, got {clean.returncode}: {clean.stderr}")
        require(not clean.stderr.strip(), "clean preflight CLI --json should not write stderr")
        clean_payload = json.loads(clean.stdout)
        require(clean_payload.get("ok") is True, "clean preflight CLI JSON should report ok=true")
        require(clean_payload.get("preflight_schema_version") == 1, "clean preflight CLI JSON should report schema version 1")
        require(clean_payload.get("created_missing_dirs") is True, "clean preflight CLI JSON should report created dirs")
        require(clean_payload.get("state_db_path", "").replace("\\", "/").endswith("/state/inkdrop-state.sqlite3"), "clean preflight CLI JSON should report state DB path")

        bad_env = base_env(tmp_root / "bad")
        bad_env.update(
            {
                "INKDROP_QBITTORRENT_URL": "qbittorrent:8080",
                "INKDROP_PROWLARR_URL": "http://prowlarr:9696?apikey=" + "fixture-secret-query&label=public",
                "INKDROP_SAB_PATH_MAPPINGS": r"/host/downloads=C:\secret-downloads",
                "INKDROP_COMICVINE_API_KEY": "super-secret-comicvine",
                "INKDROP_PROWLARR_INTERNAL_BASE_URLS": "http://user:super-secret-url@prowlarr:9696,bad-prowlarr",
            }
        )
        bad = subprocess.run(
            [sys.executable, "-B", "inkdrop_preflight.py", "--create", "--json"],
            cwd=root,
            env=bad_env,
            text=True,
            capture_output=True,
            timeout=60,
        )
        require(bad.returncode == 1, f"malformed preflight CLI should exit 1, got {bad.returncode}")
        require(not bad.stderr.strip(), "malformed preflight CLI --json should keep errors in JSON stdout, not stderr")
        bad_payload = json.loads(bad.stdout)
        require(bad_payload.get("ok") is False, "malformed preflight CLI JSON should report ok=false")
        require(bad_payload.get("preflight_schema_version") == 1, "malformed preflight CLI JSON should report schema version 1")
        errors = bad_payload.get("errors") or []
        require(any("INKDROP_QBITTORRENT_URL must be a full http(s) URL" in error for error in errors), "malformed preflight CLI JSON should include provider URL error")
        require(any("INKDROP_PROWLARR_INTERNAL_BASE_URLS entry 2" in error for error in errors), "malformed preflight CLI JSON should include URL-list entry error")
        require(any("INKDROP_SAB_PATH_MAPPINGS entry 1" in error and "Windows drive path" in error for error in errors), "malformed preflight CLI JSON should include path-mapping target error")
        effective_env = ((bad_payload.get("effective_config") or {}).get("env") or {})
        require(effective_env.get("INKDROP_COMICVINE_API_KEY") == "<set>", "malformed preflight CLI JSON should redact secret state")
        require("super-secret-query" not in effective_env.get("INKDROP_PROWLARR_URL", ""), "malformed preflight CLI JSON should redact secret URL query values")
        require("label=public" in effective_env.get("INKDROP_PROWLARR_URL", ""), "malformed preflight CLI JSON should preserve non-secret URL query values")
        require(effective_env.get("INKDROP_SAB_PATH_MAPPINGS") == "<1 mapping(s) configured>", "malformed preflight CLI JSON should summarize path mappings")
        require(bad_payload["path_mappings"]["INKDROP_SAB_PATH_MAPPINGS"]["entries"][0]["source"] == "<source-path>", "malformed preflight CLI JSON should redact mapping source")
        require(bad_payload["path_mappings"]["INKDROP_SAB_PATH_MAPPINGS"]["entries"][0]["target"] == "<invalid-target>", "malformed preflight CLI JSON should redact invalid mapping target")
        for secret in ("super-secret", r"C:\secret-downloads"):
            require(secret not in bad.stdout, "malformed preflight CLI JSON should not leak secrets or private host paths")


def assert_container_start_failure_diagnostics():
    import inkdrop_container_start

    def run_startup(env_updates):
        old_env = dict(os.environ)
        stderr = io.StringIO()
        try:
            with tempfile.TemporaryDirectory(prefix="inkdrop-container-start-smoke-") as tmp:
                root = Path(tmp)
                os.environ.update(
                    {
                        inkdrop_runtime_config.ENV_CONFIG_DIR: str(root / "config"),
                        inkdrop_runtime_config.ENV_STATE_DIR: str(root / "state"),
                        inkdrop_runtime_config.ENV_LOG_DIR: str(root / "logs"),
                        inkdrop_runtime_config.ENV_CACHE_DIR: str(root / "cache"),
                        inkdrop_runtime_config.ENV_BACKUP_DIR: str(root / "backups"),
                        inkdrop_runtime_config.ENV_STAGING_DIR: str(root / "staging"),
                        inkdrop_runtime_config.ENV_MANUAL_INBOX_DIR: str(root / "manual-inbox"),
                        inkdrop_runtime_config.ENV_QUARANTINE_DIR: str(root / "quarantine"),
                    }
                )
                os.environ.update(env_updates)
                with contextlib.redirect_stderr(stderr):
                    rc = inkdrop_container_start.main()
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        text = stderr.getvalue()
        payload = json.loads(text)
        return rc, text, payload

    rc, text, payload = run_startup({inkdrop_runtime_config.ENV_PORT: "not-a-port"})
    require(rc == 1, "container start shim should fail when strict preflight fails")
    require(payload.get("ok") is False, "container start shim should print JSON failure payload")
    require(payload.get("preflight_schema_version") == 1, "container start shim should preserve preflight schema version")
    require(payload.get("startup_phase") == "preflight", "container start shim should label preflight failures")
    require("INKDROP_PORT must be an integer" in text, "container start shim should include clear preflight error")
    require("super-secret" not in text, "container start shim diagnostics should remain redacted")

    rc, text, payload = run_startup(
        {
            "INKDROP_QBITTORRENT_URL": "qbittorrent:8080",
            "INKDROP_PROWLARR_URL": "http://prowlarr:9696?apikey=" + "fixture-secret-query&label=public",
            "INKDROP_SAB_PATH_MAPPINGS": r"/host/downloads=C:\secret-downloads",
            "INKDROP_COMICVINE_API_KEY": "super-secret-comicvine",
            "INKDROP_PROWLARR_API_KEY": "super-secret-prowlarr",
            "INKDROP_QBITTORRENT_PASSWORD": "super-secret-qbit",
            "INKDROP_PROWLARR_INTERNAL_BASE_URLS": "http://user:super-secret-url@prowlarr:9696,bad-prowlarr",
        }
    )
    require(rc == 1, "container start shim should fail for malformed URLs/path mappings")
    require(payload.get("ok") is False, "malformed config startup should print JSON failure payload")
    errors = payload.get("errors") or []
    require(any("INKDROP_QBITTORRENT_URL must be a full http(s) URL" in error for error in errors), "container diagnostics should include malformed provider URL errors")
    require(any("INKDROP_PROWLARR_INTERNAL_BASE_URLS entry 2" in error for error in errors), "container diagnostics should include malformed URL-list entry errors")
    require(any("INKDROP_SAB_PATH_MAPPINGS entry 1" in error and "Windows drive path" in error for error in errors), "container diagnostics should include malformed path-mapping target errors")
    effective_env = ((payload.get("effective_config") or {}).get("env") or {})
    require(effective_env.get("INKDROP_COMICVINE_API_KEY") == "<set>", "startup failure diagnostics should redact ComicVine secret state")
    require(effective_env.get("INKDROP_PROWLARR_API_KEY") == "<set>", "startup failure diagnostics should redact Prowlarr secret state")
    require(effective_env.get("INKDROP_QBITTORRENT_PASSWORD") == "<set>", "startup failure diagnostics should redact qBit password state")
    require("super-secret-query" not in effective_env.get("INKDROP_PROWLARR_URL", ""), "startup failure diagnostics should redact secret URL query values")
    require("label=public" in effective_env.get("INKDROP_PROWLARR_URL", ""), "startup failure diagnostics should preserve non-secret URL query values")
    require(effective_env.get("INKDROP_PROWLARR_INTERNAL_BASE_URLS") == "http://<redacted>@prowlarr:9696,bad-prowlarr", "startup failure diagnostics should redact URL-list user-info")
    require(effective_env.get("INKDROP_SAB_PATH_MAPPINGS") == "<1 mapping(s) configured>", "startup failure diagnostics should summarize path mappings")
    require(payload["path_mappings"]["INKDROP_SAB_PATH_MAPPINGS"]["entries"][0]["source"] == "<source-path>", "startup failure diagnostics should redact path mapping source")
    require(payload["path_mappings"]["INKDROP_SAB_PATH_MAPPINGS"]["entries"][0]["target"] == "<invalid-target>", "startup failure diagnostics should redact invalid path mapping target")
    for secret in ("super-secret", r"C:\secret-downloads"):
        require(secret not in text, "container start shim diagnostics should remain redacted")

    original_run_preflight = inkdrop_container_start.inkdrop_preflight.run_preflight
    try:
        def crash_preflight(*args, **kwargs):
            raise RuntimeError("synthetic preflight crash")

        stderr = io.StringIO()
        inkdrop_container_start.inkdrop_preflight.run_preflight = crash_preflight
        with contextlib.redirect_stderr(stderr):
            rc = inkdrop_container_start.main()
        payload = json.loads(stderr.getvalue())
        require(rc == 1, "container start shim should fail cleanly when preflight crashes")
        require(payload.get("preflight_schema_version") == 1, "preflight crash diagnostics should include schema version")
        require(payload.get("startup_phase") == "preflight", "preflight crash diagnostics should label startup phase")
        require(any("preflight crashed before web startup" in error for error in payload.get("errors") or []), "preflight crash diagnostics should explain the crash")
    finally:
        inkdrop_container_start.inkdrop_preflight.run_preflight = original_run_preflight

    original_execvp = inkdrop_container_start.os.execvp
    try:
        def fail_exec(*args, **kwargs):
            raise OSError("synthetic exec failure")

        stderr = io.StringIO()
        inkdrop_container_start.os.execvp = fail_exec
        inkdrop_container_start.inkdrop_preflight.run_preflight = lambda *args, **kwargs: {"ok": True, "preflight_schema_version": 1}
        # This fixture tests the exec path's diagnostics, which is the
        # dedicated-worker layout; the default now supervises web plus
        # scheduler and has its own smoke.
        previous_scheduler_env = os.environ.get("INKDROP_CONTAINER_SCHEDULER_ENABLED")
        os.environ["INKDROP_CONTAINER_SCHEDULER_ENABLED"] = "0"
        try:
            with contextlib.redirect_stderr(stderr):
                rc = inkdrop_container_start.main()
        finally:
            if previous_scheduler_env is None:
                os.environ.pop("INKDROP_CONTAINER_SCHEDULER_ENABLED", None)
            else:
                os.environ["INKDROP_CONTAINER_SCHEDULER_ENABLED"] = previous_scheduler_env
        payload = json.loads(stderr.getvalue())
        require(rc == 127, "container start shim should return 127 when web exec fails")
        require(payload.get("preflight_schema_version") == 1, "web exec failure diagnostics should include schema version")
        require(payload.get("startup_phase") == "web_exec", "web exec failure diagnostics should label startup phase")
        require(any("failed to exec web entrypoint" in error for error in payload.get("errors") or []), "web exec failure diagnostics should explain the exec failure")
    finally:
        inkdrop_container_start.os.execvp = original_execvp
        inkdrop_container_start.inkdrop_preflight.run_preflight = original_run_preflight


def assert_container_healthcheck_diagnostics():
    import inkdrop_container_healthcheck

    original_run_preflight = inkdrop_container_healthcheck.inkdrop_preflight.run_preflight
    original_probe_status = inkdrop_container_healthcheck._probe_status
    original_web_port = inkdrop_container_healthcheck.inkdrop_runtime_config.web_port
    try:
        inkdrop_container_healthcheck.inkdrop_preflight.run_preflight = lambda **kwargs: {"ok": True}
        preflight_only = inkdrop_container_healthcheck.run_healthcheck(preflight_only=True)
        require(preflight_only.get("ok") is True, "container healthcheck preflight-only mode should pass when strict preflight passes")
        require(preflight_only.get("phase") == "preflight", "container healthcheck preflight-only mode should label preflight phase")

        inkdrop_container_healthcheck.inkdrop_runtime_config.web_port = lambda strict=True: 8796
        inkdrop_container_healthcheck._probe_status = lambda port, timeout, wait_seconds=0.0: {
            "ok": True,
            "status": "ok",
            "status_cache": "fresh",
            "status_partial": False,
            "host": "127.0.0.1",
            "port": port,
            "timeout_seconds": timeout,
            "wait_seconds": wait_seconds,
            "attempts": 1,
            "elapsed_seconds": 0.01,
        }
        healthy = inkdrop_container_healthcheck.run_healthcheck()
        require(healthy.get("ok") is True, "container healthcheck should pass when preflight and HTTP status pass")
        require(healthy.get("phase") == "http", "container healthcheck should label HTTP phase after status probe")
        require(healthy.get("status_url") == "http://127.0.0.1:8796/status.json", "container healthcheck should report the status URL")
        require(healthy.get("status_probe", {}).get("port") == 8796, "container healthcheck success should report the probed port")
        require("elapsed_seconds" in healthy.get("status_probe", {}), "container healthcheck success should report probe duration")
        healthy_wait = inkdrop_container_healthcheck.run_healthcheck(wait_seconds=2)
        require(healthy_wait.get("status_probe", {}).get("wait_seconds") == 2, "manual healthcheck wait mode should pass wait seconds into the HTTP probe")
        require(healthy_wait.get("status_probe", {}).get("attempts") == 1, "manual healthcheck wait mode should report probe attempts")

        inkdrop_container_healthcheck._probe_status = lambda port, timeout, wait_seconds=0.0: {
            "ok": False,
            "error": "connection refused",
            "error_type": "ConnectionRefusedError",
            "host": "127.0.0.1",
            "port": port,
            "timeout_seconds": timeout,
            "wait_seconds": wait_seconds,
            "attempts": 2 if wait_seconds else 1,
            "elapsed_seconds": 0.02,
        }
        unhealthy_http = inkdrop_container_healthcheck.run_healthcheck(wait_seconds=1)
        require(unhealthy_http.get("ok") is False, "container healthcheck should fail when HTTP status probe fails")
        require(unhealthy_http.get("phase") == "http", "container healthcheck HTTP failures should label HTTP phase")
        require(unhealthy_http.get("preflight_ok") is True, "container healthcheck HTTP failures should preserve preflight evidence")
        require(unhealthy_http.get("status_probe", {}).get("error_type") == "ConnectionRefusedError", "container healthcheck HTTP failures should include error type")
        require(unhealthy_http.get("status_probe", {}).get("port") == 8796, "container healthcheck HTTP failures should report the probed port")
        require(unhealthy_http.get("status_probe", {}).get("wait_seconds") == 1, "container healthcheck HTTP failures should report wait window")
        require(unhealthy_http.get("status_probe", {}).get("attempts") == 2, "container healthcheck HTTP failures should report probe attempts")
        require("elapsed_seconds" in unhealthy_http.get("status_probe", {}), "container healthcheck HTTP failures should report probe duration")

        inkdrop_container_healthcheck.inkdrop_preflight.run_preflight = lambda **kwargs: {"ok": False, "errors": ["missing tool"]}
        unhealthy_preflight = inkdrop_container_healthcheck.run_healthcheck()
        require(unhealthy_preflight.get("ok") is False, "container healthcheck should fail when strict preflight fails")
        require(unhealthy_preflight.get("phase") == "preflight", "container healthcheck preflight failures should label preflight phase")
    finally:
        inkdrop_container_healthcheck.inkdrop_preflight.run_preflight = original_run_preflight
        inkdrop_container_healthcheck._probe_status = original_probe_status
        inkdrop_container_healthcheck.inkdrop_runtime_config.web_port = original_web_port


def assert_runtime_optional_root_failures_do_not_crash_web_startup():
    with tempfile.TemporaryDirectory(prefix="inkdrop-runtime-roots-smoke-") as tmp:
        root = Path(tmp)
        env = {
            inkdrop_runtime_config.ENV_CONFIG_DIR: str(root / "config"),
            inkdrop_runtime_config.ENV_STATE_DIR: str(root / "state"),
            inkdrop_runtime_config.ENV_LOG_DIR: str(root / "logs"),
            inkdrop_runtime_config.ENV_CACHE_DIR: str(root / "cache"),
            inkdrop_runtime_config.ENV_BACKUP_DIR: str(root / "backups"),
            inkdrop_runtime_config.ENV_STAGING_DIR: str(root / "offline-staging"),
            inkdrop_runtime_config.ENV_MANUAL_INBOX_DIR: str(root / "offline-manual-inbox"),
            inkdrop_runtime_config.ENV_QUARANTINE_DIR: str(root / "offline-quarantine"),
        }
        original_mkdir = inkdrop_runtime_config.Path.mkdir

        def fake_mkdir(path, *args, **kwargs):
            text = str(path)
            if "offline-" in text:
                raise OSError("simulated optional media root outage")
            return original_mkdir(path, *args, **kwargs)

        try:
            inkdrop_runtime_config.Path.mkdir = fake_mkdir
            roots = inkdrop_runtime_config.ensure_runtime_roots(env)
        finally:
            inkdrop_runtime_config.Path.mkdir = original_mkdir
        require(roots["staging_dir"] == root / "offline-staging", "optional staging root should still be reported")
        require((root / "config").is_dir(), "required config root should be created")
        require((root / "state").is_dir(), "required state root should be created")

        env[inkdrop_runtime_config.ENV_CONFIG_DIR] = str(root / "offline-config")
        try:
            inkdrop_runtime_config.Path.mkdir = fake_mkdir
            try:
                inkdrop_runtime_config.ensure_runtime_roots(env)
            except OSError:
                pass
            else:
                require(False, "required config root outage should still fail startup")
        finally:
            inkdrop_runtime_config.Path.mkdir = original_mkdir


def assert_web_uses_runtime_config():
    text = (Path(__file__).resolve().parent / "inkdrop_web.py").read_text(encoding="utf-8")
    require("import inkdrop_runtime_config" in text, "web runtime imports inkdrop_runtime_config")
    require("HOST = inkdrop_runtime_config.web_host()" in text, "web host is runtime-config-backed")
    require("PORT = inkdrop_runtime_config.web_port(strict=False)" in text, "web import should not crash on invalid port config")
    require("PORT = inkdrop_runtime_config.web_port(strict=True)" in text, "web startup validates port config before binding")
    require("STATE_DIR = inkdrop_runtime_config.state_dir()" in text, "web state dir is runtime-config-backed")
    require("LOG_DIR = inkdrop_runtime_config.log_dir()" in text, "web log dir is runtime-config-backed")
    require("inkdrop_runtime_config.ensure_runtime_roots()" in text, "web startup creates runtime roots")
    require('SLSKD_API_BASE_URL = env_value("INKDROP_SLSKD_API_BASE_URL", "")' in text, "web should not invent a default SLSKD API endpoint")
    require('KAPOWARR_URL = env_value("INKDROP_KAPOWARR_URL", "")' in text, "web should not invent a default Kapowarr API endpoint")
    require('KAVITA_API = env_value("INKDROP_KAVITA_URL", "")' in text, "web should not invent a default Kavita API endpoint")
    require('KOMGA_API = env_value("INKDROP_KOMGA_URL", "")' in text, "web should not invent a default Komga API endpoint")
    require('base_url=KAVITA_API' in text, "runtime Kavita provider template should use configured URL")
    require('base_url=KOMGA_API' in text, "runtime Komga provider template should use configured URL")
    require('base_url=os.environ.get("INKDROP_PROWLARR_URL") or ""' in text, "runtime Prowlarr provider template should use configured URL")
    require('base_url=SLSKD_API_BASE_URL' in text, "runtime SLSKD provider template should use configured URL")
    require('runtimePath("slskd_web_url", "")' in text, "Open SLSKD actions should not default to localhost")
    startup_defaults = "\n".join(text.splitlines()[:450])
    for token in PRIVATE_TOKENS:
        require(token not in startup_defaults, f"web startup defaults contain private token {token}")


def assert_optional_adapter_defaults_are_explicit():
    root = Path(__file__).resolve().parent
    completed_import = (root / "inkdrop_completed_import.py").read_text(encoding="utf-8")
    slskd_probe = (root / "inkdrop_slskd_source_probe.py").read_text(encoding="utf-8")
    manual_autoresolve = (root / "inkdrop_manual_source_autoresolve.py").read_text(encoding="utf-8")
    series_autopilot = (root / "inkdrop_series_autopilot.py").read_text(encoding="utf-8")
    acquire = (root / "inkdrop_acquire.py").read_text(encoding="utf-8")
    missing_acquire = (root / "inkdrop_missing_acquire.py").read_text(encoding="utf-8")
    reconcile = (root / "inkdrop_reconcile_imports.py").read_text(encoding="utf-8")
    require('KAVITA_API = os.environ.get("INKDROP_KAVITA_URL") or ""' in completed_import, "Kavita importer URL should be blank unless configured")
    require('KOMGA_API = os.environ.get("INKDROP_KOMGA_URL") or ""' in completed_import, "Komga importer URL should be blank unless configured")
    require('KAPOWARR_API = os.environ.get("INKDROP_KAPOWARR_URL") or ""' in completed_import, "Kapowarr importer URL should be blank unless configured")
    require('"qBittorrent URL is not configured' in completed_import, "completed import qBittorrent checks should report missing URL clearly")
    require('DEFAULT_SLSKD_BASE_URL = os.environ.get("INKDROP_SLSKD_API_BASE_URL") or ""' in slskd_probe, "SLSKD probe URL should be blank unless configured")
    require('SLSKD_BASE_URL = os.environ.get("INKDROP_SLSKD_API_BASE_URL") or ""' in manual_autoresolve, "SLSKD autoresolver URL should be blank unless configured")
    require("SLSKD API base URL is not configured" in slskd_probe, "SLSKD probe should report missing URL clearly")
    require("SLSKD API base URL is not configured" in manual_autoresolve, "SLSKD autoresolver should report missing URL clearly")
    require("inkdrop_runtime_config.worker_web_base_url()" in slskd_probe, "SLSKD mark-waiting callback should use the container-aware worker base")
    require("inkdrop_runtime_config.worker_web_base_url()" in series_autopilot, "series autopilot should use the container-aware worker base")
    require('"http://127.0.0.1:8796"' not in slskd_probe, "SLSKD probe should not hardcode the default InkDrop port")
    require('"http://127.0.0.1:8796"' not in series_autopilot, "series autopilot should not hardcode the default InkDrop port")
    require('DEFAULT_PROWLARR_BASE_URL = str(os.environ.get("INKDROP_PROWLARR_URL") or "").strip().rstrip("/")' in acquire, "Prowlarr acquire URL should come from env/provider config")
    require('"Prowlarr URL is not configured' in acquire, "Prowlarr acquire should report missing URL clearly")
    require("PROWLARR_INTERNAL_BASE_URLS" in acquire, "Prowlarr URL rewrite inputs should be explicit configuration")
    require('"http://127.0.0.1:9696", "http://localhost:9696"' not in acquire, "Prowlarr acquire should not hardcode local URL rewrite defaults")
    require('os.environ.get("INKDROP_SOURCE_WORKER_PROWLARR_ALLOWED_HOSTS") or ""' in series_autopilot, "source-worker Prowlarr allowed hosts should default blank")
    require('SOURCE_WORKER_PROWLARR_ALLOWED_HOSTS = ("127.0.0.1", "localhost")' not in series_autopilot, "source-worker Prowlarr allowed hosts should not default to loopback")
    require("def source_worker_prowlarr_allowed_hosts()" in series_autopilot, "source-worker Prowlarr hosts should derive from configured URLs")
    require('os.environ.get("INKDROP_TRUSTED_PROWLARR_HOSTS") or ""' in missing_acquire, "trusted Prowlarr metadata hosts should default blank")
    require('os.environ.get("INKDROP_TRUSTED_PROWLARR_HOSTS") or "127.0.0.1,localhost"' not in missing_acquire, "trusted Prowlarr metadata hosts should not default to loopback")
    require('"qBittorrent URL is not configured' in acquire, "qBittorrent acquire should report missing URL clearly")
    require('"SABnzbd URL is not configured' in acquire, "SABnzbd acquire should report missing URL clearly")
    require("def inkdrop_web_api_url" in missing_acquire, "missing acquire should derive InkDrop web callback URLs from runtime config")
    require('inkdrop_web_api_url("/api/pack-review/state")' in missing_acquire, "pack-review state helper should not hardcode a local InkDrop URL")
    require("worker_auth_headers(required=True)" in missing_acquire, "pack-review HTTP callback should use the supported worker API-key header")
    require('"http://127.0.0.1:8796/api/pack-review/state"' not in missing_acquire, "missing acquire should not hardcode the pack-review state URL")
    require('os.environ.get("INKDROP_QBITTORRENT_URL") or cfg.get("host") or ""' in reconcile, "qBittorrent reconcile should prefer env URL and avoid localhost fallback")


def assert_protocol_order_contract():
    root = Path(__file__).resolve().parent
    acquire = (root / "inkdrop_acquire.py").read_text(encoding="utf-8")
    missing_acquire = (root / "inkdrop_missing_acquire.py").read_text(encoding="utf-8")
    for label, text in (
        ("inkdrop_acquire.py", acquire),
        ("inkdrop_missing_acquire.py", missing_acquire),
    ):
        require('DEFAULT_PROTOCOL_ORDER = ["usenet", "torrent", "direct"]' in text, f"{label} should keep direct in default protocol order")
        require('"directdownload": "direct"' in text, f"{label} should normalize direct-download aliases")
        require('{"usenet", "torrent", "direct"}' in text, f"{label} should preserve direct in normalized protocol order")


def assert_release_safety_audit():
    path = Path(__file__).resolve().parent / "inkdrop-public-release-safety-audit.py"
    text = path.read_text(encoding="utf-8")
    require("scan_github_workflows" in text, "release safety audit scans GitHub workflow files")
    require("scan_public_image_context_private_text_files" in text, "release safety audit scans text files included in Docker context")
    require("scan_env_example_secret_defaults" in text, "release safety audit checks .env.example secret defaults")
    require("env_example_secret_default" in text, "release safety audit reports nonblank .env.example secrets")
    require("docs/inkdrop-source-candidate-catalog-20260702.json" in text, "release safety audit treats source catalog as public artifact")
    require('"inkdrop_container_healthcheck.py"' in text, "release safety audit treats container healthcheck as a public runtime artifact")
    spec = importlib.util.spec_from_file_location("inkdrop_public_release_safety_audit", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    findings = []
    for name in module.PUBLIC_FILES:
        file_path = module.ROOT / name
        if file_path.exists():
            findings.extend(module.scan_file(file_path))
    findings.extend(module.scan_file(module.ROOT / module.STARTUP_FILE, max_lines=module.STARTUP_SCAN_LINES))
    for name, max_lines in module.BACKEND_STARTUP_SCANS.items():
        findings.extend(module.scan_file(module.ROOT / name, max_lines=max_lines))
    for name in module.FULL_FILE_SCANS:
        findings.extend(module.scan_file(module.ROOT / name))
    findings.extend(module.scan_public_image_context_private_text_files())
    findings.extend(module.scan_env_example_secret_defaults())
    require(module._is_secret_env_key("INKDROP_COMICVINE_API_KEY"), "release safety audit should classify API keys as secret env keys")
    require(module._is_secret_env_key("INKDROP_QBITTORRENT_PASSWORD"), "release safety audit should classify passwords as secret env keys")
    require(not module._is_secret_env_key("INKDROP_COMIC_ROOT"), "release safety audit should not classify normal roots as secret env keys")
    require(not findings, f"release safety audit found {len(findings)} issue(s): {findings[:3]}")


def main():
    old_env = dict(os.environ)
    try:
        for key in (
            inkdrop_runtime_config.ENV_CONFIG_DIR,
            inkdrop_runtime_config.ENV_STATE_DIR,
            inkdrop_runtime_config.ENV_LOG_DIR,
            inkdrop_runtime_config.ENV_CACHE_DIR,
            inkdrop_runtime_config.ENV_BACKUP_DIR,
            inkdrop_runtime_config.ENV_STAGING_DIR,
            inkdrop_runtime_config.ENV_MANUAL_INBOX_DIR,
            inkdrop_runtime_config.ENV_QUARANTINE_DIR,
        ):
            os.environ.pop(key, None)
        assert_runtime_defaults()
        assert_runtime_env_overrides()
        assert_runtime_state_only_uses_state_as_config()
        assert_packaging_files()
        assert_compose_yaml_contract()
        assert_compose_env_interpolation_contract()
        assert_public_release_workflow_contract()
        assert_public_release_runner_contract()
        assert_public_release_runner_json_contract()
        assert_public_release_runner_docker_unavailable_json_contract()
        assert_public_release_runner_docker_only_unavailable_json_contract()
        assert_public_release_docs_contract()
        assert_install_support_summary_contract()
        assert_web_surface_audit_contract()
        assert_settings_api_surface_audit_contract()
        assert_state_schema_audit_contract()
        assert_preflight_config_key_contract()
        assert_public_operator_knobs_are_visible()
        assert_manual_source_callback_derivation_contract()
        assert_worker_callback_runtime_contract()
        assert_image_defaults_live_under_documented_mounts()
        assert_docker_python_allowlist_import_closure()
        assert_docker_context_manifest_contract()
        assert_docker_core_worker_scripts_included()
        assert_runtime_source_catalog_contract()
        assert_docker_runtime_python_files_compile()
        assert_runtime_third_party_imports_are_declared()
        assert_dockerignore_allowlist_shape()
        assert_preflight_contract()
        assert_preflight_cli_json_contract()
        assert_container_start_failure_diagnostics()
        assert_container_healthcheck_diagnostics()
        assert_runtime_optional_root_failures_do_not_crash_web_startup()
        assert_web_uses_runtime_config()
        assert_optional_adapter_defaults_are_explicit()
        assert_protocol_order_contract()
        assert_release_safety_audit()
    finally:
        os.environ.clear()
        os.environ.update(old_env)
    print("ok")


if __name__ == "__main__":
    main()
