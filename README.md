<p align="center">
  <img src="inkdrop-logo-mark.png" alt="InkDrop" width="160">
</p>

<h1 align="center">InkDrop</h1>

<p align="center">Comic and manga library tracking, search, verification, and import.</p>

<p align="center">
  <a href="https://github.com/jaredbahr/InkDrop/releases"><img src="https://img.shields.io/github/v/release/jaredbahr/InkDrop?include_prereleases&amp;style=flat-square&amp;label=Prerelease" alt="Latest prerelease"></a>
  <a href="https://github.com/users/jaredbahr/packages/container/package/inkdrop"><img src="https://img.shields.io/badge/ghcr.io-inkdrop-blue?style=flat-square&amp;logo=docker&amp;logoColor=white" alt="InkDrop container image on GHCR"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-GPL--3.0--or--later-blue.svg?style=flat-square" alt="GPL-3.0-or-later license"></a>
</p>

InkDrop manages comic and manga libraries. Add the series you collect and it keeps track of the issues or volumes you are missing. It searches the sources you connect, checks the files it gets back, and imports them into the right library folder.

Comics and manga are not always named or released in a consistent way. The same title may appear as single issues, chapters, trades, volumes, or omnibuses. Official releases and scanlations may use different names. Large packs may contain files from several series.

InkDrop checks those details before a file reaches your library.

## What it does

- Tracks missing issues and volumes with ComicVine and MangaDex metadata.
- Can link ComicVine and MangaDex records when it finds one clear match for the same series.
- Can search configured, enabled providers and retry later when automation is on and the series is monitored.
- Supports Prowlarr-managed indexers, Soulseek through slskd, MangaDex, and Suwayomi.
- Opens downloaded archives and checks that they are readable.
- Matches files to the expected series, issue, or volume before import.
- Rejects broken archives and holds imports when it cannot establish a safe match.
- Renames and organizes files with consistent folders and filenames.
- Provides manual search when you want to choose a release yourself.
- Can ask Kavita to scan the library after an import.

## What you need

- Docker Compose v2 on a Linux host, or another system that can run Linux containers
- A comics folder, a manga folder, or both
- At least one supported download source if you want automatic acquisition

A ComicVine API key is optional because InkDrop can use local metadata. Prowlarr, slskd, qBittorrent, SABnzbd, Suwayomi, and Kavita are optional too. Use only the services that fit your setup.

## Quick start

Keep InkDrop on a trusted LAN or VPN. The default endpoint uses plain HTTP. Do not publish port 8796 directly to the internet. Put remote access behind a trusted HTTPS reverse proxy, and create the admin login before other clients can reach InkDrop.

Create a `docker-compose.yml` file:

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

Change the two host-side library paths. Leave `/data/comics` and `/data/manga` unchanged unless you also change Comic root or Manga root under Settings > Media Management.

This example enables automatic searching. Set `INKDROP_QUEUE_RUNNER_AUTOPILOT_ENABLED` to `"0"` if you want to finish setup before InkDrop starts searching.

Start InkDrop:

```bash
docker compose up -d
```

Open `http://your-host:8796`.

The first-run setup asks you to create a login, choose your library folders, and connect the download sources you use. InkDrop keeps its database, logs, cache, and backups under `./config`. Downloads are staged under `./downloads` before they are checked and imported.

## First steps

1. Open Series and add something you collect.
2. Choose the correct metadata result.
3. Choose whether you collect it as issues or volumes.
4. Open Wanted to see what is missing.
5. Open Activity to see current work.
6. Open History to see completed results.
7. Leave InkDrop running.

With automation on, an enabled provider, and the series monitored, InkDrop retries searches over time. An empty first search does not mean the item will never be found.

## How it fits together

- ComicVine and MangaDex provide series and release metadata.
- Prowlarr, slskd, MangaDex, and Suwayomi provide search or download results.
- qBittorrent and SABnzbd handle downloads when those clients are configured.
- Kavita can rescan a library after InkDrop imports a file.
- The web app and scheduler run together in the public one-container install.

## Stack

| Part | Used here |
| --- | --- |
| Web | React 19 and TypeScript |
| App | Python 3.12 |
| State | SQLite |
| Install | Docker Compose |

## Current status

InkDrop is in beta.

I use this build every day with a library containing thousands of books. Tracking, search, download, verification, and import are working. There are still rough edges.

Known limitations:

- Large imports can make the web interface respond slowly.
- Prowlarr, Soulseek, and MangaDex have received the most testing.
- Other integrations may need more work.
- There is no release calendar. The available metadata does not provide dependable future dates for enough titles.
- Updates are currently manual. Pull or replace the container image when a new build is released.

## Reporting a problem

Open an issue in this repository. Include:

- The series and issue or volume involved
- What you expected
- What happened
- The related entry from History
- A screenshot when the problem is visual

The History entry is usually more useful than a general log dump. It includes the decisions InkDrop made for that item.

## License

InkDrop is licensed under the [GNU General Public License, version 3 or later](LICENSE).
