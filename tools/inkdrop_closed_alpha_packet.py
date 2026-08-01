#!/usr/bin/env python3
"""Build the source-free closed-alpha Compose release asset."""

from __future__ import annotations

import argparse
import io
import json
import re
import zipfile
from datetime import datetime
from pathlib import Path


PACKET_NAME = "inkdrop-closed-alpha"
MAX_PACKET_BYTES = 512 * 1024
PUBLIC_CANDIDATE_KEYS = frozenset({
    "branch",
    "build_date",
    "full_commit_sha",
    "image_digest",
    "image_repository",
    "image_tag",
    "manifest_schema_version",
    "qa_build_number",
    "release_channel",
    "schema",
    "short_sha",
    "state_schema_version",
    "version",
    "workflow_run_id",
})


def load_candidate(path):
    path = Path(path)
    candidate = json.loads(path.read_text(encoding="utf-8"))
    actual_keys = set(candidate)
    if actual_keys != PUBLIC_CANDIDATE_KEYS:
        raise ValueError(
            "candidate keys must match the fixed public schema: "
            f"missing={sorted(PUBLIC_CANDIDATE_KEYS - actual_keys)} "
            f"unknown={sorted(actual_keys - PUBLIC_CANDIDATE_KEYS)}"
        )
    commit = str(candidate.get("full_commit_sha") or "").strip().lower()
    digest = str(candidate.get("image_digest") or "").strip().lower()
    repository = str(candidate.get("image_repository") or "").strip().lower()
    version = str(candidate.get("version") or "").strip()
    build_date = str(candidate.get("build_date") or "").strip()
    image_tag = str(candidate.get("image_tag") or "").strip().lower()
    if candidate.get("schema") != "inkdrop.qa_candidate.v1":
        raise ValueError("unsupported QA candidate schema")
    if candidate.get("manifest_schema_version") != 1:
        raise ValueError("unsupported QA candidate manifest version")
    if candidate.get("branch") != "qa":
        raise ValueError("closed-alpha packet requires the QA branch")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("candidate commit must be an exact SHA")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("candidate image digest must be immutable")
    if not re.fullmatch(r"ghcr\.io/[a-z0-9_.-]+/[a-z0-9_.-]+", repository):
        raise ValueError("candidate image repository must be on GHCR")
    if not re.fullmatch(re.escape(repository) + r":[a-z0-9_.-]+", image_tag):
        raise ValueError("candidate image tag must match its GHCR repository")
    if candidate.get("release_channel") != "qa":
        raise ValueError("closed-alpha packet requires a QA candidate")
    if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.+-]{0,63}", version):
        raise ValueError("candidate version is invalid")
    if int(candidate.get("qa_build_number") or 0) <= 0:
        raise ValueError("candidate build number must be positive")
    try:
        datetime.fromisoformat(build_date[:-1] + "+00:00" if build_date.endswith("Z") else build_date)
    except ValueError as exc:
        raise ValueError("candidate build date is invalid") from exc
    if str(candidate.get("short_sha") or "").lower() != commit[:12]:
        raise ValueError("candidate short SHA does not match the commit")
    if not str(candidate.get("workflow_run_id") or "").isdigit():
        raise ValueError("candidate workflow run id is invalid")
    if int(candidate.get("state_schema_version") or 0) <= 0:
        raise ValueError("candidate state schema version must be positive")
    candidate.update({
        "full_commit_sha": commit,
        "image_digest": digest,
        "image_repository": repository,
        "image_tag": image_tag,
        "version": version,
        "build_date": build_date,
        "qa_build_number": int(candidate["qa_build_number"]),
    })
    return candidate


def public_candidate_bytes(candidate):
    public = {key: candidate[key] for key in sorted(PUBLIC_CANDIDATE_KEYS)}
    return (json.dumps(public, indent=2, sort_keys=True) + "\n").encode("utf-8")


def compose_text():
    return '''services:
  inkdrop:
    image: ${INKDROP_IMAGE:?Set by the release packet}
    restart: unless-stopped
    environment: &inkdrop_environment
      INKDROP_HOST: 0.0.0.0
      INKDROP_PORT: "8796"
      INKDROP_HOST_PORT: "${INKDROP_HOST_PORT:-8796}"
      INKDROP_VERSION: ${INKDROP_EXPECTED_VERSION:?Set by the release packet}
      INKDROP_COMMIT_SHA: ${INKDROP_EXPECTED_COMMIT:?Set by the release packet}
      INKDROP_BUILD_DATE: ${INKDROP_EXPECTED_BUILD_DATE:?Set by the release packet}
      INKDROP_RELEASE_CHANNEL: ${INKDROP_EXPECTED_CHANNEL:?Set by the release packet}
      INKDROP_QA_BUILD_NUMBER: "${INKDROP_EXPECTED_BUILD:?Set by the release packet}"
      INKDROP_IMAGE_DIGEST: ${INKDROP_IMAGE_DIGEST:?Set by the release packet}
      INKDROP_IMAGE_REPOSITORY: ${INKDROP_IMAGE_REPOSITORY:?Set by the release packet}
      INKDROP_WORKER_IMAGE_DIGEST: ${INKDROP_IMAGE_DIGEST:?Set by the release packet}
      INKDROP_STATE_SCHEMA_VERSION: "${INKDROP_EXPECTED_SCHEMA:?Set by the release packet}"
      INKDROP_CANDIDATE_MANIFEST_PATH: /state/release/qa-candidate.json
      INKDROP_CONFIG_DIR: /config
      INKDROP_STATE_DIR: /state
      INKDROP_LOCK_DIR: /state/locks
      INKDROP_LOG_DIR: /state/logs
      INKDROP_CACHE_DIR: /state/cache
      INKDROP_BACKUP_DIR: /state/backups
      INKDROP_STAGING_DIR: /staging
      INKDROP_MANUAL_INBOX_DIR: /manual-inbox
      INKDROP_QUARANTINE_DIR: /state/quarantine
      INKDROP_COMIC_ROOT: /library/comics
      INKDROP_MANGA_ROOT: /library/manga
      INKDROP_CONTAINER_WEB_BASE_URL: http://inkdrop:8796
      INKDROP_AUTH_MODE: built_in
      INKDROP_QUEUE_RUNNER_AUTOPILOT_ENABLED: "0"
      INKDROP_CONTAINER_SCHEDULER_ENABLED: "0"
    ports:
      - "${INKDROP_BIND_ADDRESS:-127.0.0.1}:${INKDROP_HOST_PORT:-8796}:8796"
    volumes: &inkdrop_volumes
      - ./config:/config
      - ./state:/state
      - ./staging:/staging
      - ./manual-inbox:/manual-inbox
      - ./library/comics:/library/comics
      - ./library/manga:/library/manga
    healthcheck:
      test: ["CMD", "python", "-B", "inkdrop_container_healthcheck.py", "--timeout", "5"]
      interval: 10s
      timeout: 10s
      retries: 6
      start_period: 20s

  inkdrop-worker:
    image: ${INKDROP_IMAGE:?Set by the release packet}
    restart: unless-stopped
    depends_on:
      inkdrop:
        condition: service_healthy
    environment:
      <<: *inkdrop_environment
      INKDROP_CONTAINER_SCHEDULER_ENABLED: "1"
    volumes: *inkdrop_volumes
    command: ["python", "-B", "inkdrop_container_scheduler.py"]
    healthcheck:
      test: ["CMD", "python", "-B", "inkdrop_container_healthcheck.py", "--worker"]
      interval: 10s
      timeout: 10s
      retries: 9
      start_period: 30s
'''


def env_text(candidate):
    repository = candidate["image_repository"]
    digest = candidate["image_digest"]
    return "\n".join([
        f"INKDROP_IMAGE={repository}@{digest}",
        f"INKDROP_IMAGE_DIGEST={digest}",
        f"INKDROP_IMAGE_REPOSITORY={repository}",
        f"INKDROP_EXPECTED_VERSION={candidate['version']}",
        f"INKDROP_EXPECTED_COMMIT={candidate['full_commit_sha']}",
        f"INKDROP_EXPECTED_BUILD_DATE={candidate['build_date']}",
        f"INKDROP_EXPECTED_CHANNEL={candidate['release_channel']}",
        f"INKDROP_EXPECTED_BUILD={candidate['qa_build_number']}",
        f"INKDROP_EXPECTED_SCHEMA={candidate['state_schema_version']}",
        "INKDROP_BIND_ADDRESS=127.0.0.1",
        "INKDROP_HOST_PORT=8796",
        "",
    ])


def readme_text(candidate):
    image = f"{candidate['image_repository']}@{candidate['image_digest']}"
    return f'''# InkDrop closed-alpha install packet

This packet runs InkDrop {candidate['version']} from the immutable image
`{image}`. It contains no application source and both services use that same
digest. Keep `.env`, `compose.yaml`, and `state/release/qa-candidate.json`
together.

## Authenticate and install

The package is private. Use a classic GitHub personal access token with only
`read:packages`, owner-granted access to the InkDrop package, and SSO
authorization if the owning organization requires it. Keep the token out of
this directory and shell history:

```bash
mkdir -p config state/release state/backups state/locks state/logs state/cache \
  state/quarantine staging manual-inbox library/comics library/manga
for path in config state staging manual-inbox library/comics library/manga; do
  test "$(stat -c '%u' "$path")" = "$(id -u)"
  test -w "$path"
done
read -rsp 'GitHub package token: ' CR_PAT
printf '\n'
printf '%s' "$CR_PAT" | docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin
unset CR_PAT
docker compose config --quiet
docker compose pull inkdrop inkdrop-worker
docker compose up -d --wait --wait-timeout 120
docker compose ps
```

Create the directories as the account that administers Compose; the ownership
check above deliberately fails on a root-owned copied tree. The current
InkDrop image runs as root inside the container, so retain operator access to
back up or restore root-owned descendants rather than changing the mounts to
world-writable permissions.

Open `http://127.0.0.1:8796`, create the first administrator, and complete the
setup checklist. Change `INKDROP_BIND_ADDRESS` or `INKDROP_HOST_PORT` in `.env`
only when needed. Do not expose the service before administrator bootstrap.

Verify the paired immutable identity:

```bash
EXPECTED_IMAGE="$(docker compose config --images | sort -u)"
test "$(printf '%s\\n' "$EXPECTED_IMAGE" | sed '/^$/d' | wc -l | tr -d ' ')" = 1
test "$(docker inspect "$(docker compose ps -q inkdrop)" --format '{{{{.Config.Image}}}}')" = "$EXPECTED_IMAGE"
test "$(docker inspect "$(docker compose ps -q inkdrop-worker)" --format '{{{{.Config.Image}}}}')" = "$EXPECTED_IMAGE"
EXPECTED_IMAGE_ID="$(docker image inspect "$EXPECTED_IMAGE" --format '{{{{.Id}}}}')"
test "$(docker inspect "$(docker compose ps -q inkdrop)" --format '{{{{.Image}}}}')" = "$EXPECTED_IMAGE_ID"
test "$(docker inspect "$(docker compose ps -q inkdrop-worker)" --format '{{{{.Image}}}}')" = "$EXPECTED_IMAGE_ID"
PUBLISHED_ADDRESS="$(docker compose port inkdrop 8796 | head -n 1)"
test -n "$PUBLISHED_ADDRESS"
curl -fsS "http://$PUBLISHED_ADDRESS/api/system/version" | python -c 'import json,sys; p=json.load(sys.stdin); c=json.load(open("state/release/qa-candidate.json", encoding="utf-8")); assert p["candidate_manifest_status"] == "matched", p; assert p["image_digest"] == c["image_digest"], p; assert p["commit_sha"] == c["full_commit_sha"], p; assert p["version"] == c["version"], p; assert p["release_channel"] == c["release_channel"], p; assert int(p["qa_build_number"]) == int(c["qa_build_number"]), p; assert int(p["state_schema_version"]) == int(c["state_schema_version"]), p; print(p["version"], p["commit_sha"])'
```

## Backup, update, rollback, and restore

Create and verify an application backup before changing any files. The archive
covers config and state, not media:

```bash
docker compose exec -T inkdrop python -B inkdrop_backup_restore.py backup --label pre-update | tee pre-update-backup.json
python -c 'import json,sys; p=json.load(sys.stdin); assert p["ok"] and p["manifest"]["state_db_backup"]["ok"] and p["archive_path"]; print(p["archive_path"])' < pre-update-backup.json
```

For an update, unpack the newer packet to a temporary directory. Keep the
existing install directory as the Compose project directory so its relative
bind paths continue to point at the same `config`, `state`, staging, inbox, and
library locations. Stop both services before copying release-control files.
Do not copy the database or media into the new packet directory.

```bash
INSTALL_ROOT="$(pwd -P)"
NEW_PACKET='/absolute/path/to/new/inkdrop-closed-alpha'
test -f "$NEW_PACKET/compose.yaml"
test -f "$NEW_PACKET/.env"
test -f "$NEW_PACKET/state/release/qa-candidate.json"
docker inspect "$(docker compose ps -q inkdrop)" --format '{{{{range .Mounts}}}}{{{{println .Destination "=" .Source}}}}{{{{end}}}}' > pre-update-mounts.txt
cp "$INSTALL_ROOT/.env" "$INSTALL_ROOT/.env.pre-update"
merge_release_env() {{
  python3 - "$1" "$2" <<'PY'
import os, sys, tempfile
target, packet = sys.argv[1:]
release_keys = {{
    "INKDROP_IMAGE", "INKDROP_IMAGE_REPOSITORY", "INKDROP_IMAGE_DIGEST",
    "INKDROP_EXPECTED_COMMIT", "INKDROP_EXPECTED_VERSION",
    "INKDROP_EXPECTED_BUILD_DATE", "INKDROP_EXPECTED_CHANNEL",
    "INKDROP_EXPECTED_BUILD", "INKDROP_EXPECTED_SCHEMA",
}}
def release_lines(path):
    rows = {{}}
    for line in open(path, encoding="utf-8").read().splitlines():
        key, marker, _value = line.partition("=")
        if marker and key.strip() in release_keys:
            rows[key.strip()] = line
    if set(rows) != release_keys:
        raise SystemExit("packet release identity is incomplete")
    return rows
existing = open(target, encoding="utf-8").read().splitlines()
kept = [line for line in existing if line.partition("=")[0].strip() not in release_keys]
rows = release_lines(packet)
fd, temporary = tempfile.mkstemp(prefix=".env.", dir=os.path.dirname(os.path.abspath(target)), text=True)
try:
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join([*kept, *(rows[key] for key in sorted(release_keys)), ""]))
    os.chmod(temporary, os.stat(target).st_mode)
    os.replace(temporary, target)
finally:
    if os.path.exists(temporary): os.unlink(temporary)
PY
}}
docker compose stop inkdrop-worker inkdrop
cp "$NEW_PACKET/compose.yaml" "$INSTALL_ROOT/compose.yaml"
merge_release_env "$INSTALL_ROOT/.env" "$NEW_PACKET/.env"
mkdir -p "$INSTALL_ROOT/state/release"
cp "$NEW_PACKET/state/release/qa-candidate.json" "$INSTALL_ROOT/state/release/qa-candidate.json"
cd "$INSTALL_ROOT"
docker compose config --quiet
docker compose pull inkdrop inkdrop-worker
docker compose up -d --force-recreate --wait --wait-timeout 120 inkdrop inkdrop-worker
```

Verify both services, candidate identity, and database integrity/schema before
considering the update successful:

```bash
EXPECTED_IMAGE="$(docker compose config --images | sort -u)"
test "$(printf '%s\\n' "$EXPECTED_IMAGE" | sed '/^$/d' | wc -l | tr -d ' ')" = 1
test "$(docker inspect "$(docker compose ps -q inkdrop)" --format '{{{{.Config.Image}}}}')" = "$EXPECTED_IMAGE"
test "$(docker inspect "$(docker compose ps -q inkdrop-worker)" --format '{{{{.Config.Image}}}}')" = "$EXPECTED_IMAGE"
EXPECTED_IMAGE_ID="$(docker image inspect "$EXPECTED_IMAGE" --format '{{{{.Id}}}}')"
test "$(docker inspect "$(docker compose ps -q inkdrop)" --format '{{{{.Image}}}}')" = "$EXPECTED_IMAGE_ID"
test "$(docker inspect "$(docker compose ps -q inkdrop-worker)" --format '{{{{.Image}}}}')" = "$EXPECTED_IMAGE_ID"
PUBLISHED_ADDRESS="$(docker compose port inkdrop 8796 | head -n 1)"
curl -fsS "http://$PUBLISHED_ADDRESS/api/system/version" | python -c 'import json,sys; p=json.load(sys.stdin); c=json.load(open("state/release/qa-candidate.json", encoding="utf-8")); assert p["candidate_manifest_status"] == "matched", p; assert p["image_digest"] == c["image_digest"], p; assert p["commit_sha"] == c["full_commit_sha"], p; assert p["version"] == c["version"], p; assert p["release_channel"] == c["release_channel"], p; assert int(p["qa_build_number"]) == int(c["qa_build_number"]), p; assert int(p["state_schema_version"]) == int(c["state_schema_version"]), p; print(p["version"], p["commit_sha"])'
docker compose exec -T inkdrop python -B -c 'import sqlite3,inkdrop_runtime_config,inkdrop_state; c=sqlite3.connect(inkdrop_runtime_config.state_db_path()); actual=int(c.execute("select value from schema_meta where key=?", ("schema_version",)).fetchone()[0]); assert c.execute("pragma quick_check").fetchone()[0] == "ok"; assert not c.execute("pragma foreign_key_check").fetchall(); assert actual == inkdrop_state.SCHEMA_VERSION, (actual, inkdrop_state.SCHEMA_VERSION); print(actual)'
docker compose exec -T inkdrop-worker python -B inkdrop_container_healthcheck.py --worker --json --wait-seconds 90
```

For rollback without a database restore, first confirm the prior image supports
the current state schema. Stop both services, restore the prior packet's
`compose.yaml` and candidate manifest, and merge only its release-identity keys
into the existing `.env`; operator settings and credentials remain untouched.
If this is a new shell, redefine `merge_release_env` from the update block.
Then pull and recreate both services together. Do not move or copy media.

```bash
INSTALL_ROOT="$(pwd -P)"
PRIOR_PACKET='/absolute/path/to/prior/inkdrop-closed-alpha'
docker compose stop inkdrop-worker inkdrop
cp "$PRIOR_PACKET/compose.yaml" "$INSTALL_ROOT/compose.yaml"
type merge_release_env >/dev/null
merge_release_env "$INSTALL_ROOT/.env" "$PRIOR_PACKET/.env"
cp "$PRIOR_PACKET/state/release/qa-candidate.json" "$INSTALL_ROOT/state/release/qa-candidate.json"
cd "$INSTALL_ROOT"
docker compose config --quiet
docker compose pull inkdrop inkdrop-worker
docker compose up -d --force-recreate --wait --wait-timeout 120 inkdrop inkdrop-worker
```

If the update migrated state or the prior image does not support the current
schema, use the matching pre-update backup instead. Stop both services, restore
the exact prior Compose and candidate identity while preserving operator `.env`
settings, then preview and apply that backup with the prior image before
starting either service:

```bash
docker compose stop inkdrop-worker inkdrop
cp "$PRIOR_PACKET/compose.yaml" "$INSTALL_ROOT/compose.yaml"
type merge_release_env >/dev/null
merge_release_env "$INSTALL_ROOT/.env" "$PRIOR_PACKET/.env"
cp "$PRIOR_PACKET/state/release/qa-candidate.json" "$INSTALL_ROOT/state/release/qa-candidate.json"
cd "$INSTALL_ROOT"
docker compose config --quiet
docker compose pull inkdrop inkdrop-worker
docker compose run --rm --no-deps inkdrop python -B inkdrop_backup_restore.py restore \\
  /state/backups/inkdrop-backup-YYYYMMDD-HHMMSS-label.zip \\
  --target-config-dir /config --target-state-dir /state
docker compose run --rm --no-deps inkdrop python -B inkdrop_backup_restore.py restore \\
  /state/backups/inkdrop-backup-YYYYMMDD-HHMMSS-label.zip \\
  --target-config-dir /config --target-state-dir /state --apply
docker compose up -d --force-recreate --wait --wait-timeout 120 inkdrop inkdrop-worker
```

Never run `docker compose down -v` against an install you intend to retain.
For support, share the candidate manifest and redacted diagnostics, never the
token, `.env` additions containing secrets, raw database, or media.
'''


def _entry(name, data):
    info = zipfile.ZipInfo(f"{PACKET_NAME}/{name}", (1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    return info, data.encode("utf-8") if isinstance(data, str) else data


def build_packet_bytes(candidate_path):
    candidate = load_candidate(candidate_path)
    entries = (
        _entry("compose.yaml", compose_text()),
        _entry(".env", env_text(candidate)),
        _entry("README.md", readme_text(candidate)),
        _entry("state/release/qa-candidate.json", public_candidate_bytes(candidate)),
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for info, data in entries:
            archive.writestr(info, data)
    payload = output.getvalue()
    if len(payload) > MAX_PACKET_BYTES:
        raise ValueError("closed-alpha packet exceeds size bound")
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--output", default="inkdrop-closed-alpha-compose.zip")
    args = parser.parse_args(argv)
    payload = build_packet_bytes(args.candidate)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(payload)
    print(json.dumps({"ok": True, "output": str(output), "bytes": len(payload)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
