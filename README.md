# InkDrop

InkDrop manages comic and manga libraries. Add the series you collect and it keeps track of the issues or volumes you are missing, searches your configured sources, checks the files it gets back, and imports them into the correct library folder.

If you use Kavita, InkDrop can also trigger a library scan after an import.

The workflow will feel familiar if you have used Sonarr or Radarr. Comics and manga need some additional handling, though. The same title may be released as single issues, chapters, trades, volumes, or omnibuses. Official releases and scanlations may use completely different names, and large packs can contain hundreds of files from several series.

InkDrop tries to sort that out before anything reaches your library.

## What it does

- Keeps track of missing issues and volumes using ComicVine for comics and MangaDex for manga.
- Links ComicVine and MangaDex records when the same series exists in both, preventing chapters and volumes from being counted twice.
- Searches your connected sources automatically and retries failed searches later.
- Supports Prowlarr-managed indexers, Soulseek through slskd, and downloads through MangaDex and Suwayomi.
- Checks downloaded files before importing them. InkDrop opens the archive, makes sure it is readable, and matches it to the expected series and issue or volume.
- Sets aside broken archives and questionable matches instead of adding them to your library.
- Renames and organizes imported files using consistent series folders and filenames.
- Provides a manual search when you would rather choose a release yourself.
- Runs larger library scans and maintenance jobs on a schedule instead of continuously.

## What you need

- Docker Compose v2 on a Linux host, or another system capable of running Linux containers
- A comics folder, a manga folder, or both
- A free ComicVine API key if you collect western comics
- At least one supported download source

Depending on your setup, you may also want:

- Prowlarr and one or more configured indexers
- slskd for Soulseek
- qBittorrent or SABnzbd
- Kavita as your reader

Kavita is optional, but InkDrop can request a library scan after importing new files.

## Installation

```yaml
services:
  inkdrop:
    image: ghcr.io/jaredbahr/inkdrop:latest
    container_name: inkdrop
    environment:
      # Off by default so a fresh install never grabs anything before you've
      # reviewed your setup. Turned on here because this is the documented
      # path new users follow, and a beta build that silently finds nothing
      # is worse than one that starts searching right away.
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
      test: ["CMD", "python", "-B", "inkdrop_container_healthcheck.py", "--timeout", "5"]
      interval: 60s
      timeout: 10s
      retries: 3
      start_period: 30s
```

Point the two library lines at your own folders and leave the container-side
paths alone. InkDrop reads comics from `/data/comics` and manga from
`/data/manga`, keeps its database, logs, and backups under `/config`, and
downloads into `/downloads` before checking and importing them. If you would
rather mount a library somewhere else, change Comic root or Manga root under
**Settings -> Media Management** to match, otherwise InkDrop will not see it.

Start the container:

```bash
docker compose up -d
```

Then open:

```text
http://your-host:8796
```

The first-run setup will ask you to create a login, select your library folders, and connect the download sources you use.

The first person to open InkDrop creates the administrator account, and the
setup screen closes for good once that account exists. There is no setup code
to copy. That does mean anyone who can reach port 8796 before you finish could
create the account instead of you, so keep the port on your own network until
setup is done. Do not forward it through your router or expose it to the
internet first.

Settings include a short explanation of what they control. If something is still unclear, open an issue.

## Getting started

1. Open **Series** and add something you collect.
2. Select the correct metadata result and choose whether you collect it as issues or volumes.
3. Open **Wanted** to see what is currently missing.
4. Open **Activity** to see active downloads and previous results.
5. Leave InkDrop running.

InkDrop retries searches over time and continues checking for new releases. An empty first search does not necessarily mean it will never find the item.

## Current status

InkDrop is still in beta.

I run this build every day against a library with thousands of books. The main track, search, download, verify, and import workflow is working, but there are still rough edges.

Known limitations:

- During a large import, the web interface may pause or respond slowly for a few seconds.
- Prowlarr, Soulseek, and MangaDex downloads have received the most testing. Other integrations may need more work.
- There is no release calendar yet. The available metadata sources do not provide dependable future release dates for enough titles to make one useful.
- Updates are currently manual. When a new build is released, you will need to pull or replace the container image.

Please report anything that does not behave as expected, especially:

- An issue or volume InkDrop should have found
- A bad series or issue match
- A file imported into the wrong folder
- A broken archive that was not rejected
- A page or setting that was difficult to understand

## Reporting a problem

Open an issue in this repository and include:

- The series and issue or volume involved
- What you expected InkDrop to do
- What happened instead
- The related entry from the **History** page
- A screenshot when the problem is visual

The History entry is usually more useful than a general log dump because it includes the decisions InkDrop made for that item.

## License

All rights reserved. This code is not licensed for reuse, modification, or redistribution.
