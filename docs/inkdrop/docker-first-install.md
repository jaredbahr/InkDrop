# InkDrop install guide

InkDrop runs as a single Docker container. This guide covers installing it,
getting through first run, keeping it updated, and what to do when something
looks wrong.

**InkDrop is prerelease software.** It is usable and in daily use, but it is
not finished, and releases are published as prereleases on purpose. Expect
rough edges, keep backups of anything you care about, and do not point it at a
library you cannot afford to have reorganized until you have watched it work.

## Before you start

You need:

- Docker with Compose v2, on a Linux host or anything else that runs Linux
  containers.
- A comics folder, a manga folder, or both.
- At least one download source, if you want InkDrop to acquire anything. It
  will track and organize a library without one.

Everything else is optional. ComicVine, Prowlarr, slskd, qBittorrent, SABnzbd,
Suwayomi, Kavita, and Komga are each used only if you configure them.

**Keep InkDrop on a trusted network.** It serves plain HTTP and is not built to
face the internet directly. Do not forward port 8796. If you need remote
access, put it behind an HTTPS reverse proxy you trust, and create the admin
login before anything else can reach it.

## Install

Create a `docker-compose.yml`:

```yaml
services:
  inkdrop:
    image: ghcr.io/jaredbahr/inkdrop:latest
    container_name: inkdrop
    environment:
      INKDROP_QUEUE_RUNNER_AUTOPILOT_ENABLED: "1"
    ports:
      - "8796:8796"
    volumes:
      - ./config:/config
      - ./downloads:/downloads
      - /path/to/your/Comics:/data/comics
      - /path/to/your/Manga:/data/manga
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "python", "-B", "core/inkdrop_container_healthcheck.py", "--timeout", "5"]
      interval: 60s
      timeout: 10s
      retries: 3
      start_period: 30s
```

Change the two host-side library paths on the left of the colon. Leave
`/data/comics` and `/data/manga` as they are unless you also change Comic root
and Manga root under Settings > Media Management.

The four mounts are the whole layout:

| Mount | Holds |
| --- | --- |
| `/config` | database, settings, logs, cache, backups, quarantine |
| `/downloads` | downloads in progress, staged before they are checked |
| `/data/comics` | your comic library |
| `/data/manga` | your manga library |

Anything InkDrop needs to keep lives under `/config`. Back that up and you have
backed up InkDrop.

Start it:

```bash
docker compose up -d
```

Then open `http://your-host:8796` and create your login.

The example above turns automatic searching on. Set
`INKDROP_QUEUE_RUNNER_AUTOPILOT_ENABLED` to `"0"` if you would rather finish
configuring sources before InkDrop starts searching for anything.

### Pinning a version

`latest` tracks the newest release. If you would rather pin — and on prerelease
software that is a reasonable thing to want — replace it with a version tag:

```yaml
    image: ghcr.io/jaredbahr/inkdrop:0.1.15
```

Every published release has a matching image tag, so the version numbers on the
releases page are the tags you can use. Pinning also gives you somewhere to go
back to; see Updating below.

## Your first ten minutes

**If you already have comics or manga organized on disk, start there.** Go to
Settings > Media Management > Library adoption and point it at a folder.
InkDrop scans it, works out what you already own, and shows you what it found.
Nothing is written until you review the results and press Adopt for a specific
folder — it registers what you have as already satisfied, and does not download
or move anything.

Doing this first means InkDrop knows what you own before it starts looking for
what you are missing. Adding series one at a time works too, but on an existing
library it is much slower and InkDrop may search for issues already on disk.

Then:

1. Open Series and add something you collect that adoption did not cover.
2. Pick the right metadata result.
3. Say whether you collect it as issues or as volumes.
4. Check Wanted for what is missing.
5. Watch Activity for work in progress, and History for finished results.
6. Leave it running.

With automation on, a provider enabled, and the series monitored, InkDrop keeps
retrying over time. A first search that finds nothing is normal and does not
mean the item will never turn up.

## Configuration

Nearly everything is configured in the web interface under Settings. That is
the intended path and the one that gets tested.

**This Compose file does not read a `.env` file.** Docker Compose loads `.env`
for substitution into the Compose file itself, not into the container, and the
file above has nothing to substitute. Putting `INKDROP_SLSKD_URL=...` in a
`.env` next to it will appear to work and will reach nothing.

To set an environment variable, put it in the `environment:` block directly:

```yaml
    environment:
      INKDROP_QUEUE_RUNNER_AUTOPILOT_ENABLED: "1"
      INKDROP_COMICVINE_API_KEY: "your-key-here"
```

Then `docker compose up -d --force-recreate` to apply it.

## Running as your own user

By default the container runs as root, so files it creates under `./config` and
`./downloads` are root-owned on the host. To run as your own account instead,
set both `PUID` and `PGID`:

```yaml
    environment:
      PUID: "1000"
      PGID: "1000"
```

Use `id -u` and `id -g` to find yours. On startup InkDrop remaps its internal
account to those ids, takes ownership of the directories it manages, and drops
root before the application starts.

Existing library content is left alone — only the mount point itself is
adjusted, which is enough for new imports to land with the right ownership. If
you are moving over from a root-owned install and want the library reowned as
well, set `INKDROP_CHOWN_LIBRARY=1` for one start; on a large library this can
take a while. Set `INKDROP_SKIP_CHOWN=1` if you manage ownership yourself.

Leaving `PUID` and `PGID` unset keeps the existing root behaviour, so adding
them later is opt-in and upgrading never changes it for you.

## Updating

```bash
docker compose pull
docker compose up -d
```

If you pinned a version, change the tag first, then run those two commands.

**Rolling back** is the reason to pin. Set `image:` to the previous version tag
and run the same two commands — old release tags stay published. Note that
InkDrop may upgrade its database when a newer build starts, and going back to
an older build afterwards is not something to rely on. Take a backup before
updating if the install matters to you.

Updates are manual. There is no in-app updater.

**Being told a new version exists** is optional and off until you ask for it.
Set `INKDROP_UPDATE_MANIFEST_URL` to the release manifest you want InkDrop to
watch and it will check on a schedule and mention a newer release in the web
interface. It never downloads or installs anything -- the two commands above
are still how you update. With the key unset InkDrop makes no update request at
all, so an install that never sets it talks to nothing.

The URL has to be a release asset of an accepted repository, and the accepted
repository is `jaredbahr/InkDrop`. Anything outside that is refused before the
request is made, and a manifest naming a repository outside it is refused after
the fetch, so a redirected or substituted asset cannot be adopted. If you
publish InkDrop releases somewhere else, set
`INKDROP_UPDATE_RELEASE_REPOSITORY=owner/name`: that repository is *added* to
the accepted set rather than replacing it, and a value that is not a plain
`owner/name` pair is ignored rather than trusted.

## Backups

InkDrop keeps its own backups under `/config`, with retention you can set in
Settings. Because everything lives under that one mount, copying `./config`
while the container is stopped is a complete backup.

**Provider credentials are deliberately not included** — settings exports are
written with secrets redacted. After restoring, re-enter your API keys and
passwords. A restore brings back your library state, not your logins.

## When something looks wrong

Check the container first:

```bash
docker compose ps
docker compose logs --tail 100 inkdrop
```

The healthcheck runs on its own and a container reporting unhealthy usually
means the application did not finish starting. The logs say why.

Things that are expected and not faults:

- Warnings that optional adapters are unconfigured. That is normal until you
  configure providers, and stays normal for the ones you never use.
- An empty first search. See the note under First steps.
- A library root that is missing or not writable **will** block imports even
  when the container is healthy. If imports are not happening, check that
  first — the mount has to exist and be writable, and a mounted parent folder
  does not create the subfolders inside it for you.

The entrypoint is quiet on a normal start. Set
`INKDROP_ENTRYPOINT_VERBOSE=1` for startup detail when you are diagnosing
permissions or user-remapping problems. Failures that matter are always printed
regardless of that setting.

## Building from source

The published Compose file is image-only and has no `build:` block, so
`docker compose up -d --build` will pull the published image rather than build
your checkout. To build the image yourself:

```bash
git clone https://github.com/jaredbahr/InkDrop
cd InkDrop
docker build -t inkdrop:local .
```

Then set `image: inkdrop:local` in your Compose file and start it as usual.
The build compiles the web frontend and needs network access for package
installs.

Note that this repository is a published mirror of a private development tree,
so a build from it is a snapshot of the last release rather than current
development. `CONTRIBUTING.md` explains how changes get in.

## Reporting a problem

Open an issue. The single most useful thing you can include is the relevant
entry from History — it carries the decisions InkDrop made for that item, which
a general log dump does not. Beyond that: what you expected, what happened, the
series and issue or volume involved, and a screenshot if it is visual.

For anything security-sensitive, use private vulnerability reporting on the
repository rather than a public issue.
