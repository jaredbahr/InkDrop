# InkDrop Existing Arr Stack Deployment Plan

This is a public-safe deployment guide for operators who already run an
Arr-style Docker stack. It is intentionally conservative: InkDrop should start
with its own state/config/staging/library mounts, and optional adapters should
stay blank until the operator chooses to enable them.

## Recommended First Install

InkDrop should run as a real Docker service/container. It should not be copied
into another application's container, and it should not remain a loose pile of
host scripts as the long-term install.

For the lowest-risk source-build trial, you can run InkDrop as a separate
Compose project attached to the same Docker network as your existing Arr stack:

```bash
mkdir -p inkdrop
cd inkdrop
git clone https://github.com/jaredbahr/InkDrop .
cp .env.example .env
docker compose up -d --build
docker compose run --rm inkdrop python -B core/inkdrop_preflight.py --create --quiet --strict-dependencies --strict-runtime-tools
```

That trial keeps rollback simple:

```bash
docker compose down
```

Running the first trial separately avoids accidental edits to existing Sonarr,
Radarr, Prowlarr, SABnzbd, qBittorrent, Kavita, Komga, or reverse-proxy
services. It is still a true InkDrop container, not a host-script install.
Closed-alpha candidate testing should use the source-free install packet from
the validated prerelease instead of this source-build path.

Suwayomi is also external to this Compose contract. Setting
`INKDROP_SUWAYOMI_API_BASE_URL` connects InkDrop to an existing endpoint; it
does not install Suwayomi or take ownership of its image, data volume,
extensions, or credentials. Follow the
[Suwayomi quick start](SUWAYOMI_QUICK_START.md) for the separate UID/GID and
fresh-storage contract.

If your operational preference is one existing Arr Compose file, that is a
valid target too. Add InkDrop as its own `inkdrop` service with its own
`/config`, `/state`, `/staging`, `/manual-inbox`, and `/library` mounts. Do not
install InkDrop inside another container or reuse another application's config
directory as InkDrop's `/config`.

## Adding To An Existing Stack

If you add InkDrop to an existing Arr Compose file, copy/adapt this service
block. Do not paste secrets into source-controlled compose files; put API keys
and passwords in `.env` or a private Docker secret workflow.
You can generate the same report-only plan for a real compose file with:

```bash
python -B tools/inkdrop_compose_deployment_plan.py /path/to/compose.yaml
python -B tools/inkdrop_compose_deployment_plan.py /path/to/compose.yaml --json
```

The helper reports service names, whether `inkdrop` already exists, a proposed
service block, required preflight commands, and safety notes. It does not write
or edit compose files unless you explicitly ask it to write a separate overlay
file:

```bash
python -B tools/inkdrop_compose_deployment_plan.py /path/to/compose.yaml --output inkdrop.override.yaml
docker compose -f /path/to/compose.yaml -f inkdrop.override.yaml config
docker compose -f /path/to/compose.yaml -f inkdrop.override.yaml run --rm inkdrop python -B core/inkdrop_preflight.py --create --quiet --strict-dependencies --strict-runtime-tools
docker compose -f /path/to/compose.yaml -f inkdrop.override.yaml run --rm inkdrop python -B core/inkdrop_container_healthcheck.py --preflight-only
```

The `--output` mode refuses to overwrite an existing overlay unless you pass
`--force`, and it refuses to write an overlay when the base compose file
already has an `inkdrop` service. It never edits the base compose file.
If you use `--service-name`, the generated service block and preflight commands
use that same name.

```yaml
services:
  inkdrop:
    build:
      context: ${INKDROP_BUILD_CONTEXT:-./InkDrop}
    restart: unless-stopped
    environment:
      INKDROP_HOST: 0.0.0.0
      INKDROP_PORT: ${INKDROP_PORT:-8796}
      INKDROP_HOST_PORT: ${INKDROP_HOST_PORT:-${INKDROP_PORT:-8796}}
      INKDROP_CONFIG_DIR: /config
      INKDROP_STATE_DIR: /state
      INKDROP_STAGING_DIR: /staging
      INKDROP_MANUAL_INBOX_DIR: /manual-inbox
      INKDROP_QUARANTINE_DIR: /state/quarantine
      INKDROP_COMIC_ROOT: /library/comics
      INKDROP_MANGA_ROOT: /library/manga

      # Optional adapters. Leave blank until each service is reachable from
      # inside the InkDrop container and credentials/path mappings are known.
      INKDROP_COMICVINE_API_KEY: ${INKDROP_COMICVINE_API_KEY:-}
      INKDROP_PROWLARR_URL: ${INKDROP_PROWLARR_URL:-}
      INKDROP_PROWLARR_API_KEY: ${INKDROP_PROWLARR_API_KEY:-}
      INKDROP_SABNZBD_URL: ${INKDROP_SABNZBD_URL:-}
      INKDROP_SABNZBD_API_KEY: ${INKDROP_SABNZBD_API_KEY:-}
      INKDROP_QBITTORRENT_URL: ${INKDROP_QBITTORRENT_URL:-}
      INKDROP_QBITTORRENT_USERNAME: ${INKDROP_QBITTORRENT_USERNAME:-}
      INKDROP_QBITTORRENT_PASSWORD: ${INKDROP_QBITTORRENT_PASSWORD:-}
      INKDROP_SLSKD_API_BASE_URL: ${INKDROP_SLSKD_API_BASE_URL:-}
      INKDROP_SUWAYOMI_API_BASE_URL: ${INKDROP_SUWAYOMI_API_BASE_URL:-}
      INKDROP_KAVITA_URL: ${INKDROP_KAVITA_URL:-}
      INKDROP_KOMGA_URL: ${INKDROP_KOMGA_URL:-}
      INKDROP_SAB_PATH_MAPPINGS: ${INKDROP_SAB_PATH_MAPPINGS:-}
      INKDROP_UNC_PATH_MAPPINGS: ${INKDROP_UNC_PATH_MAPPINGS:-}
    ports:
      - "${INKDROP_HOST_PORT:-${INKDROP_PORT:-8796}}:${INKDROP_PORT:-8796}"
    volumes:
      - ./inkdrop/config:/config
      - ./inkdrop/state:/state
      - ./inkdrop/staging:/staging
      - ./inkdrop/manual-inbox:/manual-inbox
      - ./inkdrop/library:/library
    healthcheck:
      test: ["CMD", "python", "-B", "core/inkdrop_container_healthcheck.py", "--timeout", "5"]
      interval: 60s
      timeout: 10s
      retries: 3
      start_period: 30s
```

Service-name URLs such as `http://prowlarr:9696`,
`http://sabnzbd:8080`, `http://qbittorrent:8080`,
`http://kavita:5000`, or `http://komga:25600` are useful only when those
services share a Docker network with InkDrop. Do not prefill them as active
configuration for users who may not run those services.

## Preflight Before Automation

After adding the service, run:

```bash
docker compose -f /path/to/compose.yaml -f inkdrop.override.yaml config
docker compose -f /path/to/compose.yaml -f inkdrop.override.yaml run --rm inkdrop python -B core/inkdrop_preflight.py --create --quiet --strict-dependencies --strict-runtime-tools
docker compose -f /path/to/compose.yaml -f inkdrop.override.yaml run --rm inkdrop python -B core/inkdrop_container_healthcheck.py --preflight-only
```

Expected clean-install warnings include unconfigured optional adapters. They
should not block local-folder/manual-inbox mode.

## Roll Back An Arr Compose Edit

Image upgrades and rollback to a previous immutable image use the pinned
`INKDROP_IMAGE` procedure in
[`docker-first-install.md`](docker-first-install.md#upgrade-and-image-version-rollback).
That procedure records the previous digest, backs up state, replaces both web
and worker containers, verifies API/health/schema identity, and keeps the
existing mounts. Do not substitute the service-removal steps below for an image
rollback.

Before editing an existing Arr stack:

```bash
cp compose.yaml compose.yaml.bak-$(date +%Y%m%d-%H%M%S)
```

Rollback is:

```bash
docker compose stop inkdrop
docker compose rm inkdrop
mv compose.yaml.bak-YYYYMMDD-HHMMSS compose.yaml
docker compose up -d
```

Do not delete InkDrop's mounted `./inkdrop/state` or `./inkdrop/config`
folders unless you deliberately want to discard its queue, history, settings,
and backups.

## Safety Notes

- Do not mount another application's config folder as InkDrop's `/config`.
- Do not mount another application's SQLite database unless you are explicitly
  using a migration/visibility adapter and understand that it is read-only
  compatibility state, not InkDrop's source of truth.
- Do not make qBittorrent, SABnzbd, SLSKD, Prowlarr, Kavita, Komga,
  Suwayomi, or ComicVine required for a clean start.
- Do not commit `.env`, real hostnames, LAN IPs, API keys, passwords, media
  paths, generated evidence bundles, or mounted runtime folders.
