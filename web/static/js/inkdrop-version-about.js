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
    var catalog = validateCatalog((options.catalog || PUBLIC_RELEASES).map(publicRelease));
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
    detail.textContent = "The latest 10 updates are shown here. Older release notes remain available on GitHub.";
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
      row("Product", text(metadata.product, "InkDrop"), "Application name"),
      row("Version", productVersionLabel(metadata), "Product version"),
      row("Build ID", displayVersion(metadata), "Machine-readable SemVer"),
      metadata.qa_build_number !== undefined && metadata.qa_build_number !== null && text(metadata.qa_build_number)
        ? row("QA Build", text(metadata.qa_build_number), "QA candidate build number")
        : null,
      row("Channel", releaseStageLabel(metadata), "Release stage"),
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
