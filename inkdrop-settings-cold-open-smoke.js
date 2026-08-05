// Isolated smoke test for the Settings cold-open default-area logic added to
// inkdrop_web.py (readSettingsLastArea / persistSettingsLastArea /
// settingsDefaultColdOpenArea). No DOM/server dependency -- these are pure
// functions once localStorage is mocked, so this runs the exact same source
// lines lifted verbatim from inkdrop_web.py rather than reimplementing them.
//
// Run with: node inkdrop-settings-cold-open-smoke.js

class FakeStorage {
  constructor() { this.map = new Map(); }
  getItem(key) { return this.map.has(key) ? this.map.get(key) : null; }
  setItem(key, value) { this.map.set(key, String(value)); }
  removeItem(key) { this.map.delete(key); }
  clear() { this.map.clear(); }
}

const window = { localStorage: new FakeStorage() };

// --- verbatim from inkdrop_web.py ---

const SETTINGS_LAST_AREA_KEY = "inkdrop.settingsLastArea";
const SETTINGS_DEFAULT_AREA = "media_management";

function settingsGroupFromRouteArea(areaValue="") {
  const key = String(areaValue || "").trim().toLowerCase().replace(/-/g, "_");
  const aliases = {
    setup: "setup",
    first_run: "setup",
    first_run_setup: "setup",
    first_run_setup_flow: "setup",
    initial_setup: "setup",
    onboarding: "setup",
    indexer: "prowlarr",
    indexers: "prowlarr",
    prowlarr: "prowlarr",
    profile: "language",
    profiles: "language",
    language_profile: "language",
    language_profiles: "language",
    media: "media_management",
    media_management: "media_management",
    management: "media_management",
    download_client: "download_clients",
    download_clients: "download_clients",
    downloadclients: "download_clients",
    clients: "download_clients",
    download: "download_clients",
    sab: "download_clients",
    sabnzbd: "download_clients",
    qbit: "download_clients",
    qbittorrent: "download_clients",
    import_list: "import_lists",
    import_lists: "import_lists",
    comicvine: "comicvine",
    comic_vine: "comicvine",
    metadata: "comicvine",
    metadata_source: "comicvine",
    metadata_file: "metadata_files",
    metadata_files: "metadata_files",
    metadata_export: "metadata_files",
    metadata_exports: "metadata_files",
    slskd: "download_clients",
    soulseek: "download_clients",
    library: "libraries",
    libraries: "libraries",
    library_adapter: "libraries",
    library_adapters: "libraries",
    library_frontend: "libraries",
    library_frontends: "libraries",
    frontend: "libraries",
    frontends: "libraries",
    frontend_adapter: "libraries",
    frontend_adapters: "libraries",
    connect: "libraries",
    kavita: "libraries",
    komga: "libraries",
    paths: "paths",
    path: "paths",
    root_folder: "paths",
    root_folders: "paths",
    quality: "language",
    language: "language",
    rules: "language",
    general: "other",
    host: "other",
    tag: "other",
    tags: "other",
    ui: "ui",
    automation: "automation",
    download_source: "automation",
    download_sources: "automation",
    direct_download: "automation",
    direct_downloads: "automation",
    source_order: "automation",
    sources: "automation",
  };
  return aliases[key] || key;
}

const SETTINGS_KNOWN_AREAS = new Set([
  "setup", "media_management", "language", "prowlarr", "download_clients",
  "import_lists", "libraries", "metadata_files", "comicvine", "other", "ui",
  "paths", "automation",
]);

function readSettingsLastArea() {
  let stored = "";
  try {
    stored = window.localStorage?.getItem(SETTINGS_LAST_AREA_KEY) || "";
  } catch (_) {
    stored = "";
  }
  const key = settingsGroupFromRouteArea(stored) || "";
  return SETTINGS_KNOWN_AREAS.has(key) ? key : "";
}

function persistSettingsLastArea(area) {
  const key = settingsGroupFromRouteArea(area || "");
  if (!key) return;
  try {
    window.localStorage?.setItem(SETTINGS_LAST_AREA_KEY, key);
  } catch (_) {
    // Best effort; a user with storage disabled just keeps the built-in default.
  }
}

function settingsDefaultColdOpenArea() {
  return readSettingsLastArea() || SETTINGS_DEFAULT_AREA;
}

// --- test harness ---

let failures = 0;
function check(label, actual, expected) {
  const ok = actual === expected;
  if (!ok) {
    failures += 1;
    console.error(`FAIL ${label}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  } else {
    console.log(`ok   ${label}`);
  }
}

// 1. Empty storage on a first-ever open falls back to media_management.
window.localStorage.clear();
check("empty storage -> default area", settingsDefaultColdOpenArea(), "media_management");

// 2. A previously-visited area wins over the default.
window.localStorage.clear();
persistSettingsLastArea("download_clients");
check("persisted area -> that area", settingsDefaultColdOpenArea(), "download_clients");

// 3. persistSettingsLastArea normalizes aliases before storing (e.g. "slskd" -> "download_clients").
window.localStorage.clear();
persistSettingsLastArea("slskd");
check("alias normalized on persist", window.localStorage.getItem(SETTINGS_LAST_AREA_KEY), "download_clients");

// 4. A stale/unrecognized value left over in storage (e.g. a removed area
// name from an old build) does not get treated as a valid area -- falls
// back to the default instead of resolving to garbage.
window.localStorage.clear();
window.localStorage.setItem(SETTINGS_LAST_AREA_KEY, "some_removed_area");
check("unknown stored area -> default area", settingsDefaultColdOpenArea(), "media_management");

// 5. persistSettingsLastArea silently no-ops on an empty/garbage-only area
// rather than clobbering an existing valid persisted value with junk.
window.localStorage.clear();
persistSettingsLastArea("prowlarr");
persistSettingsLastArea("");
check("persist no-op on empty area keeps prior value", settingsDefaultColdOpenArea(), "prowlarr");

// 6. localStorage throwing (private-browsing / storage disabled) degrades
// to the built-in default instead of throwing out of the settings loader.
const throwingStorage = {
  getItem() { throw new Error("storage disabled"); },
  setItem() { throw new Error("storage disabled"); },
};
const savedStorage = window.localStorage;
window.localStorage = throwingStorage;
check("storage throws -> default area", settingsDefaultColdOpenArea(), "media_management");
window.localStorage = savedStorage;

if (failures > 0) {
  console.error(`\n${failures} failure(s)`);
  process.exit(1);
}
console.log("\nall checks passed");
