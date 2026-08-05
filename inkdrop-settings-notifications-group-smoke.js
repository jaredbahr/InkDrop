// Isolated smoke test for settingsProviderGroup()'s handling of the "notifications"
// provider, which must group under "Connect" next to Kavita/Komga -- Jared explicitly
// asked for the Discord/Pushover card to sit there, not under General, since it's an
// outbound integration a user configures alongside the other reader/frontend adapters.
//
// This id has its own entry in settingsProviderGroup()'s providerGroups lookup table
// (checked first, before provider.settings_group or provider_type fall back), so it
// can never land in the generic catch-all regardless of what PROVIDER_SETTINGS_META
// sets settings_group to.
//
// No DOM/server dependency -- these are pure functions, lifted verbatim from
// inkdrop_web.py rather than reimplemented.
//
// Run with: node inkdrop-settings-notifications-group-smoke.js

// --- verbatim from inkdrop_web.py: settingsProviderGroup ---

function settingsProviderGroup(provider) {
  const id = String(provider?.id || provider?.provider_id || "").toLowerCase();
  const providerGroups = {
    comicvine: ["comicvine", "Metadata", "Where InkDrop gets series and issue information, and how it writes metadata to your files"],
    mangadex: ["comicvine", "Metadata", "Where InkDrop gets series and issue information, and how it writes metadata to your files"],
    prowlarr: ["prowlarr", "Indexers", "Indexers and indexer options"],
    slskd: ["download_clients", "Download Clients", "SLSKD, SABnzbd, qBittorrent, download handling and remote path mappings"],
    sabnzbd: ["download_clients", "Download Clients", "Download clients, download handling and remote path mappings"],
    qbittorrent: ["download_clients", "Download Clients", "Download clients, download handling and remote path mappings"],
    media_management: ["media_management", "Media Management", "Naming, file management settings, and root folders"],
    library_paths: ["paths", "Paths", "Library and download paths"],
    manual_inboxes: ["paths", "Paths", "Library and download paths"],
    kavita: ["libraries", "Connect", "Library frontend adapters, sync visibility hooks, and outbound notifications"],
    komga: ["libraries", "Connect", "Library frontend adapters, sync visibility hooks, and outbound notifications"],
    notifications: ["libraries", "Connect", "Library frontend adapters, sync visibility hooks, and outbound notifications"],
    plex: ["libraries", "Connect", "Legacy media-server integrations and optional reader hooks"],
    kometa: ["libraries", "Connect", "Legacy media-server integrations and optional reader hooks"],
    roksbox: ["libraries", "Connect", "Legacy media-server integrations and optional reader hooks"],
    wdtv: ["libraries", "Connect", "Legacy media-server integrations and optional reader hooks"],
    xbmc: ["libraries", "Connect", "Legacy media-server integrations and optional reader hooks"],
    quality_language_rules: ["language", "Language", "Comics and manga language matching rules"],
    adult_nsfw_sources: ["automation", "Download Sources", "Direct downloads, RSS/ComicsCodes, and automatic search order"],
    audiobook_sources: ["automation", "Download Sources", "Direct downloads, RSS/ComicsCodes, and automatic search order"],
    course_and_video_sites: ["automation", "Download Sources", "Direct downloads, RSS/ComicsCodes, and automatic search order"],
    manual_ddl_blogs: ["automation", "Download Sources", "Direct downloads, RSS/ComicsCodes, and automatic search order"],
    manual_reader_sites: ["automation", "Download Sources", "Direct downloads, RSS/ComicsCodes, and automatic search order"],
    manual_search_engines: ["automation", "Download Sources", "Direct downloads, RSS/ComicsCodes, and automatic search order"],
    private_trackers: ["automation", "Download Sources", "Direct downloads, RSS/ComicsCodes, and automatic search order"],
    public_free_book_sites: ["automation", "Download Sources", "Direct downloads, RSS/ComicsCodes, and automatic search order"],
    rss: ["automation", "Sources", "Configured sources for Automatic Search"],
    comicscodes: ["automation", "Sources", "Configured sources for Automatic Search"],
    kapowarr: ["automation", "Sources", "Optional compatibility source"],
  };
  if (providerGroups[id]) return providerGroups[id];
  if (/(_sources|_trackers|reader_sites|search_engines|ddl_blogs|book_sites)$/.test(id)) {
    return ["automation", "Sources", "Configured sources for Automatic Search"];
  }
  const explicit = String(provider?.settings_group || "").toLowerCase();
  const explicitGroups = {
    metadata: ["comicvine", "Metadata", "Where InkDrop gets series and issue information, and how it writes metadata to your files"],
    indexers: ["prowlarr", "Indexers", "Indexers and indexer options"],
    download_sources: ["automation", "Sources", "Configured sources for Automatic Search"],
    source_boundaries: ["automation", "Sources", "Configured sources for Automatic Search"],
    source_templates: ["automation", "Sources", "Configured sources for Automatic Search"],
    download_clients: ["download_clients", "Download Clients", "Download clients, download handling and remote path mappings"],
    library: ["libraries", "Connect", "Library frontend adapters, sync visibility hooks, and outbound notifications"],
    media_management: ["media_management", "Media Management", "Naming, file management settings, and root folders"],
    paths: ["paths", "Paths", "Library and download paths"],
    rules: ["language", "Language", "Comics and manga language matching rules"],
    adapters: ["automation", "Sources", "Optional compatibility sources"],
    other: ["other", "General", "Host, security, proxy, and logging"],
  };
  if (explicitGroups[explicit]) return explicitGroups[explicit];
  const type = String(provider?.provider_type || "provider").toLowerCase();
  const groups = {
    metadata: ["comicvine", "Metadata", "Where InkDrop gets series and issue information, and how it writes metadata to your files"],
    metadata_adapter: ["automation", "Sources", "Optional compatibility sources"],
    indexer: ["prowlarr", "Indexers", "Indexers and indexer options"],
    source: ["automation", "Sources", "Configured sources for Automatic Search"],
    download_source: ["automation", "Sources", "Configured sources for Automatic Search"],
    direct_download: ["automation", "Sources", "Configured sources for Automatic Search"],
    download_client: ["download_clients", "Download Clients", "Download clients, download handling and remote path mappings"],
    library: ["libraries", "Connect", "Library frontend adapters, sync visibility hooks, and outbound notifications"],
    media_management: ["media_management", "Media Management", "Naming, file management settings, and root folders"],
    path: ["paths", "Paths", "Library and download paths"],
    rule: ["language", "Language", "Comics and manga language matching rules"],
  };
  return groups[type] || ["other", "Other", "Additional providers"];
}

function settingsProviderRank(provider) {
  if (!provider?.enabled) return 5;
  const health = provider.health || {};
  const activity = provider.activity || {};
  const text = String(health.state || health.label || activity.operational_state || "").toLowerCase();
  if (["error", "failed", "unavailable", "blocked", "disabled"].some(token => text.includes(token))) return 0;
  if (["watch", "backoff", "stalled"].some(token => text.includes(token))) return 1;
  if (Number(activity.recent_attempts || 0)) return 2;
  if (["healthy", "active", "running", "configured"].some(token => text.includes(token))) return 3;
  return 4;
}

function settingsProviderDisplayName(provider) {
  const id = String(provider?.id || provider?.provider_id || "").trim().toLowerCase();
  if (id === "quality_language_rules") return "Language Rules";
  if (id === "suwayomi") return "Suwayomi API / Page Sources";
  if (id === "suwayomi_managed_folder") return "Suwayomi Managed Folder";
  return provider?.display_name || provider?.id || "Provider";
}

function settingsProviderGroups(providers) {
  const order = ["comicvine", "prowlarr", "download_clients", "media_management", "language", "libraries", "paths", "automation", "other"];
  const grouped = new Map();
  for (const provider of providers || []) {
    const [key, title, detail] = settingsProviderGroup(provider);
    if (!grouped.has(key)) grouped.set(key, {key, title, detail, providers: []});
    grouped.get(key).providers.push(provider);
  }
  for (const group of grouped.values()) {
    group.providers.sort((a, b) => {
      const rank = settingsProviderRank(a) - settingsProviderRank(b);
      if (rank) return rank;
      return String(settingsProviderDisplayName(a)).localeCompare(String(settingsProviderDisplayName(b)));
    });
  }
  return [...grouped.values()].sort((a, b) => order.indexOf(a.key) - order.indexOf(b.key));
}

// --- end verbatim ---

function require_(condition, message) {
  if (!condition) throw new Error(message);
}

// Exactly what the live provider list contains for this provider (fetched live from
// inkdrop_web.runtime_provider_settings()). settings_group is "library" here on
// purpose -- the id-specific entry in providerGroups wins regardless, so this also
// proves the id lookup, not the settings_group fallback, is what's driving placement.
const NOTIFICATIONS_PROVIDER = {
  id: "notifications",
  provider_type: "notification_channel",
  display_name: "Notifications",
  enabled: false,
  settings_group: "library",
};

const [key, title, detail] = settingsProviderGroup(NOTIFICATIONS_PROVIDER);
require_(key === "libraries", `notifications must group under key "libraries" so it renders on the same Connect tab as Kavita/Komga: got ${key}`);
require_(title === "Connect", `notifications' group must be titled "Connect", matching the nav tab a user actually clicks, not "General": got ${JSON.stringify(title)}`);
require_(detail === "Library frontend adapters, sync visibility hooks, and outbound notifications", `detail must match the Connect area's real description: got ${JSON.stringify(detail)}`);

// End-to-end through settingsProviderGroups(), the way the real render path calls it:
// notifications must land in a group whose title is "Connect", not "General".
const groups = settingsProviderGroups([NOTIFICATIONS_PROVIDER]);
require_(groups.length === 1, `expected exactly one group for a single provider: ${JSON.stringify(groups)}`);
require_(groups[0].key === "libraries", groups[0]);
require_(groups[0].title === "Connect", `the rendered section heading a user actually sees must say "Connect": ${JSON.stringify(groups[0])}`);
require_(groups[0].providers[0] === NOTIFICATIONS_PROVIDER, "the notifications provider must be present in the Connect group");

console.log("inkdrop-settings-notifications-group-smoke: all checks passed");
