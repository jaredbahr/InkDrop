# InkDrop Docker-First Install

This is the closed-alpha install guide. Invited testers use the
source-free `inkdrop-closed-alpha-compose.zip` attached to the approved
candidate. Its `.env` pins one immutable GHCR digest, and both web and worker
use that exact image. Do not test a moving tag or a source checkout as though it
were the released candidate.

## Closed-alpha candidate install

The packet README is the supported procedure. It includes private-package
authentication, the exact repository and digest, persistent mounts, directory
creation and ownership, rendered Compose validation, first-administrator
bootstrap, and version/commit/build/schema/digest checks. In outline:

```bash
unzip inkdrop-closed-alpha-compose.zip
cd inkdrop-closed-alpha
mkdir -p config state/release state/backups state/locks state/logs state/cache \
  state/quarantine staging manual-inbox library/comics library/manga
for path in config state staging manual-inbox library/comics library/manga; do
  test "$(stat -c '%u' "$path")" = "$(id -u)" && test -w "$path"
done
docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin
docker compose config --quiet
test "$(docker compose config --images | sort -u | sed '/^$/d' | wc -l | tr -d ' ')" = 1
docker compose pull inkdrop inkdrop-worker
docker compose up -d --wait --wait-timeout 120
```

Use a package token with `read:packages` only and supply it out of band; never
put it in `.env`, the packet, support output, or shell transcripts. Follow the
packet's identity commands before entering any settings. A healthy container
does not by itself prove candidate identity or workflow readiness.

Create the mount directories as the account that administers Compose. The
current image runs as root inside the container and may create root-owned
descendants; preserve administrator backup/restore access and do not use
blanket `0777` permissions.

## Development source build

The repository Compose file builds the current checkout. This path is for
development and contributor testing, not closed-alpha candidate evaluation.

1. Optional: copy `.env.example` to `.env` if you want to configure provider
   endpoints, credentials, paths, or automation knobs before first start.
2. Keep optional adapter secrets blank unless you already run that service.
3. Start InkDrop:

```bash
docker compose up -d --build
```

4. Open `http://localhost:8796`.

A `.env` file is not required for a clean local-folder/manual-inbox startup;
Docker Compose uses safe defaults from `docker-compose.yml` when `.env` is
absent. This is different from the closed-alpha packet, which ships its own
`.env` and `compose.yaml` with release identity already pinned.
For a development server install, keep InkDrop in its own directory instead of
inside an existing Arr stack directory:

```bash
mkdir -p /opt/inkdrop
cd /opt/inkdrop
git clone https://github.com/jaredbahr/InkDrop .
docker compose up -d --build
```

The default Compose file then creates the runtime folders beside the checkout:

- `./config` -> `/config`
- `./state` -> `/state`
- `./staging` -> `/staging`
- `./manual-inbox` -> `/manual-inbox`
- `./library` -> `/library`

That layout is intentionally independent from `arr-docker`, `docker-arrs`, or
any other existing media stack. Attach InkDrop to an existing Docker network or
set adapter URLs only when you want it to talk to those services.
If InkDrop should stay as its own Compose project but also reach services on an
existing Docker network, use the optional `compose.network.example.yml`
override after setting `INKDROP_EXTERNAL_NETWORK` to an existing network name:

```bash
docker compose -f docker-compose.yml -f compose.network.example.yml up -d --build
```

This attaches only the `inkdrop` web container to that network. The repository
worker still uses the default project network unless you add a matching worker
override. The network override does not turn Prowlarr, SABnzbd, qBittorrent,
SLSKD, Kavita, Komga, Kapowarr, or Suwayomi into active config; set those
adapter URLs separately when you choose to enable them.
The public smoke suite treats `.env.example` and `docker-compose.yml` as one
contract: every documented `INKDROP_*` setting must be passed into the
container, and every Compose `INKDROP_*` setting must be documented in the
example file.
The public Compose service also avoids host networking, privileged mode, host
PID/IPC namespace sharing, host devices, and added Linux capabilities. InkDrop
should reach download clients, readers, and source providers over normal
service URLs or mounted folders instead of broad host access.

If you adapt the Compose file for an existing production stack and keep a fixed
local image tag such as `image: inkdrop:local`, keep a matching `build:` block
with the InkDrop source checkout as its context. Without that build context,
`docker compose build inkdrop` has no source to rebuild and can leave the
running container on an older image than the checkout you are testing.

## First-Run Checklist

Required for a clean Docker start:

- Run `docker compose up -d --build`.
- Open `http://localhost:8796`.
- Check `docker compose ps` for container health.

Optional before enabling automation:

- Set `INKDROP_COMICVINE_API_KEY` for comic metadata lookup.
- Confirm at least one writable comic or manga library root. A mounted parent
  directory does not make missing `/library/comics` or `/library/manga`
  subdirectories ready for import.
- Set Prowlarr, SABnzbd, qBittorrent, slskd, or Suwayomi URLs and credentials
  only for services you actually run.
- For SLSKD, InkDrop can read an API key from `INKDROP_SLSKD_API_KEY` or from
  the mounted `INKDROP_SLSKD_CONFIG`. Optional SLSKD search-history cleanup is
  off by default; enable `Delete SLSKD Search History` in Settings or set
  `INKDROP_SLSKD_SEARCH_HISTORY_CLEANUP_ENABLED=true` to let the Docker worker
  periodically delete old completed SLSKD search rows while keeping the newest
  configured entries. Active searches are not deleted.
- Settings > Automation exposes **SLSKD Active Stall Threshold** in minutes.
  It applies only to an active transfer that still has zero progress; normal
  SLSKD queued/waiting states retain their separate queue policy. The default
  is 45 minutes and the accepted range is 5-1440 minutes. When crossed, InkDrop
  records the failed attempt, retires/cancels that candidate safely, and allows
  the existing bounded failover path to try the next candidate. Existing
  `INKDROP_SLSKD_ZERO_PROGRESS_STALE_SECONDS` overrides remain effective until
  an operator explicitly saves the Settings value.
- Configure `INKDROP_SAB_PATH_MAPPINGS` or `INKDROP_UNC_PATH_MAPPINGS` only
  when download-client paths differ from InkDrop container paths.
- Set `INKDROP_KAPOWARR_DB` or `INKDROP_KAVITA_DB` only for deliberate
  migration/visibility compatibility.
- Monitoring a series does not enable Automatic Search. Set
  `INKDROP_QUEUE_RUNNER_AUTOPILOT_ENABLED=1` in the packet `.env`, then run
  `docker compose up -d --force-recreate inkdrop inkdrop-worker`. Confirm the
  UI reports Automatic Search enabled, worker healthy, scheduler active, and
  either running or idle.

Expected clean-install warnings:

- `optional_adapters_unconfigured` is normal until providers are configured.
- Kapowarr and Kavita are migration/visibility adapters, not first-run
  requirements.
- Missing or unwritable library roots block import readiness even when the web
  and worker containers are healthy.
- Direct host runs may report `python_dependencies_missing` or
  `runtime_tools_missing`; the Docker image should keep those strict checks
  green.

## Preflight

Before starting the web UI, you can verify runtime folders and optional adapter
configuration:

```bash
docker compose run --rm inkdrop python -B inkdrop_preflight.py --create --json
```

The preflight creates/checks the configured runtime folders, verifies they are
writable, reports the state database path, reports required Python package
availability, and lists optional adapters that are not configured yet. Missing
optional adapters are warnings, not startup failures; local-folder/manual-inbox
mode should still be able to start.
The JSON output includes `warning_summary`, a stable machine-readable grouping
for expected clean-install warnings:

- `optional_adapters_unconfigured`: optional services left blank
- `runtime_tools_missing`: archive/runtime tools not found in the current host
  or image
- `python_dependencies_missing`: required Python packages missing from the
  current host environment

Kapowarr and Kavita are intentionally not clean-install warnings; they are
adapter paths only, not first-run requirements.

The same output also reports archive-tool availability. `7z` is the primary
tool InkDrop uses for CBR/RAR inspection and pack extraction; `unrar-free` (or
another `INKDROP_UNRAR_PATH` override) is a fallback. The provided Dockerfile
installs archive tooling, but custom images should keep those checks green
before enabling automatic imports.

The container startup command runs preflight with `--create`,
`--strict-dependencies`, and `--strict-runtime-tools` before launching the web
UI. Docker healthchecks run `inkdrop_container_healthcheck.py` after startup;
that repeats the same strict preflight and probes `/status.json` on the
configured `INKDROP_PORT`. That makes missing Python runtime packages, missing
archive tools, unwritable required folders, invalid bind settings, and a
non-responsive web process show up in container logs and health status instead
of becoming a later operator mystery.
If startup preflight fails, the container prints the redacted JSON preflight
payload to stderr so errors, warnings, effective paths, and `<set>` / `<unset>`
secret states are visible in `docker compose logs inkdrop`. Direct source
checkouts can run `pip install -r requirements.txt` before using strict mode.

The JSON preflight output includes `effective_config`, a redacted snapshot of
the runtime roots and relevant `INKDROP_*` environment values. Secret-looking
keys report only `<set>` or `<unset>`, so the output is suitable for debugging
install problems without pasting API keys or passwords.
The `preflight_schema_version` field marks the machine-readable diagnostic
contract for scripts, bots, and support requests.
Username fields and credentials embedded in service URLs are also redacted in
this diagnostic block. Secret-looking URL query parameters such as `apikey`,
`token`, `password`, or `secret` are redacted as well, while harmless query
parameters remain visible for troubleshooting.
It also includes a `web` block showing the bind host/port plus the callback
base URL InkDrop workers will use. Compose supplies
`INKDROP_CONTAINER_WEB_BASE_URL` with the `inkdrop` service DNS name; an
explicit container value takes precedence over `INKDROP_WEB_BASE_URL`.
Standalone/local commands with neither value keep the loopback fallback on the
configured internal port.
Path mapping settings such as `INKDROP_SAB_PATH_MAPPINGS` and
`INKDROP_UNC_PATH_MAPPINGS` are also validated up front as comma-separated
`source=target` pairs, so malformed mappings fail preflight before imports or
download-client reconciliation runs.

Preflight also validates basic web bind settings. `INKDROP_HOST` must be a bind
host/address such as `0.0.0.0`, not a URL. `INKDROP_HOST_PORT` and
`INKDROP_PORT` must be integers from `1` through `65535`; invalid values fail
preflight before the web server tries to bind a socket.

For a normal Docker install, change only `INKDROP_HOST_PORT` in `.env`. It is
the browser-facing port published by Docker; `INKDROP_PORT` is the internal
container listener and should normally remain `8796`. Existing installs that
set only `INKDROP_PORT` remain compatible: the host port falls back to that
value. Apply a port change by recreating both services:

```bash
# .env
INKDROP_HOST_PORT=9876
INKDROP_PORT=8796

docker compose config
docker compose up -d --force-recreate inkdrop inkdrop-worker
docker compose port inkdrop 8796
```

Then open `http://localhost:9876`. System > Settings > General reports the
effective host and container ports read-only. A settings/database save cannot
rebind a running process, so the recreate step is required.
If external workers need to call back into InkDrop, set `INKDROP_WEB_BASE_URL`
to the full operator-facing `http://` or `https://` URL and keep it aligned with
any custom port or reverse proxy route.

## Docker Health Troubleshooting

After startup, check Docker health with:

```bash
docker compose ps
docker inspect "$(docker compose ps -q inkdrop)" --format '{{json .State.Health}}'
docker compose logs inkdrop
docker compose exec inkdrop python -B inkdrop_container_healthcheck.py --json
docker compose exec inkdrop python -B inkdrop_container_healthcheck.py --json --wait-seconds 60
docker compose exec inkdrop-worker python -B inkdrop_container_healthcheck.py --worker --json
```

The healthcheck prints JSON. A failure with `phase=preflight` means
configuration, required Python packages, archive tools, or writable runtime
folders are failing before the web probe runs. A failure with `phase=http`
means strict preflight passed but `/status.json` did not answer on the
configured `INKDROP_PORT`; check the web startup log, port mapping, and whether
the container is still inside Docker's healthcheck start period.
Use `--wait-seconds` only for manual troubleshooting when startup is slow or a
large state database makes the first `/status.json` response late; Docker's
configured healthcheck intentionally keeps a short probe and relies on
`start_period` plus retries.
The worker healthcheck reads the scheduler heartbeat from persistent state.
`phase=worker` with `worker_state=unavailable` means the scheduler heartbeat is
missing or stale. `worker_state=degraded` reports failed optional/provider jobs
without failing Docker health; repeated critical failures, a critically late
job, or a hung critical job makes the worker unhealthy.

## Backup And Restore

InkDrop includes a small config/state backup helper for public installs:

```bash
docker compose exec inkdrop python -B inkdrop_backup_restore.py backup
```

The archive includes a SQLite state backup, a redacted config/settings export,
a secret-reference manifest without secret values, and a restore manifest. It
does not include user media, staging downloads, reader databases, or external
service state.

Preview a restore into new roots before writing files:

```bash
docker compose exec inkdrop python -B inkdrop_backup_restore.py restore /state/backups/inkdrop-backup-YYYYMMDD-HHMMSS-manual.zip --target-config-dir /config-restore --target-state-dir /state-restore
```

Add `--apply` only after reviewing path warnings. Restored path values that do
not exist on the new host are reported for remapping instead of silently
rewritten.

Backup archives are created with mode `0600` inside a `0700` backup directory.
Restore targets are different: the current closed-alpha helper writes restored
database and redacted config files using the container process ownership and
normal file-creation mode, which can result in mode `0644`. Keep `/config` and
`/state` on operator-controlled storage, and inspect ownership and permissions
after a restore:

```bash
docker compose exec inkdrop stat -c '%n %a %U:%G' /state/inkdrop-state.sqlite3 /config/inkdrop-config-export.json /config/inkdrop-secret-refs.json
```

Restrict the parent directories or adjust ownership/mode for the account that
runs InkDrop if other host users must not read restored state. This documents
the current behavior; it is not a promise that restore output is private merely
because its source archive was private.

## Upgrade And Image-Version Rollback

Use immutable image digests for an image-based install. A moving tag such as
`latest`, `qa`, or an alpha version tag is useful for discovery, but it is not
an exact upgrade or rollback target. The repository Compose file is build-first,
so create this local override once to make both the web and worker use the same
required image reference:

```bash
cat > compose.image.yml <<'YAML'
services:
  inkdrop:
    image: ${INKDROP_IMAGE:?Set INKDROP_IMAGE to a pinned tag or digest}
    build: null
    environment:
      INKDROP_VERSION: ${INKDROP_EXPECTED_VERSION:?Set the version from the image label}
      INKDROP_COMMIT_SHA: ${INKDROP_EXPECTED_COMMIT:?Set the commit from the image label}
      INKDROP_BUILD_DATE: ${INKDROP_EXPECTED_BUILD_DATE:?Set the build date from the image label}
      INKDROP_RELEASE_CHANNEL: ${INKDROP_EXPECTED_CHANNEL:?Set the release channel from the image label}
      INKDROP_QA_BUILD_NUMBER: ${INKDROP_EXPECTED_BUILD:?Set the build number from the image label}
      INKDROP_CONTAINER_SCHEDULER_ENABLED: "0"
  inkdrop-worker:
    image: ${INKDROP_IMAGE:?Set INKDROP_IMAGE to a pinned tag or digest}
    build: null
    environment:
      INKDROP_VERSION: ${INKDROP_EXPECTED_VERSION:?Set the version from the image label}
      INKDROP_COMMIT_SHA: ${INKDROP_EXPECTED_COMMIT:?Set the commit from the image label}
      INKDROP_BUILD_DATE: ${INKDROP_EXPECTED_BUILD_DATE:?Set the build date from the image label}
      INKDROP_RELEASE_CHANNEL: ${INKDROP_EXPECTED_CHANNEL:?Set the release channel from the image label}
      INKDROP_QA_BUILD_NUMBER: ${INKDROP_EXPECTED_BUILD:?Set the build number from the image label}
      INKDROP_CONTAINER_SCHEDULER_ENABLED: "1"
YAML
```

The explicit identity environment above is required because the base Compose
file supplies development defaults (`dev`, `unknown`, and `0`) that otherwise
override the identity baked into a published image. Do not remove these
required substitutions or replace them with defaults. The procedure below
reads the immutable OCI labels directly and proves that the API values match
them, so Compose cannot silently mask a different image identity.

The following sequence is intentionally explicit. Run it from the InkDrop
checkout that owns the existing `./config` and `./state` mounts.

### System update awareness

System > Updates is informational. InkDrop never mounts or calls the Docker
socket and does not expose an Install button. A trusted external manager such
as Dockhand, Docker Compose, or Portainer remains responsible for pulling and
recreating both `inkdrop` and `inkdrop-worker` together.

After validating a QA image, the release workflow publishes
`inkdrop-update-manifest.json` beside the existing candidate and image
validation evidence. System > Updates performs no check during startup or
normal first paint. When explicitly opened, it may read the approved GitHub
release asset with bounded connection and total timeouts, a 64-KiB response
limit, and a one-hour cache. Concurrent or repeated page requests share that
cache; timeout and availability failures use the last validated result when
one exists. No credentials, cookies, or authorization headers are sent.

An external updater may copy the asset to
`/state/release/latest-update.json` (or pass `INKDROP_UPDATE_MANIFEST_PATH` to
the web container for a different mounted file). To let InkDrop check an exact
release asset when the Updates page opens, pass `INKDROP_UPDATE_MANIFEST_URL`
to the web container with that asset's approved InkDrop GitHub URL; unset it or
pass `INKDROP_UPDATE_REMOTE_ENABLED=0` for local-only operation. Only the exact
`ghcr.io/jaredbahr/inkdrop@sha256:...` image identity is accepted.

If the manager can report the worker identity, pass
`INKDROP_WORKER_IMAGE_DIGEST=sha256:...` on the web service. The closed-alpha
packet already does this. System > Updates will fail closed when it differs
from the web candidate digest. Missing, stale, malformed, future-schema,
mutable-tag, or wrong-repository manifests produce guidance only; they never
interrupt startup, scheduling, or acquisition.

1. Record the currently running image reference and create a pre-upgrade backup.
   Do not continue unless the recorded image is the exact digest you intend to
   use for rollback.

```bash
export INKDROP_PREVIOUS_IMAGE="$(docker inspect "$(docker compose -f docker-compose.yml ps -q inkdrop)" --format '{{.Config.Image}}')"
case "$INKDROP_PREVIOUS_IMAGE" in *@sha256:*) ;; *) echo 'Current image is not digest-pinned; resolve and record its RepoDigest before upgrading.' >&2; exit 1;; esac
printf '%s\n' "$INKDROP_PREVIOUS_IMAGE" > .inkdrop-previous-image
export INKDROP_IMAGE="$INKDROP_PREVIOUS_IMAGE"
export COMPOSE_FILE='docker-compose.yml:compose.image.yml'
export INKDROP_EXPECTED_VERSION="$(docker image inspect "$INKDROP_IMAGE" --format '{{index .Config.Labels "org.opencontainers.image.version"}}')"
export INKDROP_EXPECTED_COMMIT="$(docker image inspect "$INKDROP_IMAGE" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')"
export INKDROP_EXPECTED_BUILD_DATE="$(docker image inspect "$INKDROP_IMAGE" --format '{{index .Config.Labels "org.opencontainers.image.created"}}')"
export INKDROP_EXPECTED_CHANNEL="$(docker image inspect "$INKDROP_IMAGE" --format '{{index .Config.Labels "io.inkdrop.release.channel"}}')"
export INKDROP_EXPECTED_BUILD="$(docker image inspect "$INKDROP_IMAGE" --format '{{index .Config.Labels "io.inkdrop.qa.build-number"}}')"
docker compose exec inkdrop python -B inkdrop_backup_restore.py backup --label pre-upgrade
```

2. Set the new immutable image plus its expected public identity, pull it, and
   replace both containers without changing or deleting the mounted directories.

```bash
export INKDROP_IMAGE='ghcr.io/jaredbahr/inkdrop@sha256:<new-image-digest>'
export INKDROP_EXPECTED_VERSION='<exact-version-label>'
export INKDROP_EXPECTED_COMMIT='<full-40-character-commit-sha>'
export INKDROP_EXPECTED_BUILD_DATE='<exact-created-label>'
export INKDROP_EXPECTED_CHANNEL='<exact-release-channel-label>'
export INKDROP_EXPECTED_BUILD='<qa-build-number>'
docker compose pull inkdrop inkdrop-worker
test "$(docker image inspect "$INKDROP_IMAGE" --format '{{index .Config.Labels "org.opencontainers.image.version"}}')" = "$INKDROP_EXPECTED_VERSION"
test "$(docker image inspect "$INKDROP_IMAGE" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" = "$INKDROP_EXPECTED_COMMIT"
test "$(docker image inspect "$INKDROP_IMAGE" --format '{{index .Config.Labels "org.opencontainers.image.created"}}')" = "$INKDROP_EXPECTED_BUILD_DATE"
test "$(docker image inspect "$INKDROP_IMAGE" --format '{{index .Config.Labels "io.inkdrop.release.channel"}}')" = "$INKDROP_EXPECTED_CHANNEL"
test "$(docker image inspect "$INKDROP_IMAGE" --format '{{index .Config.Labels "io.inkdrop.qa.build-number"}}')" = "$INKDROP_EXPECTED_BUILD"
docker compose stop inkdrop-worker
docker compose up -d --no-deps --force-recreate inkdrop
```

The web is staged by itself with `INKDROP_CONTAINER_SCHEDULER_ENABLED=0`; no
worker automation is allowed to touch the mounted state during acceptance.

3. Resolve the effective container and published ports from the staged Compose
   container, wait at most two minutes for the version endpoint, then verify the
   exact image, API identity, web health, and database schema/integrity. Only
   after all checks pass should the worker be created and its bounded health
   check pass.

```bash
test "$(docker inspect "$(docker compose ps -q inkdrop)" --format '{{.Config.Image}}')" = "$INKDROP_IMAGE"
INKDROP_CONTAINER_PORT="$(docker inspect "$(docker compose ps -q inkdrop)" --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^INKDROP_PORT=//p')"
INKDROP_PUBLISHED_ADDRESS="$(docker compose port inkdrop "$INKDROP_CONTAINER_PORT" | head -n 1)"
INKDROP_EFFECTIVE_PORT="${INKDROP_PUBLISHED_ADDRESS##*:}"
VERSION_FILE="$(mktemp)"
INKDROP_READY=0
for attempt in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${INKDROP_EFFECTIVE_PORT}/api/system/version" -o "$VERSION_FILE"; then INKDROP_READY=1; break; fi
  sleep 2
done
test "$INKDROP_READY" = 1
VERSION_JSON="$(cat "$VERSION_FILE")"
rm -f "$VERSION_FILE"
printf '%s\n' "$VERSION_JSON" | python -c 'import json,sys; p=json.load(sys.stdin); assert p["version"] == sys.argv[1], p; assert p["commit_sha"] == sys.argv[2], p; assert str(p["qa_build_number"]) == sys.argv[3], p; print(json.dumps({"commit_sha": p["commit_sha"], "qa_build_number": p["qa_build_number"], "version": p["version"]}, sort_keys=True))' "$INKDROP_EXPECTED_VERSION" "$INKDROP_EXPECTED_COMMIT" "$INKDROP_EXPECTED_BUILD"
docker compose exec inkdrop python -B inkdrop_container_healthcheck.py --json --wait-seconds 60
docker compose exec inkdrop python -B -c 'import json,sqlite3,inkdrop_runtime_config,inkdrop_state; c=sqlite3.connect(inkdrop_runtime_config.state_db_path()); actual=int(c.execute("select value from schema_meta where key=\"schema_version\"").fetchone()[0]); assert c.execute("pragma quick_check").fetchone()[0] == "ok"; assert not c.execute("pragma foreign_key_check").fetchall(); assert actual == inkdrop_state.SCHEMA_VERSION, (actual, inkdrop_state.SCHEMA_VERSION); print(json.dumps({"schema_version": actual, "quick_check": "ok", "foreign_key_violations": 0}))'
docker compose up -d --no-deps --force-recreate inkdrop-worker
test "$(docker inspect "$(docker compose ps -q inkdrop-worker)" --format '{{.Config.Image}}')" = "$INKDROP_IMAGE"
docker compose exec inkdrop-worker python -B inkdrop_container_healthcheck.py --worker --json --wait-seconds 90
docker compose ps
```

For a standalone Compose install, `/api/system/version` can legitimately show
`image_digest=""` and `candidate_manifest_status="missing"`. The candidate
manifest is a deployment artifact, not part of a bare image volume, and Docker
does not automatically inject its resolved digest into the container. In that
case the two `docker inspect` comparisons above prove the digest pin, while the
API `commit_sha`, `qa_build_number`, version, and OCI labels prove build
identity. Do not treat an empty API digest as permission to use a moving tag.

If verification fails, roll both services back to the saved image. Normal image
rollback reuses the existing mounts and does not require a state restore:

```bash
export INKDROP_IMAGE="$(cat .inkdrop-previous-image)"
docker compose pull inkdrop inkdrop-worker
export INKDROP_EXPECTED_VERSION="$(docker image inspect "$INKDROP_IMAGE" --format '{{index .Config.Labels "org.opencontainers.image.version"}}')"
export INKDROP_EXPECTED_COMMIT="$(docker image inspect "$INKDROP_IMAGE" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')"
export INKDROP_EXPECTED_BUILD_DATE="$(docker image inspect "$INKDROP_IMAGE" --format '{{index .Config.Labels "org.opencontainers.image.created"}}')"
export INKDROP_EXPECTED_CHANNEL="$(docker image inspect "$INKDROP_IMAGE" --format '{{index .Config.Labels "io.inkdrop.release.channel"}}')"
export INKDROP_EXPECTED_BUILD="$(docker image inspect "$INKDROP_IMAGE" --format '{{index .Config.Labels "io.inkdrop.qa.build-number"}}')"
docker compose stop inkdrop-worker
docker compose up -d --no-deps --force-recreate inkdrop
test "$(docker inspect "$(docker compose ps -q inkdrop)" --format '{{.Config.Image}}')" = "$INKDROP_IMAGE"
INKDROP_CONTAINER_PORT="$(docker inspect "$(docker compose ps -q inkdrop)" --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^INKDROP_PORT=//p')"
INKDROP_PUBLISHED_ADDRESS="$(docker compose port inkdrop "$INKDROP_CONTAINER_PORT" | head -n 1)"
INKDROP_EFFECTIVE_PORT="${INKDROP_PUBLISHED_ADDRESS##*:}"
rm -f /tmp/inkdrop-version.json
for attempt in $(seq 1 60); do curl -fsS "http://127.0.0.1:${INKDROP_EFFECTIVE_PORT}/api/system/version" -o /tmp/inkdrop-version.json && break; sleep 2; done
test -s /tmp/inkdrop-version.json
python -c 'import json,sys; p=json.load(open(sys.argv[1])); assert p["version"] == sys.argv[2], p; assert p["commit_sha"] == sys.argv[3], p; assert str(p["qa_build_number"]) == sys.argv[4], p' /tmp/inkdrop-version.json "$INKDROP_EXPECTED_VERSION" "$INKDROP_EXPECTED_COMMIT" "$INKDROP_EXPECTED_BUILD"
rm -f /tmp/inkdrop-version.json
docker compose exec inkdrop python -B inkdrop_container_healthcheck.py --json --wait-seconds 60
docker compose exec inkdrop python -B -c 'import json,sqlite3,inkdrop_runtime_config,inkdrop_state; c=sqlite3.connect(inkdrop_runtime_config.state_db_path()); actual=int(c.execute("select value from schema_meta where key=\"schema_version\"").fetchone()[0]); assert c.execute("pragma quick_check").fetchone()[0] == "ok"; assert not c.execute("pragma foreign_key_check").fetchall(); assert actual == inkdrop_state.SCHEMA_VERSION, (actual, inkdrop_state.SCHEMA_VERSION); print(json.dumps({"schema_version": actual, "quick_check": "ok", "foreign_key_violations": 0}))'
docker compose up -d --no-deps --force-recreate inkdrop-worker
test "$(docker inspect "$(docker compose ps -q inkdrop-worker)" --format '{{.Config.Image}}')" = "$INKDROP_IMAGE"
docker compose exec inkdrop-worker python -B inkdrop_container_healthcheck.py --worker --json --wait-seconds 90
docker compose ps
```

Restore the pre-upgrade state only when the previous image cannot safely read
the current schema or integrity verification fails. First stop both services,
preserve the current failed state for diagnosis, preview the archive into new
roots, and review every path warning. Apply to the live mounts only after that
preview is accepted:

`restore --apply` snapshots the state DB and config files it's about to
overwrite into the backups directory automatically, but that covers only
what the archive itself restores. It never touches the rest of `./state`
(`imported-files.sqlite3`, `pending-pack-imports.jsonl`, and anything else
InkDrop writes there at runtime), so the manual `cp -a` below is still the
only full-tree safety net -- keep doing it:

```bash
docker compose stop inkdrop-worker inkdrop
cp -a ./state "./state.before-restore-$(date +%Y%m%d-%H%M%S)"
docker compose run --rm --no-deps inkdrop python -B inkdrop_backup_restore.py restore /state/backups/inkdrop-backup-YYYYMMDD-HHMMSS-pre-upgrade.zip --target-config-dir /state/restore-preview-config --target-state-dir /state/restore-preview-state
docker compose run --rm --no-deps inkdrop python -B inkdrop_backup_restore.py restore /state/backups/inkdrop-backup-YYYYMMDD-HHMMSS-pre-upgrade.zip --target-config-dir /config --target-state-dir /state --apply
docker compose up -d --no-deps --force-recreate inkdrop
INKDROP_CONTAINER_PORT="$(docker inspect "$(docker compose ps -q inkdrop)" --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^INKDROP_PORT=//p')"
INKDROP_PUBLISHED_ADDRESS="$(docker compose port inkdrop "$INKDROP_CONTAINER_PORT" | head -n 1)"
INKDROP_EFFECTIVE_PORT="${INKDROP_PUBLISHED_ADDRESS##*:}"
rm -f /tmp/inkdrop-version.json
for attempt in $(seq 1 60); do curl -fsS "http://127.0.0.1:${INKDROP_EFFECTIVE_PORT}/api/system/version" -o /tmp/inkdrop-version.json && break; sleep 2; done
test -s /tmp/inkdrop-version.json
python -c 'import json,sys; p=json.load(open(sys.argv[1])); assert p["version"] == sys.argv[2], p; assert p["commit_sha"] == sys.argv[3], p; assert str(p["qa_build_number"]) == sys.argv[4], p' /tmp/inkdrop-version.json "$INKDROP_EXPECTED_VERSION" "$INKDROP_EXPECTED_COMMIT" "$INKDROP_EXPECTED_BUILD"
rm -f /tmp/inkdrop-version.json
test "$(docker inspect "$(docker compose ps -q inkdrop)" --format '{{.Config.Image}}')" = "$INKDROP_IMAGE"
docker compose exec inkdrop python -B inkdrop_container_healthcheck.py --json --wait-seconds 60
docker compose exec inkdrop python -B -c 'import json,sqlite3,inkdrop_runtime_config,inkdrop_state; c=sqlite3.connect(inkdrop_runtime_config.state_db_path()); actual=int(c.execute("select value from schema_meta where key=\"schema_version\"").fetchone()[0]); assert c.execute("pragma quick_check").fetchone()[0] == "ok"; assert not c.execute("pragma foreign_key_check").fetchall(); assert actual == inkdrop_state.SCHEMA_VERSION, (actual, inkdrop_state.SCHEMA_VERSION); print(json.dumps({"schema_version": actual, "quick_check": "ok", "foreign_key_violations": 0}))'
docker compose up -d --no-deps --force-recreate inkdrop-worker
test "$(docker inspect "$(docker compose ps -q inkdrop-worker)" --format '{{.Config.Image}}')" = "$INKDROP_IMAGE"
docker compose exec inkdrop-worker python -B inkdrop_container_healthcheck.py --worker --json --wait-seconds 90
docker compose ps
```

Never use `docker compose down -v`, delete the bind-mounted config/state
directories, or remove volumes as part of an upgrade or image rollback.

Optional adapters are considered configured only when their required connection
settings are present, or when an adapter-specific local DB/config path actually
exists inside the container. Placeholder defaults such as
`/config/kavita/kavita.db` do not by themselves make a fresh install depend on
Kavita or Kapowarr; the example `.env` and Compose defaults leave
`INKDROP_KAPOWARR_DB` and `INKDROP_KAVITA_DB` blank unless an operator is
deliberately migrating from those tools.
The `configured_adapters` preflight block includes `configured_by`,
`missing_required_keys`, existing-path evidence, and a short `reason` for each
adapter so operators can tell unconfigured adapters apart from broken mounted
paths or incomplete credentials.

Frontend scan adapters also stay opt-in. Leave `INKDROP_KAVITA_URL` and
`INKDROP_KOMGA_URL` blank unless InkDrop should ask those readers to rescan or
add books after import. Mounted reader databases may still be useful for
read-only visibility checks, but scan/add API calls require an explicit URL.

Likewise, the web Settings provider templates do not seed localhost service
URLs for Prowlarr, SLSKD, Kavita, Komga, or Kapowarr. Add those endpoints only
when the service is reachable from the InkDrop container. Preflight validates
configured service endpoints as full `http://` or `https://` URLs, so use
values like `http://prowlarr:9696` rather than bare hostnames.

For packaging and first-run UI, the redacted install support summary exposes an
`install_defaults` block. It is the machine-readable split between values that
should be prefilled and values that should remain blank:

- container paths and conservative automation knobs are prefilled
- optional adapter URLs, usernames, passwords, API keys, and tokens stay blank
- Compose service-name examples such as `http://slskd:5030/api/v0`,
  `http://sabnzbd:8080`, and `http://qbittorrent:8080` are suggestions only

Do not turn those suggestions into active config unless the operator chooses
the adapter and supplies any required credentials.

The same summary also exposes a `first_run_setup` block for UI/installer
workflows. It groups setup into runtime locations, media roots, folder naming,
library adapters, metadata providers, source providers, download clients,
manual staging, security summary, backup/export, and local-folder-only mode.
External adapters remain optional in that payload; a clean local-folder-only
install should be able to complete without qBittorrent, SABnzbd, SLSKD,
Prowlarr, Suwayomi, Kavita, Komga, Kapowarr, or ComicVine configured.

## Authentication

Built-in login/session auth is the closed-alpha default:

```env
INKDROP_AUTH_MODE=built_in
INKDROP_PASSWORD_MIN_LENGTH=8
```

New and changed passwords default to a minimum of eight characters. Set
`INKDROP_PASSWORD_MIN_LENGTH` from 1 through 128 for bootstrap policy, or save
the non-secret minimum in General > Authentication; a saved value takes
precedence. InkDrop allows spaces and Unicode and does not impose composition
rules. Existing password hashes remain valid when the policy changes.

Before the first administrator is created, InkDrop reports `setup_required`
and keeps the existing installation reachable so an image upgrade cannot lock
the operator out. Successful first-admin bootstrap immediately activates
enforcement. After that point only the root shell, health/version, auth status,
login, and recovery reset remain public; all application data and mutations
require an authenticated administrator or a scoped API key. Existing
installs may deliberately retain unauthenticated trusted-LAN testing with
`INKDROP_AUTH_MODE=disabled`, but only when both
`INKDROP_AUTH_ALLOW_DISABLED=1` and `INKDROP_TRUSTED_LAN_TESTING=1` are set.
API keys are created through InkDrop and their raw value is shown only once;
list and status payloads expose only masked previews and fingerprints.

For an existing unauthenticated QA installation, choose the upgrade behavior
before replacing the image:

1. Set `INKDROP_AUTH_MODE=built_in`, start the new image, and complete the
   one-time `/api/auth/bootstrap` flow. Existing APIs remain reachable during
   this bootstrap window; enforcement begins as soon as the administrator is
   created.
2. For temporary trusted-LAN continuity, set all three explicit values:
   `INKDROP_AUTH_MODE=disabled`, `INKDROP_AUTH_ALLOW_DISABLED=1`, and
   `INKDROP_TRUSTED_LAN_TESTING=1`. Move to built-in or trusted external auth
   before exposing the service beyond that LAN.

An unacknowledged `disabled` request is rejected and falls back to built-in
authentication, so a typo cannot silently expose the application.

Backend endpoints for setup/login/API clients:

- `GET /api/auth/status`
- `POST /api/auth/bootstrap`
- `POST /api/auth/login`
- `POST /api/auth/logout`
- `GET /api/auth/session`
- `POST /api/auth/password`
- `GET /api/auth/api-keys`
- `POST /api/auth/api-keys`
- `POST or DELETE /api/auth/api-keys/<id>/revoke`

Legacy `/api/inkdrop-auth/*` routes remain compatibility aliases. Cookie
mutations require `X-InkDrop-CSRF`; browser sessions receive the matching CSRF
value as a non-HttpOnly `inkdrop_csrf` cookie. API keys support `read`, `write`,
`settings`, `acquisition`, `maintenance`, and `admin` scopes.

### Password recovery

There is no in-app "forgot password" flow and InkDrop never sends email.
Recovering a locked-out account needs local access to the server InkDrop
runs on -- run these two commands inside the InkDrop container to create a
short-lived one-time token and reset the password:

```bash
docker compose exec inkdrop python -B inkdrop_auth_cli.py recovery-token
docker compose exec inkdrop python -B inkdrop_auth_cli.py reset-password
```

Recovery tokens expire and can be used only once. Password recovery revokes
existing sessions and never requires direct SQLite editing.

Redacted settings exports and support bundles omit password hashes, session
verifiers, API-key verifiers, recovery-token verifiers, and reusable secrets.
Full state backups contain the authentication database in verifier-only form
and are still sensitive operational data: never commit them, attach them to a
support report, or publish them as CI evidence.

Editable settings are available through the existing POST routes and REST-style
aliases: `PATCH` or `PUT /api/inkdrop-settings/app` for app settings and
`PATCH` or `PUT /api/inkdrop-settings/provider` for provider settings.

Reverse-proxy auth remains supported. Identity headers are ignored unless this
is explicitly enabled behind a trusted proxy:

```env
INKDROP_EXTERNAL_AUTH_ENABLED=1
INKDROP_EXTERNAL_AUTH_HEADER=X-Forwarded-User
INKDROP_EXTERNAL_AUTH_TRUSTED_PROXIES=172.18.0.0/16
```

The trusted proxy list is mandatory. Optional group restrictions use
`INKDROP_EXTERNAL_AUTH_GROUP_HEADER` and `INKDROP_EXTERNAL_AUTH_ADMIN_GROUP`.
Do not enable external header auth for a directly exposed InkDrop container.

`INKDROP_WEB_BASE_URL` is optional for the single-container Compose path. Leave
it blank unless an external worker, reverse proxy, or separate container needs
a stable operator-facing callback URL. Compose workers use
`INKDROP_CONTAINER_WEB_BASE_URL` (normally derived as
`http://inkdrop:${INKDROP_PORT}`), because loopback inside the worker container
does not reach the web container. Default scheduled jobs use trusted in-process
dispatch and do not weaken the HTTP authentication boundary.

If you explicitly enable worker HTTP callbacks with `INKDROP_WEB_BASE_URL`, a
per-route callback URL, or `INKDROP_WORKER_API_KEY`, create a dedicated InkDrop
API key with only `read` and `acquisition` scopes and place its one-time raw
value in `INKDROP_WORKER_API_KEY`. Worker requests use the existing
`X-InkDrop-API-Key` header. A missing, invalid, revoked, expired, or
under-scoped key fails closed; Docker network membership is never treated as
authentication. The key is redacted from preflight/effective configuration and
must not be placed in a URL. Leave
`INKDROP_MANUAL_SOURCE_IMPORT_API_URL` and `INKDROP_MARK_WAITING_API_URL` blank
unless an external worker needs custom callback URLs; internal workers derive
their endpoints from that container callback base.

## Runtime Volumes

The compose file uses neutral container paths:

- `/config`: user config and provider settings
- `/state`: SQLite state, worker state, logs, cache, backups, quarantine
- `/staging`: InkDrop-owned staged downloads and pack extraction workspace
- `/manual-inbox`: user-dropped files for inspection/import
- `/library`: optional local library root mount

## Existing Arr Stack Install

If you already run Sonarr/Radarr/Prowlarr/SABnzbd/qBittorrent/Kavita/Komga in
an Arr-style Docker stack, InkDrop should run as its own service/container.
Do not install it inside another application's container, and do not use another
application's config directory as InkDrop's `/config`.

For the first trial, a separate Compose project attached to the same Docker
network is the lowest-risk path because rollback is just `docker compose down`.
Adding InkDrop directly to an existing Arr Compose file is also valid when you
want one stack to manage all media services. In both cases, keep InkDrop's
`/config`, `/state`, `/staging`, `/manual-inbox`, and `/library` mounts
explicit and separate from existing app config folders.

If adding InkDrop directly to an existing Arr Compose file, do it deliberately:

- keep InkDrop's state/config/staging/manual-inbox/library volumes separate
  from existing application config folders
- prefer `compose.network.example.yml` when all you need is same-network access
  from a separate InkDrop Compose project
- use service DNS names such as `http://prowlarr:9696`,
  `http://sabnzbd:8080`, `http://qbittorrent:8080`, `http://kavita:5000`, or
  `http://komga:25600` only when those services share a Docker network with
  InkDrop
- keep optional adapter URLs and API keys blank until the matching service is
  reachable from inside the InkDrop container
- configure remote path mappings only when a download client reports paths
  InkDrop cannot read directly
- run strict preflight before enabling automation:

```bash
docker compose run --rm inkdrop python -B inkdrop_preflight.py --create --quiet --strict-dependencies --strict-runtime-tools
```

Do not mount another application's SQLite database or config folder just
because it exists. Kapowarr, Kavita, and Komga are compatibility or visibility
adapters, not required runtime dependencies for a clean InkDrop install.
For a copy/adapt service block, preflight commands, and rollback checklist,
see `docs/inkdrop/arr-stack-deployment-plan.md`.

## Migrating A Script-Managed Install

Some early homelab installs still run InkDrop as a direct Python web process
plus cron-launched workers. That mode can keep a working production instance
online during migration, but it is not the target public runtime. Before moving
one of those installs into Compose, capture the current process model:

- the web PID and command line
- crontab entries that call `inkdrop_*` or legacy `kavita_*` scripts
- current `INKDROP_CONFIG_DIR`, `INKDROP_STATE_DIR`, and mounted media roots
- state/config backup path from `inkdrop_backup_restore.py`
- current compact endpoint timing for Queue, Wanted, and Series

The safe migration pattern is parallel validation first, not an in-place cutover:

1. Back up the script-managed config/state directories.
2. Start the Compose service with separate `/config`, `/state`, `/staging`,
   `/manual-inbox`, and `/library` mounts.
3. Run strict preflight and the container healthcheck.
4. Import or restore config/state only after reviewing path warnings.
5. Verify `/status.json` and compact Queue/Wanted/Series endpoints.
6. Disable cron entries only after the Compose-managed worker service is known
   to cover the same jobs.

Do not leave both host cron workers and Compose workers active against the
same state database unless the job uses explicit locking and has been tested
that way. During migration, prefer report-only or dry-run commands for import,
cleanup, duplicate repair, and media-management tasks.
For a report-only proposal against an existing compose file, run:

```bash
python -B tools/inkdrop_compose_deployment_plan.py /path/to/compose.yaml --json
```

To write a separate overlay file instead of editing the existing stack:

```bash
python -B tools/inkdrop_compose_deployment_plan.py /path/to/compose.yaml --output inkdrop.override.yaml
docker compose -f /path/to/compose.yaml -f inkdrop.override.yaml config
```

The Dockerfile copies an explicit runtime allowlist into the image, and
`.dockerignore` now admits those runtime Python files by exact filename rather
than broad `inkdrop*.py` / `kavita*.py` globs. The image contains InkDrop
Python modules, legacy compatibility modules still used by the runtime, the
InkDrop logo asset, the README, requirements, and the source-candidate catalog
JSON. It does not copy the full homelab workspace. Runtime state, local
configs, media, backups, reports, and historical helper scripts belong in
mounted volumes or outside the image.
When a new root-level runtime Python module is added, update both
`.dockerignore` and the Dockerfile exact `COPY` list in the same change. The
public Docker runtime smoke derives the allowed root modules from
`.dockerignore` and fails if any allowed module is not explicitly copied into
the image.

The public repo also ships a conservative `.gitignore` for those same local
artifacts: `.env`, mounted runtime folders, SQLite databases, JSONL worker
logs, downloaded media, archives, backups, and temporary diagnostics should not
be committed.

Preflight treats `/config` and `/state` as required because InkDrop cannot keep
durable state without them. Staging, manual inbox, and quarantine roots are
reported as optional feature roots: missing or unwritable paths warn clearly and
limit related workflows, but should not prevent the web UI from explaining the
configuration problem.

Pack/import helpers use these same roots by default:

- `INKDROP_PACK_DOWNLOAD_ROOT=/staging/downloads/comics`
- `INKDROP_PACK_TEMP_DOWNLOAD_ROOT=/staging/temp/downloads/comics`
- `INKDROP_PACK_REVIEW_QUARANTINE_ROOT=/state/quarantine/pack-review`
- `INKDROP_MANAGED_DUPLICATE_QUARANTINE_ROOT=/state/quarantine/managed-duplicate-files`

The Compose contract also exposes conservative automation knobs rather than
hiding them in worker code:

- `INKDROP_PROTOCOL_ORDER=usenet,torrent,direct` controls the preferred
  download protocol order when an indexer can return multiple protocols.
  Supported values are `usenet`, `torrent`, and `direct`.
- `INKDROP_QUEUE_RUNNER_IMPORT_PRIORITY_READY_IMPORTS=1` finishes a downloaded
  file before Automatic Search starts another provider cycle. Search resumes
  after the ready-import queue drains.
- `INKDROP_IMPORT_READY_QUEUE_ONLY=0` keeps import-ready reconciliation from
  being limited to queue-owned rows unless explicitly enabled.
- `INKDROP_PACK_PROBE_SCAN_SECONDS=2` and
  `INKDROP_PACK_PROBE_SCAN_ENTRIES=20000` bound pack manifest probing so large
  weekly packs remain predictable.
- `INKDROP_DEBUG_ACTIVE_REQUESTS=0` keeps the active-request diagnostic
  endpoint disabled by default. Set it to `1` only for a short debugging
  session when you need `/api/inkdrop-debug/active-requests`.
- `INKDROP_CONTAINER_SCHEDULER_ENABLED=1` is the one knob for where
  scheduling lives, and both entrypoints read it. Left at its default, the
  `inkdrop` service runs the scheduler beside the web server on its own, so a
  single-service install still searches, downloads, and does maintenance
  instead of just serving a UI nothing feeds. The `inkdrop-worker` Compose
  service runs the same recurring import-ready, completed-import,
  manual-inbox, manual-source, queue/status, source-worker, metadata-guard,
  and cleanup commands from inside the InkDrop image. Running both is safe --
  every scheduled job holds a nonblocking per-job lock, so whichever service
  loses the race skips that run instead of double-running it. Set it to `0`
  on the web service if you'd rather scheduling live only in the dedicated
  worker.
- `INKDROP_SCHEDULER_MAX_CONCURRENCY=3` lets independent jobs run without one
  long import starving queue maintenance or status refresh. Each named job
  still has at most one active run.
- `INKDROP_SCHEDULER_HEARTBEAT_SECONDS=10` and
  `INKDROP_WORKER_STATUS_FILE=/state/worker-scheduler-status.json` control the
  persistent heartbeat used by Docker health.
- Failed jobs use bounded exponential backoff capped by
  `INKDROP_SCHEDULER_FAILURE_BACKOFF_MAX_SECONDS`. Optional-provider failures
  remain visible as degraded state without making core worker health fail.
- Leave `INKDROP_CONTAINER_WEB_BASE_URL` blank in normal Compose installs so it
  derives `http://inkdrop:${INKDROP_PORT}` for web-owned maintenance endpoints.
  An explicit value is an advanced worker callback override and must use the
  internal port, not `INKDROP_HOST_PORT`. In `network_mode: host` production
  overlays, use a loopback URL with the internal listener port.
- `INKDROP_SCHEDULER_*_INTERVAL_SECONDS` variables tune recurring worker
  cadences. Keep the defaults for first-run installs; they mirror the current
  production-safe cron cadence while keeping all writes inside the container
  runtime.

Preflight validates these exposed knobs before workers start. Invalid boolean,
integer, or protocol-order values fail clearly instead of being silently
clamped or ignored by a background worker.

If SABnzbd or another download client reports host/UNC paths that differ from
the container mount, map them explicitly instead of editing code:

```env
INKDROP_SAB_PATH_MAPPINGS=//server/share/downloads=/staging/downloads
```

If Prowlarr returns download URLs that point at an internal address but the
download client needs a different reachable URL, configure both sides
explicitly:

```env
INKDROP_PROWLARR_INTERNAL_BASE_URLS=http://prowlarr:9696
INKDROP_PROWLARR_PUBLIC_BASE_URL=http://prowlarr.example.internal:9696
```

The Prowlarr source worker derives its allowed request hosts from
`INKDROP_PROWLARR_URL`, `INKDROP_PROWLARR_PUBLIC_BASE_URL`, and
`INKDROP_PROWLARR_INTERNAL_BASE_URLS`. If a custom deployment needs additional
Prowlarr metadata hosts, set `INKDROP_SOURCE_WORKER_PROWLARR_ALLOWED_HOSTS`
explicitly as a comma-separated hostname list.

`INKDROP_TRUSTED_PROWLARR_HOSTS` is blank by default. Only set it when InkDrop
must fetch Prowlarr-protected pack metadata from specific trusted hostnames and
you want InkDrop to attach the configured Prowlarr API key for those hosts.
Fresh Docker installs should prefer normal service URLs such as
`http://prowlarr:9696` over localhost-style trust defaults.

Fresh installs should not require maintainer-specific home directories, media
mounts, Kapowarr, Kavita, Komga, Prowlarr, SLSKD, SABnzbd, or qBittorrent.
Those are optional adapters as the standalone migration continues.

Suwayomi is also optional. Leave `INKDROP_SUWAYOMI_API_BASE_URL` blank until
you have a Suwayomi server reachable from the InkDrop container; source-worker
provider rows can still override the base URL when you enable that source.

## Current Limits

- The web runtime starts from `inkdrop_web.py`, but some internal modules still
  contain legacy `kavita_*` names during migration.
- Some advanced source/download automation still expects configured external
  adapters.
- Destructive media repairs should stay dry-run/apply gated.

## Release Checks

The public-release CI workflow runs the same local checks expected before a
Docker-first release:

```bash
python -B tools/inkdrop_public_release_check.py
python -B tools/inkdrop_docker_context_manifest.py --summary
python -B tools/inkdrop_docker_context_manifest.py --json
python -B tools/inkdrop_install_support_summary.py --json
python -B tools/inkdrop_release_evidence_bundle.py
python -B tools/inkdrop_release_evidence_bundle.py --remote-host user@docker-host
python -B tools/inkdrop_public_http_smoke.py --json
python -B tools/inkdrop_public_release_check.py --docker --require-docker
python -B tools/inkdrop_public_release_check.py --docker-only --require-docker
python -B tools/inkdrop_public_release_check.py --docker --require-docker --skip-docker-build
python -B tools/inkdrop_public_release_check.py --docker-only --skip-docker-build
docker compose config --quiet
docker compose build inkdrop
docker compose run --rm inkdrop python -B inkdrop_preflight.py --create --quiet --strict-dependencies --strict-runtime-tools
docker compose run --rm inkdrop python -B inkdrop_container_healthcheck.py --preflight-only
```

Local development environments without Docker can still run the first command;
it executes Python compile checks, the public runtime smoke, the safety audit,
and a host preflight in temporary runtime folders. Add
`--strict-host-dependencies` after installing `requirements.txt` if you want
host preflight to fail on missing local Python packages and also run the live
temp-root HTTP smoke:

```bash
python -B tools/inkdrop_public_release_check.py --strict-host-dependencies
```

`python -B tools/inkdrop_docker_context_manifest.py --summary` and
`python -B tools/inkdrop_docker_context_manifest.py --json` are Docker-free
review aids for the build context. They apply the repository `.dockerignore`
contract and print the files, sizes, and SHA-256 hashes that should be visible
to a public image build. The summary form is what CI writes into the GitHub
Actions step summary. The main local release runner also executes
`tools/inkdrop_docker_context_manifest.py --warnings-json`, so context warning
regressions appear in the release JSON. Manifest size warnings are
non-blocking review signals for oversized legacy modules; neither form
replaces the Docker build/preflight gate.

This development checkout may live beside unrelated homelab automation. Before
publishing to the public GitHub repo, create a sanitized InkDrop-only export:

```bash
python -B tools/inkdrop_public_repo_export.py --json
python -B tools/inkdrop_public_repo_export.py --apply --target ../InkDrop-public
```

Use the staged export directory as the source for publication. The helper writes
`PUBLIC_REPO_MANIFEST.json` in that target and blocks missing files, forbidden
paths, local state/config roots, databases, archives, media files, and private
workspace paths. The release evidence bundle also records the export dry-run
result.
The manifest describes that generated export only; it is not checked into or
read from the maintainer workspace.

`python -B tools/inkdrop_install_support_summary.py --json` is a Docker-free
support aid. It reports `install_support_schema_version`, preflight/release
schema versions, Docker availability, warning summaries, adapter readiness,
root status booleans, `release_gate`, and path-mapping counts without echoing
full effective config, secrets, or host path mappings. It also includes
`setup_guidance` and per-adapter impact/next-step text, which makes clean
installs with blank optional providers easier to understand. The `release_gate`
block is always `ready=false` in this support tool because it does not run
Docker; it names
`python -B tools/inkdrop_public_release_check.py --docker --require-docker` and
the Docker checks still required before a broader release. Add `--create` if you want
the summary to create/check the configured runtime directories like the normal
preflight.
The public safety audit also checks that secret-looking keys in `.env.example`
default blank, so example config can be copied without carrying placeholder
tokens or local credentials.
Preflight accepts but warns about adapter URLs that point at `localhost`,
`127.0.0.1`, or `::1`, because inside Docker those usually point at the InkDrop
container rather than the external service. Prefer a Compose service name, LAN
host, reverse proxy name, or `host.docker.internal` when appropriate.
When Docker is unavailable in the current shell, `setup_guidance` includes a
Docker warning so support bundles clearly distinguish host-only checks from the
required Docker Compose release gate.

For a repeatable release packet, run:

```bash
python -B tools/inkdrop_release_evidence_bundle.py
```

The helper creates `release-evidence/inkdrop-public-release-evidence-*` with
local release JSON, install support JSON, Docker context manifest output, a
Docker-only validation tarball, and `remote-docker-only-command.sh`.
With `--remote-host user@docker-host`, it also copies the tarball over SSH,
runs the Docker-only gate remotely, and stores `inkdrop-docker-only-release.json`
beside the local evidence. When the local Docker-free checks pass and the
remote Docker-only gate passes, the bundle summary reports
`split_host_release_ready=true`.

If Docker-free checks have already passed from the same revision and only a
Docker-capable host is available for Compose validation, use:

```bash
python -B tools/inkdrop_public_release_check.py --docker-only --require-docker
```

`--docker-only` skips host/static smokes and records those checks as
`provided_separately` in `release_blockers.items[]`; it is only valid when the
Docker-free runner output from the same revision is preserved.

`python -B tools/inkdrop_public_http_smoke.py --json` starts `inkdrop_web.py`
on an ephemeral localhost port with temporary config/state/staging roots, seeds
an empty InkDrop schema, probes `/status.json` and
`/api/inkdrop-state/sections`, then shuts the process down. It is intended for
CI or hosts with `requirements.txt` installed; it does not touch live media,
downloads, or reader databases.

A release candidate is not fully validated until the Compose build and
container preflight pass on a Docker-capable host. In JSON output, `ok` means
the requested checks passed, while `release_ready` is true only after the
Docker gate passes. `release_check_schema_version` marks the machine-readable
output contract, and `release_blocker_schema_version` marks the
release-blocker summary contract. Use `release_blockers.items[]` to see each
broader-release gate as passed, failed, skipped, missing, or not requested. Use
`docker_gate_status` to see whether the Docker gate passed, failed, was
unavailable, or was not requested. The Docker-free release runner also executes
`tools/inkdrop_install_support_summary.py --create --json` with temporary
runtime roots, which catches redacted support-bundle regressions before the
Docker-capable container check.

Use `--skip-docker-build` only after `docker compose build inkdrop` has already
completed for the same checkout. The release runner will still validate Compose
config and run strict container preflight, and its JSON output reports
`docker_build_skipped=true` with an explicit skipped `docker_compose_build`
result.

## Broader Release Blockers

Do not publish beyond the closed-alpha channel until all release blockers are cleared:

- `python -B tools/inkdrop_public_release_check.py --docker --require-docker`
  exits with `ok=true` and `release_ready=true`.
- The release JSON reports `release_blockers.ready=true`; any failed
  `release_blockers.items[]` entry is treated as a broader-release blocker.
- `docker compose build inkdrop` completes on a Docker-capable host.
- `docker compose run --rm inkdrop python -B inkdrop_preflight.py --create --quiet --strict-dependencies --strict-runtime-tools`
  passes inside the built image.
- `docker compose run --rm inkdrop python -B inkdrop_container_healthcheck.py --preflight-only`
  passes inside the built image.
- `python -B tests/inkdrop-public-release-safety-audit.py` reports
  `finding_count=0`.
- The `inkdrop-public-release-evidence` artifact contains the release JSON,
  install support JSON, Docker context manifest, public HTTP smoke JSON, and
  Docker-only gate JSON when split-host validation is used.
- Docker context warnings are reviewed. The only accepted large-context
  warnings are the large modules `inkdrop_state.py` and
  `inkdrop_web.py`; the manifest records their reason, risk, owner,
  next action, and exit criteria as packaging debt, and any new unaccepted large context file fails the warning check.
