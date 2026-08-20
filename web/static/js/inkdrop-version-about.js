(function (global) {
  "use strict";

  var RELEASE_LIMITS = Object.freeze({
    version: 64,
    slug: 64,
    title: 100,
    summary: 280,
    highlights: 8,
    highlight: 200
  });
  var DETAILED_RELEASE_LIMIT = 10;
  var GITHUB_RELEASE_HISTORY_URL = "https://github.com/jaredbahr/InkDrop/releases";

  function publicRelease(release) {
    var highlights = Array.isArray(release.highlights) ? release.highlights.slice() : [];
    var fields = ["version", "slug", "released_at", "title", "summary"];
    fields.forEach(function (field) {
      if (!String(release[field] || "").trim()) throw new Error("Release " + field + " is required");
    });
    if (String(release.version).length > RELEASE_LIMITS.version) throw new Error("Release version is too long");
    if (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(release.slug) || release.slug.length > RELEASE_LIMITS.slug) {
      throw new Error("Release slug must be a stable lowercase identifier");
    }
    if (!/^\d{4}-\d{2}-\d{2}$/.test(release.released_at)) throw new Error("Release date must use YYYY-MM-DD");
    if (release.title.length > RELEASE_LIMITS.title) throw new Error("Release title is too long");
    if (release.summary.length > RELEASE_LIMITS.summary) throw new Error("Release summary is too long");
    if (!highlights.length || highlights.length > RELEASE_LIMITS.highlights) throw new Error("Release highlights are out of bounds");
    var historyUrl = String(release.history_url || "").trim();
    if (historyUrl && historyUrl !== GITHUB_RELEASE_HISTORY_URL) throw new Error("Release history URL is not allowed");
    highlights.forEach(function (highlight) {
      if (!String(highlight || "").trim() || String(highlight).length > RELEASE_LIMITS.highlight) {
        throw new Error("Release highlight is out of bounds");
      }
    });
    return Object.freeze({
      version: release.version,
      slug: release.slug,
      released_at: release.released_at,
      title: release.title,
      summary: release.summary,
      highlights: Object.freeze(highlights),
      compact: release.compact === true,
      history_url: historyUrl
    });
  }

  // Keep only the latest ten updates in the application. GitHub retains the
  // complete release history without adding old entries to every page load.
  var DETAILED_RELEASES = Object.freeze([
    publicRelease({
      version: "v0.1.12",
      slug: "v0-1-12",
      released_at: "2026-08-19",
      title: "Screens that were telling you things that weren't true",
      summary: "Mostly corrections. A few screens were confidently telling you things that weren't true — what got searched, what was actually downloading, what a backup brings back. They've stopped. If books have been stuck in Wanted or Queue for weeks, this one's worth taking.",
      highlights: [
        "Wanted now tells you why something hasn't arrived, instead of a catch-all \"no source found yet\"",
        "Five search bugs fixed — wrong issue numbers, wrong years, dropped volume markers",
        "Downloads no longer stick on \"Importing\" forever",
        "\"Actively processing\" now means something is actually happening",
        "Mobile home shows your series, instead of a download-client list that was counting wrong",
        "\"Compare & Merge\" is now just Compare — it won't merge anything on its own",
        "New report showing where your library and your database disagree",
        "Restoring a backup doesn't bring back your passwords — you'll re-enter those"
      ]
    }),
    publicRelease({
      version: "v0.1.11",
      slug: "v0-1-11",
      released_at: "2026-08-14",
      title: "Security and reliability follow-up to 0.1.10",
      summary: "Tightens the MangaDex download transport and CBR/RAR extraction, stops an admin path reveal succeeding when its access record cannot be written, and fixes an import guard that could quarantine a correct collected edition because of a temporary folder's name.",
      highlights: [
        "MangaDex downloads now use one bounded transport: embedded credentials and odd ports are refused, every redirect is re-checked, and the address connected to must match the one approved.",
        "CBR/RAR conversion reads each member's type and link target instead of just its name, so an archive cannot smuggle a symlink or device node past the path check and write outside its work folder.",
        "The admin-only \"show full paths\" control on Queue now refuses when the record of that access cannot be written, instead of returning the paths and reporting that it was recorded.",
        "API keys are stripped from cross-origin redirects based on where the value came from rather than a fixed list of header names, and scrubbed from error text in the form a query string encodes them.",
        "A file staged under a folder containing something like \"c3\" is no longer read as chapter 3 -- unit identity comes from the file and its release folder, not the download client's temporary path.",
        "Fixed a crash in duplicate-issue cleanup that could stop the background sweep when two workers disagreed about which record to keep.",
        "A completed Soulseek transfer from weeks ago is no longer replayed as if it just finished, and confirmed unambiguous rejections no longer go to Manual Review.",
        "A failed row action no longer stays on screen after a newer action has already succeeded, and System health findings now say what to do about them."
      ]
    }),
    publicRelease({
      version: "v0.1.10",
      slug: "v0-1-10",
      released_at: "2026-08-14",
      title: "Pull List, full-backup import, and a large matching and security pass",
      summary: "Adds Pull List, a full-backup import/restore workflow with a preview step, an Acquisition Reliability view with recovery controls, and Metron as a fallback metadata source. Also stops manga/comic classification reverting on every sync.",
      highlights: [
        "Manga and comic classification no longer reverts on every library sync -- it corrects itself now, and a manual correction sticks.",
        "Added Pull List, a week-boxed view of what's publishing for the series you follow, plus a lightweight mobile status view at /m.",
        "Added a full-backup import/restore workflow with a safety preview step; previewing a large library's backup dropped from about two minutes to about a second.",
        "Added an Acquisition Reliability view with per-item lifecycle tracking, and Recovery controls to retry through a different source, block a release, or reopen a stuck import.",
        "Prowlarr sends its API key in the header only by default now, keeping it out of proxy and access logs, and its health checks report real failures instead of swallowing them.",
        "Fixed queue items stuck on \"Importing\" after the transfer had already finished, and manga volume/chapter imports silently stalling in operator review.",
        "Fixed Edition Indifferent and the Monitored/Auto-Grab toggles turning themselves back on when a request sent \"false\" as a string instead of a real boolean.",
        "Fixed a duplicate-series merge leaving issue and collection records pointed at the wrong series, and added Metron as an optional fallback comic metadata source."
      ]
    }),
    publicRelease({
      version: "v0.1.09",
      slug: "v0-1-09",
      released_at: "2026-08-10",
      title: "New direct-download sources, scheduled backups, and more reliable SLSKD matching",
      summary: "Adds Pixeldrain, WeTransfer, and Buzzheavier as direct-download sources, scheduled full backups, and encrypted settings export. Also fixes SLSKD subseries matching, adds real Test-button feedback for every provider, and surfaces the real reason when a Settings restore fails.",
      highlights: [
        "SLSKD now rejects wrong-subseries matches before downloading instead of after, and recovery lanes get half of max_series instead of a third for faster backlog catch-up.",
        "Added Pixeldrain, WeTransfer, and Buzzheavier as direct-download sources, and fixed GetComics to Pixeldrain redirect resolution.",
        "Fixed Suwayomi's connection status being stuck on \"Unknown\" forever -- every provider's Test button now shows a real spinner and a pass/fail result tied to what was actually found.",
        "Added scheduled full backups with automatic retention, and Settings export/import can now carry encrypted credentials.",
        "Fixed Settings restore failures showing only a generic \"Bad Request\" -- the real reason (wrong passphrase, which setting failed) is now shown.",
        "Manual Review's Reject and Search Again actually retries now, and accepts exact unresolved manga matches with a real approve path.",
        "A verified collected trade now satisfies an individual issue want directly, and stale import claims auto-release after a timeout instead of blocking forever.",
        "Added an OPDS catalog discoverability panel to Settings, and Series pages now render through the same fast, virtualized approach used elsewhere in InkDrop."
      ]
    }),
    publicRelease({
      version: "v0.1.08",
      slug: "v0-1-08",
      released_at: "2026-08-07",
      title: "Undo a wrong match, fix manga units per series, and steadier imports",
      summary: "Mostly focused on search/import reliability, better troubleshooting when something goes wrong, and UI cleanup, including a way to correct a wrong match after import and fix a series' manga unit type individually.",
      highlights: [
        "Manga series that release as individual issues are no longer searched and imported as volumes -- InkDrop now checks the series itself instead of assuming based on the provider.",
        "Added a way to correct a wrong match after import: retract it, quarantine the file, and start a new search for the right one.",
        "Allow-and-retry on Blocklist no longer hangs waiting on an external API; blocked items now show the source filename, wanted issue, and rejection reason.",
        "Manual Review now shows the actual SLSKD filename, wanted issue, and rejection reason, with working approve/retry/delete and pagination for larger queues.",
        "The Test button for additional SLSKD instances now performs a real connection check instead of doing nothing.",
        "ComicInfo.xml now includes publisher information from ComicVine, backfilled across 2,417 existing archives.",
        "Suwayomi and MangaDex downloads are now correctly attributed in History instead of losing their source.",
        "Fixed a long-running import verification bug that could leave successfully imported files stuck for weeks."
      ]
    }),
    publicRelease({
      version: "v0.1.07",
      slug: "v0-1-07",
      released_at: "2026-08-05",
      title: "Acquisition, search, and importing get more reliable",
      summary: "This build focused mainly on making acquisition, search, and importing more reliable. It also includes a security pass, several performance improvements, and some lighter UI work.",
      highlights: [
        "Manual Search no longer fails across every provider at once — a locking issue meant one slow provider could block the other three from starting; providers now run independently again.",
        "\"Use this candidate\" in Manual Review now works for downloaded files instead of silently doing nothing, while still checking for corruption and duplicates.",
        "Fixed a cleanup crash that could break search, imports, and queue processing at the same time.",
        "Fixed an issue that could import a release into the wrong series when two MangaDex titles shared an alias or creator credit.",
        "Fixed Roman numeral parsing, unnecessary search cooldowns, and several causes of stuck or silently failed downloads, including a new 48-hour Soulseek timeout.",
        "Search matching is more accurate, and several import problems (ordering, stuck-in-queue, duplicate imports, multi-series packs) are fixed.",
        "Security improvements: provider credentials are no longer written to persistent storage, and a script-injection issue in search-result data has been fixed.",
        "Wanted, Queue, History, Blocklist, and Manual Review now use a new page-rendering system — pagination is noticeably faster on larger libraries."
      ]
    }),
    publicRelease({
      version: "v0.1.06",
      slug: "v0-1-06",
      released_at: "2026-08-02",
      title: "Notifications become a real system, and SLSKD gets smarter searches",
      summary: "Notifications now support per-channel event triggers, series scoping, quiet hours, and delivery history. SLSKD searches use better terms and more patience, several stuck-download patterns are fixed, and provider secrets no longer leak into diagnostics.",
      highlights: [
        "Notifications are a real system now: per-channel event triggers, series scoping, quiet hours, delivery history, and test buttons for Discord and Pushover.",
        "SLSKD searches no longer waste queries on literal \"cbz\"/\"cbr\" keywords or miss singular/plural title variants, and get more time before assuming a timeout.",
        "Fixed several stuck-download patterns: repeat-reject loops, permanent single-timeout blocks, and dead-end searches that only turn up already-rejected results.",
        "Fixed downloads that were grabbed but never finished landing in your library.",
        "Rate-limited or temporarily unavailable sources no longer get mislabeled as failed transfers.",
        "The \"item imported\" notification no longer repeats for the same file on every re-check.",
        "Provider API keys and webhook tokens no longer show up in error messages or diagnostic output.",
        "Recover Missing's tiles no longer overlap, Search All's scope is clearer, and SLSKD's default per-user transfer cap was raised."
      ]
    }),
    publicRelease({
      version: "v0.1.05",
      slug: "v0-1-05",
      released_at: "2026-08-02",
      title: "Comic one-shots stop getting rejected, and series can move library folders",
      summary: "A large batch of acquisition and UI fixes. Comic one-shots and graphic novels no longer get rejected at import, oversized packs go to Manual Review instead of auto-grabbing, and a series content type and library folder can now be changed after creation.",
      highlights: [
        "Packs over a configurable size limit now go to Manual Review instead of being auto-grabbed.",
        "Fixed comic one-shots and graphic novels getting permanently rejected at import.",
        "Fixed a ComicsCodes health-check bug that could get the source stuck instead of simply marking it unhealthy.",
        "You can now change a series' content type and library root folder after it's created.",
        "SLSKD searches no longer waste early attempts on a redundant qualifier, and no longer leak filename text into queries through series aliases.",
        "SLSKD can now recognize and convert raw page-image folders into a CBZ during import.",
        "The History page supports searching by series title and no longer repeats duplicate entries.",
        "Recover Missing, Attempts, and the SLSKD/download-client Settings cards all got clarity and usability fixes this build."
      ]
    }),
    publicRelease({
      version: "v0.1.04",
      slug: "v0-1-04",
      released_at: "2026-08-02",
      title: "SLSKD stops crying wolf, and a wrong print-run stops auto-grabbing",
      summary: "Fixed a bug where finished SLSKD searches could be wrongly logged and retried as timed out, likely the cause of \"SLSKD isn't working\" reports. Also fixes a wrong-volume auto-grab bug and a queue bug that excluded some series from search.",
      highlights: [
        "Fixed finished SLSKD searches sometimes being wrongly logged and retried as timed out.",
        "Fixed candidates with the wrong volume or print run getting auto-grabbed as a safe match; unclear cases now go to Manual Review.",
        "Fixed a queue bug that could permanently exclude some series from SLSKD search, and a stalled RSS discovery check.",
        "Fixed a wrong-series match retrying endlessly instead of stopping after the first rejection.",
        "SLSKD, Prowlarr, Torznab, and Newznab now try more results on a genuine zero-result search instead of waiting for the next pass.",
        "Manual Review no longer hides legacy decisions, and supports bulk-ignore.",
        "Fixed several qBittorrent and download-client bugs, including one that could wrongly blacklist a release.",
        "Smaller fixes: faster status indicator, a clearer Queue wait panel, and automatic search on by default for new installs."
      ]
    }),
    publicRelease({
      version: "v0.1.03",
      slug: "v0-1-03",
      released_at: "2026-08-02",
      title: "Search tries the right title, and the System page stops hanging",
      summary: "Search now tries the title people actually share files under instead of burning its budget on one nobody uses, and a real print-run marker on the exact issue you wanted no longer gets rejected. The System page also stops hanging if a request stalls.",
      highlights: [
        "Searches for licensed creator-credit titles (like \"Naoki Urasawa's Monster\") now try the title people actually share files under, instead of spending the whole budget on one nobody uses.",
        "A volume/print-run marker like \"v1 #19\" no longer gets treated as a mismatch when it's exactly the issue you wanted — that was silently blocking real grabs.",
        "The System page no longer hangs on a silent \"Loading...\" if a request stalls; you'll see what failed instead."
      ]
    })
  ]);

  var PUBLIC_RELEASES = Object.freeze(DETAILED_RELEASES.slice(0, DETAILED_RELEASE_LIMIT));

  function validateCatalog(catalog) {
    var seenVersions = new Set();
    var seenSlugs = new Set();
    var previousDate = "9999-99-99";
    catalog.forEach(function (release) {
      if (seenVersions.has(release.version) || seenSlugs.has(release.slug)) throw new Error("Release versions and slugs must be unique");
      if (release.released_at > previousDate) throw new Error("Release catalog must be newest first");
      seenVersions.add(release.version);
      seenSlugs.add(release.slug);
      previousDate = release.released_at;
    });
    return catalog;
  }

  function releaseHistorySummary(count) {
    var visibleCount = Math.max(0, Math.min(DETAILED_RELEASE_LIMIT, Math.floor(Number(count) || 0)));
    if (!visibleCount) return "No recent updates are shown here. Older release notes remain available on GitHub.";
    if (visibleCount === 1) return "The latest update is shown here. Older release notes remain available on GitHub.";
    return "The latest " + visibleCount + " updates are shown here. Older release notes remain available on GitHub.";
  }

  validateCatalog(PUBLIC_RELEASES);

  function text(value, fallback) {
    var result = String(value === undefined || value === null ? "" : value).trim();
    return result || fallback || "";
  }

  function displayVersion(metadata) {
    var explicit = text(metadata.display_version);
    var version = text(metadata.version, "dev");
    var shortSha = text(metadata.short_commit_sha);
    var development = metadata.development === true || text(metadata.release_channel).toLowerCase() === "dev";
    var base = explicit || version;
    if (development && shortSha && base.indexOf(shortSha) < 0) return base + "+" + shortSha;
    return base;
  }

  var PRERELEASE_STAGES = Object.freeze({
    alpha: { label: "Closed Alpha", stage: "Closed alpha · not publicly launched" },
    beta: { label: "Beta", stage: "Public beta" }
  });

  // Three shapes, newest first. Current releases are the bare number: 0.1.02.
  // Before that the counter sat in the patch slot with a stage suffix
  // (0.1.01-beta), and before that it trailed the stage (0.1.0-alpha.98).
  // Both older forms are still parsed so historical entries and deep links
  // keep rendering.
  function closedAlphaParts(value) {
    var raw = text(value);
    var trailing = /^v?(\d+)\.(\d+)\.(\d+)-(alpha|beta)\.(\d+)$/i.exec(raw);
    if (trailing) {
      return {
        major: Number(trailing[1]),
        minor: Number(trailing[2]),
        patch: Number(trailing[3]),
        prerelease: trailing[4].toLowerCase(),
        update: Number(trailing[5]),
        counterInPatch: false,
        patchText: trailing[3]
      };
    }
    var inPatch = /^v?(\d+)\.(\d+)\.(\d+)-(alpha|beta)$/i.exec(raw);
    if (inPatch) {
      return {
        major: Number(inPatch[1]),
        minor: Number(inPatch[2]),
        patch: Number(inPatch[3]),
        prerelease: inPatch[4].toLowerCase(),
        update: Number(inPatch[3]),
        counterInPatch: true,
        // Kept as written so 0.1.01-beta does not render as 0.1.1-beta.
        patchText: inPatch[3]
      };
    }
    // Releases from 0.1.02 on carry no stage suffix at all: the version is just
    // the number. Parsed explicitly rather than left to fall through, because
    // the unparsed path renders the string but reports no stage, which would
    // put the raw channel ("qa") on the About page where a label belongs.
    var plain = /^v?(\d+)\.(\d+)\.(\d+)$/.exec(raw);
    if (!plain) return null;
    return {
      major: Number(plain[1]),
      minor: Number(plain[2]),
      patch: Number(plain[3]),
      prerelease: null,
      update: Number(plain[3]),
      counterInPatch: true,
      // Kept as written so 0.1.02 does not render as 0.1.2.
      patchText: plain[3]
    };
  }

  function productVersionLabel(value) {
    var parts = closedAlphaParts(value && typeof value === "object" ? displayVersion(value) : value);
    if (!parts) return text(value && typeof value === "object" ? displayVersion(value) : value, "Development build");
    // No suffix means no stage word: the version stands on its own.
    if (!parts.prerelease) {
      return parts.major + "." + parts.minor + "." + parts.patchText;
    }
    var stage = PRERELEASE_STAGES[parts.prerelease] || PRERELEASE_STAGES.alpha;
    if (parts.counterInPatch) {
      return parts.major + "." + parts.minor + "." + parts.patchText + " " + stage.label;
    }
    var patch = parts.patch ? "." + parts.patchText : "";
    return parts.major + "." + parts.minor + patch + " " + stage.label + " · Update " + parts.update;
  }

  function releaseStageLabel(metadata) {
    metadata = metadata && typeof metadata === "object" ? metadata : {};
    var parts = closedAlphaParts(displayVersion(metadata));
    if (parts && parts.prerelease) return (PRERELEASE_STAGES[parts.prerelease] || PRERELEASE_STAGES.alpha).stage;
    if (parts) return "Release";
    return text(metadata.release_channel || metadata.channel, "Development");
  }

  function releaseFromHash(hashValue) {
    var raw = String(hashValue === undefined ? global.location?.hash || "" : hashValue || "");
    var queryIndex = raw.indexOf("?");
    if (queryIndex < 0) return "";
    return String(new URLSearchParams(raw.slice(queryIndex + 1)).get("release") || "").trim();
  }

  function canonicalReleaseHref(version) {
    return "#system?area=about&release=" + encodeURIComponent(String(version || "").trim());
  }

  function setReleaseExpanded(entry, button, panel, expanded) {
    button.setAttribute("aria-expanded", expanded ? "true" : "false");
    button.textContent = expanded ? "Hide notes" : "Show notes";
    panel.hidden = !expanded;
    entry.classList.toggle("expanded", expanded);
  }

  function renderReleases(container, options) {
    if (!container) return null;
    options = options || {};
    var sourceCatalog = Array.isArray(options.catalog) ? options.catalog : PUBLIC_RELEASES;
    var catalog = validateCatalog(sourceCatalog.map(publicRelease)).slice(0, DETAILED_RELEASE_LIMIT);
    var requested = String(options.releaseVersion || releaseFromHash(options.hash)).trim();
    var selectedIndex = catalog.findIndex(function (release) { return release.version === requested; });
    if (selectedIndex < 0) selectedIndex = 0;
    container.replaceChildren();
    container.classList.add("inkdrop-release-notes");

    var heading = document.createElement("div");
    heading.className = "inkdrop-release-notes-heading";
    var title = document.createElement("h3");
    title.textContent = "Release history";
    var detail = document.createElement("p");
    detail.textContent = releaseHistorySummary(catalog.length);
    var fullHistory = document.createElement("a");
    fullHistory.href = GITHUB_RELEASE_HISTORY_URL;
    fullHistory.textContent = "Full release history on GitHub";
    fullHistory.target = "_blank";
    fullHistory.rel = "noreferrer";
    heading.append(title, detail, fullHistory);
    container.appendChild(heading);

    catalog.forEach(function (release, index) {
      var entry = document.createElement("article");
      entry.className = "inkdrop-release-entry" + (release.compact ? " compact" : "");
      entry.dataset.releaseKind = release.compact ? "rollup" : "detailed";
      entry.id = "inkdrop-release-" + release.slug;
      var header = document.createElement("header");
      var identity = document.createElement("div");
      identity.className = "inkdrop-release-identity";
      var versionHeading = document.createElement("h4");
      var versionLink = document.createElement("a");
      versionLink.href = canonicalReleaseHref(release.version);
      versionLink.textContent = release.compact ? release.title : productVersionLabel(release.version);
      versionLink.setAttribute("aria-label", "Permanent link to release notes for " + release.version);
      versionHeading.appendChild(versionLink);
      var technicalVersion = document.createElement("code");
      technicalVersion.className = "inkdrop-release-build-id";
      technicalVersion.textContent = release.version;
      var releaseTitle = document.createElement("strong");
      releaseTitle.textContent = release.title;
      var releaseDate = document.createElement("time");
      releaseDate.dateTime = release.released_at;
      releaseDate.textContent = release.released_at;
      if (release.compact) identity.append(versionHeading, releaseDate);
      else identity.append(versionHeading, technicalVersion, releaseTitle, releaseDate);

      var panelId = "inkdrop-release-notes-" + release.slug;
      var toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "inkdrop-release-toggle";
      toggle.setAttribute("aria-controls", panelId);
      var panel = document.createElement("div");
      panel.id = panelId;
      panel.className = "inkdrop-release-body";
      var summary = document.createElement("p");
      summary.textContent = release.summary;
      var highlights = document.createElement("ul");
      release.highlights.forEach(function (highlight) {
        var item = document.createElement("li");
        item.textContent = highlight;
        highlights.appendChild(item);
      });
      panel.append(summary, highlights);
      if (release.history_url) {
        var historyLink = document.createElement("a");
        historyLink.href = release.history_url;
        historyLink.textContent = "View these releases on GitHub";
        historyLink.target = "_blank";
        historyLink.rel = "noreferrer";
        panel.appendChild(historyLink);
      }
      header.append(identity, toggle);
      entry.append(header, panel);
      setReleaseExpanded(entry, toggle, panel, index === selectedIndex);
      toggle.addEventListener("click", function () {
        setReleaseExpanded(entry, toggle, panel, toggle.getAttribute("aria-expanded") !== "true");
      });
      container.appendChild(entry);
    });
    return catalog;
  }

  function row(label, value, detail) {
    var item = document.createElement("div");
    item.className = "inkdrop-about-row";
    var name = document.createElement("strong");
    name.textContent = label;
    var content = document.createElement("span");
    content.textContent = value;
    item.append(name, content);
    if (detail) {
      var description = document.createElement("small");
      description.textContent = detail;
      item.append(description);
    }
    return item;
  }

  function copyableRow(label, value, detail) {
    var item = row(label, value, detail);
    var content = item.querySelector("span");
    if (!content || !value) return item;
    var button = document.createElement("button");
    button.type = "button";
    button.className = "inkdrop-about-copy";
    button.textContent = "Copy";
    button.setAttribute("aria-label", "Copy " + label.toLowerCase());
    button.addEventListener("click", function () {
      if (global.navigator?.clipboard?.writeText) global.navigator.clipboard.writeText(String(value));
    });
    content.after(button);
    return item;
  }

  function render(container, metadata) {
    if (!container) return null;
    metadata = metadata && typeof metadata === "object" ? metadata : {};
    container.replaceChildren();
    container.classList.add("inkdrop-about-version");
    var rows = [
      row("Version", productVersionLabel(metadata), "Product version"),
      metadata.qa_build_number !== undefined && metadata.qa_build_number !== null && text(metadata.qa_build_number)
        ? row("QA Build", text(metadata.qa_build_number), "QA candidate build number")
        : null,
      copyableRow("Commit", text(metadata.short_commit_sha || metadata.commit_sha, "unknown"), "Source revision"),
      row("Built", text(metadata.build_date, "unknown"), "Build date"),
      metadata.image_digest || metadata.digest
        ? copyableRow("Image digest", text(metadata.image_digest || metadata.digest), "Container image digest")
        : null
    ].filter(Boolean);
    container.append.apply(container, rows);
    return metadata;
  }

  async function mount(container, options) {
    options = options || {};
    if (options.metadata) return render(container, options.metadata);
    var fetchImpl = options.fetch || global.fetch;
    if (typeof fetchImpl !== "function") throw new Error("A fetch implementation is required");
    container.setAttribute("aria-busy", "true");
    try {
      var response = await fetchImpl(options.endpoint || "/api/system/version", { headers: { Accept: "application/json" } });
      if (!response || !response.ok) throw new Error("Version metadata request failed");
      return render(container, await response.json());
    } finally {
      container.removeAttribute("aria-busy");
    }
  }

  global.InkDropVersionAbout = Object.freeze({
    canonicalReleaseHref: canonicalReleaseHref,
    displayVersion: displayVersion,
    productVersionLabel: productVersionLabel,
    releaseStageLabel: releaseStageLabel,
    detailedReleaseLimit: DETAILED_RELEASE_LIMIT,
    releaseHistoryUrl: GITHUB_RELEASE_HISTORY_URL,
    publicReleases: PUBLIC_RELEASES,
    render: render,
    renderReleases: renderReleases,
    releaseFromHash: releaseFromHash,
    mount: mount
  });
  if (typeof global.dispatchEvent === "function" && typeof global.Event === "function") {
    global.dispatchEvent(new global.Event("inkdrop-version-about-ready"));
  }
})(typeof window !== "undefined" ? window : globalThis);
