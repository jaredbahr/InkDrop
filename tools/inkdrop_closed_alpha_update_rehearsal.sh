#!/usr/bin/env bash
set -euo pipefail

: "${PREVIOUS_IMAGE:?Set PREVIOUS_IMAGE to an immutable repository@sha256 digest}"
: "${CURRENT_IMAGE:?Set CURRENT_IMAGE to an immutable repository@sha256 digest}"

for image in "$PREVIOUS_IMAGE" "$CURRENT_IMAGE"; do
  case "$image" in
    *@sha256:????????????????????????????????????????????????????????????????) ;;
    *) echo "both images must be immutable repository@sha256 references" >&2; exit 2 ;;
  esac
done

ROOT="$(mktemp -d /tmp/inkdrop-update-rehearsal.XXXXXX)"
case "$ROOT" in /tmp/inkdrop-update-rehearsal.*) ;; *) exit 3 ;; esac
PROJECT="inkdrop-update-rehearsal-$(printf '%s' "${ROOT##*.}" | tr '[:upper:]' '[:lower:]')"
export COMPOSE_PROJECT_NAME="$PROJECT"

cleanup() {
  cd "$ROOT" 2>/dev/null || return
  docker compose down --remove-orphans >/dev/null 2>&1 || true
  case "$ROOT" in /tmp/inkdrop-update-rehearsal.*) rm -rf -- "$ROOT" ;; esac
}
trap cleanup EXIT

mkdir -p "$ROOT"/{config,state/release,state/backups,state/locks,state/logs,state/cache,state/quarantine,staging,manual-inbox,library/comics,library/manga,rehearsal-backup}
cd "$ROOT"

cat > compose.yaml <<'YAML'
services:
  inkdrop:
    image: ${INKDROP_IMAGE:?}
    environment: &env
      INKDROP_HOST: 0.0.0.0
      INKDROP_PORT: "8796"
      INKDROP_VERSION: ${INKDROP_EXPECTED_VERSION:?}
      INKDROP_COMMIT_SHA: ${INKDROP_EXPECTED_COMMIT:?}
      INKDROP_BUILD_DATE: ${INKDROP_EXPECTED_BUILD_DATE:?}
      INKDROP_RELEASE_CHANNEL: qa
      INKDROP_QA_BUILD_NUMBER: ${INKDROP_EXPECTED_BUILD:?}
      INKDROP_IMAGE_DIGEST: ${INKDROP_IMAGE_DIGEST:?}
      INKDROP_IMAGE_REPOSITORY: ${INKDROP_IMAGE_REPOSITORY:?}
      INKDROP_WORKER_IMAGE_DIGEST: ${INKDROP_IMAGE_DIGEST:?}
      INKDROP_STATE_SCHEMA_VERSION: "17"
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
    volumes: &volumes
      - ./config:/config
      - ./state:/state
      - ./staging:/staging
      - ./manual-inbox:/manual-inbox
      - ./library/comics:/library/comics
      - ./library/manga:/library/manga
      - ./rehearsal-backup:/rehearsal-backup:ro
    healthcheck:
      test: ["CMD", "python", "-B", "core/inkdrop_container_healthcheck.py", "--timeout", "5"]
      interval: 5s
      timeout: 10s
      retries: 18
      start_period: 10s
  inkdrop-worker:
    image: ${INKDROP_IMAGE:?}
    depends_on:
      inkdrop:
        condition: service_healthy
    environment:
      <<: *env
      INKDROP_CONTAINER_SCHEDULER_ENABLED: "1"
    volumes: *volumes
    command: ["python", "-B", "core/inkdrop_container_scheduler.py"]
    healthcheck:
      test: ["CMD", "python", "-B", "core/inkdrop_container_healthcheck.py", "--worker"]
      interval: 5s
      timeout: 10s
      retries: 18
      start_period: 10s
YAML

write_release_identity() {
  local image="$1"
  IMAGE="$image" python3 - <<'PY'
import json, os, subprocess
image = os.environ["IMAGE"]
labels = json.loads(subprocess.check_output(["docker", "image", "inspect", image, "--format", "{{json .Config.Labels}}"], text=True))
repository, digest = image.rsplit("@", 1)
commit = labels["org.opencontainers.image.revision"]
payload = {
    "schema": "inkdrop.qa_candidate.v1", "manifest_schema_version": 1,
    "branch": "qa", "release_channel": "qa", "image_repository": repository,
    "image_digest": digest, "image_tag": f"{repository}:qa-{commit[:12]}",
    "full_commit_sha": commit, "short_sha": commit[:12],
    "version": labels["org.opencontainers.image.version"],
    "build_date": labels["org.opencontainers.image.created"],
    "qa_build_number": int(labels["io.inkdrop.qa.build-number"]),
    "state_schema_version": 17, "workflow_run_id": "rehearsal",
}
open("state/release/qa-candidate.json", "w", encoding="utf-8").write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
release_values = {
    "INKDROP_IMAGE": image,
    "INKDROP_IMAGE_REPOSITORY": repository,
    "INKDROP_IMAGE_DIGEST": digest,
    "INKDROP_EXPECTED_COMMIT": commit,
    "INKDROP_EXPECTED_VERSION": payload["version"],
    "INKDROP_EXPECTED_BUILD_DATE": payload["build_date"],
    "INKDROP_EXPECTED_CHANNEL": payload["release_channel"],
    "INKDROP_EXPECTED_BUILD": str(payload["qa_build_number"]),
    "INKDROP_EXPECTED_SCHEMA": str(payload["state_schema_version"]),
}
existing = open(".env", encoding="utf-8").read().splitlines() if os.path.exists(".env") else []
preserved = [line for line in existing if line.split("=", 1)[0].strip() not in release_values]
open(".env", "w", encoding="utf-8").write("\n".join([*preserved, *(f"{key}={value}" for key, value in release_values.items()), ""]))
PY
}

verify_identity_and_state() {
  docker compose exec -T inkdrop python -B - <<'PY'
import json, os, sqlite3, urllib.request
p = json.load(urllib.request.urlopen("http://127.0.0.1:8796/api/system/version", timeout=10))
assert p["candidate_manifest_status"] == "matched", p
assert p["image_digest"] == os.environ["INKDROP_IMAGE_DIGEST"], p
assert p["commit_sha"] == os.environ["INKDROP_COMMIT_SHA"], p
assert p["version"] == os.environ["INKDROP_VERSION"], p
assert int(p["qa_build_number"]) == int(os.environ["INKDROP_QA_BUILD_NUMBER"]), p
con = sqlite3.connect("/state/inkdrop-state.sqlite3")
assert con.execute("pragma quick_check").fetchone()[0] == "ok"
assert not con.execute("pragma foreign_key_check").fetchall()
assert int(con.execute("select value from schema_meta where key='schema_version'").fetchone()[0]) == 17
assert con.execute("select count(*) from series where id='series:update-rehearsal'").fetchone()[0] == 1
assert con.execute("select value_json from app_settings where key='rehearsal.sentinel'").fetchone()[0] == '"preserved"'
auth = json.load(urllib.request.urlopen("http://127.0.0.1:8796/api/auth/status", timeout=10))
assert ((auth.get("auth") or {}).get("built_in_auth") or {}).get("bootstrap_required") is False, auth
print(json.dumps({"version": p["version"], "commit": p["commit_sha"], "digest": p["image_digest"], "schema": 17, "state": "preserved"}, sort_keys=True))
PY
  docker compose exec -T inkdrop-worker python -B core/inkdrop_container_healthcheck.py --worker --json --wait-seconds 90 </dev/null >/dev/null
  expected="$(docker image inspect "$(docker compose config --images | sort -u)" --format '{{.Id}}')"
  test "$(docker inspect "$(docker compose ps -q inkdrop)" --format '{{.Image}}')" = "$expected"
  test "$(docker inspect "$(docker compose ps -q inkdrop-worker)" --format '{{.Image}}')" = "$expected"
}

docker pull "$PREVIOUS_IMAGE" >/dev/null
docker pull "$CURRENT_IMAGE" >/dev/null
write_release_identity "$PREVIOUS_IMAGE"
cat >> .env <<'ENV'
REHEARSAL_OPERATOR_SENTINEL=preserved
INKDROP_PROWLARR_URL=http://disposable-provider.invalid
INKDROP_PROWLARR_API_KEY=disposable-rehearsal-only
ENV
docker compose config --quiet
docker compose up -d --wait --wait-timeout 120 inkdrop inkdrop-worker

docker compose exec -T inkdrop python -B - <<'PY'
import json, urllib.request
body=json.dumps({"username":"rehearsal-admin","password":"Disposable-Rehearsal-Only-2026!"}).encode()
request=urllib.request.Request("http://127.0.0.1:8796/api/auth/bootstrap", data=body, headers={"Content-Type":"application/json"}, method="POST")
with urllib.request.urlopen(request, timeout=10) as response: assert 200 <= response.status < 300
PY
docker compose exec -T inkdrop python -B - <<'PY'
import sqlite3, time, inkdrop_state
db="/state/inkdrop-state.sqlite3"
inkdrop_state.sync_settings(db, settings=[{"key":"rehearsal.sentinel","value":"preserved","source":"user"}])
with inkdrop_state.connect(db) as con:
    inkdrop_state.init_schema(con)
    now=time.time()
    con.execute("insert into series(id,title,media_type,created_at,updated_at,raw_json) values(?,?,?,?,?,?)", ("series:update-rehearsal","Update Rehearsal","comic",now,now,"{}"))
    con.commit()
PY

docker compose exec -T inkdrop python -B inkdrop_backup_restore.py backup --label update-rehearsal </dev/null > pre-update-backup.json
BACKUP_PATH="$(python3 -c 'import json; print(json.load(open("pre-update-backup.json"))["archive_path"])')"
docker compose exec -T inkdrop sh -c 'cat "$1"' sh "$BACKUP_PATH" </dev/null > rehearsal-backup/pre-update.zip

write_release_identity "$CURRENT_IMAGE"
grep -qx 'REHEARSAL_OPERATOR_SENTINEL=preserved' .env
grep -qx 'INKDROP_PROWLARR_URL=http://disposable-provider.invalid' .env
grep -qx 'INKDROP_PROWLARR_API_KEY=disposable-rehearsal-only' .env
docker compose up -d --force-recreate --wait --wait-timeout 120 inkdrop inkdrop-worker
verify_identity_and_state > update-result.json

docker compose stop inkdrop-worker inkdrop >/dev/null
mv config post-update-config
mv state post-update-state
mkdir -p config state/release state/backups state/locks state/logs state/cache state/quarantine
write_release_identity "$PREVIOUS_IMAGE"
grep -qx 'REHEARSAL_OPERATOR_SENTINEL=preserved' .env
grep -qx 'INKDROP_PROWLARR_URL=http://disposable-provider.invalid' .env
grep -qx 'INKDROP_PROWLARR_API_KEY=disposable-rehearsal-only' .env
docker compose run --rm --no-deps inkdrop python -B inkdrop_backup_restore.py restore /rehearsal-backup/pre-update.zip --target-config-dir /config --target-state-dir /state </dev/null >/dev/null
docker compose run --rm --no-deps inkdrop python -B inkdrop_backup_restore.py restore /rehearsal-backup/pre-update.zip --target-config-dir /config --target-state-dir /state --apply </dev/null >/dev/null
write_release_identity "$PREVIOUS_IMAGE"
docker compose up -d --force-recreate --wait --wait-timeout 120 inkdrop inkdrop-worker
verify_identity_and_state > restore-result.json

python3 - <<'PY'
import json
print(json.dumps({"ok": True, "update": json.load(open("update-result.json")), "matching_backup_restore": json.load(open("restore-result.json"))}, sort_keys=True))
PY
