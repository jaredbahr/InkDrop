(function (global) {
  "use strict";

  var STORAGE_KEY = "inkdrop.operationalTable.preferences.v1";
  var DEFAULTS = Object.freeze({
    density: "compact",
    thumbnails: true,
    detailsOpen: false,
    pageSize: 24,
    columns: [],
  });

  function copyDefaults() {
    return {
      density: DEFAULTS.density,
      thumbnails: DEFAULTS.thumbnails,
      detailsOpen: DEFAULTS.detailsOpen,
      pageSize: DEFAULTS.pageSize,
      columns: DEFAULTS.columns.slice(),
    };
  }

  function normalize(value) {
    var input = value && typeof value === "object" ? value : {};
    var pageSize = Number(input.pageSize);
    return {
      density: input.density === "detailed" ? "detailed" : DEFAULTS.density,
      thumbnails: input.thumbnails !== false,
      detailsOpen: input.detailsOpen === true,
      pageSize:
        [24, 50, 100, 250].indexOf(pageSize) >= 0
          ? pageSize
          : DEFAULTS.pageSize,
      columns: Array.isArray(input.columns)
        ? input.columns.filter(function (column) {
            return typeof column === "string" && column.length > 0;
          })
        : DEFAULTS.columns.slice(),
    };
  }

  function storageFor(candidate) {
    if (candidate) return candidate;
    try {
      return global.localStorage;
    } catch (_error) {
      return null;
    }
  }

  function load(storage) {
    var target = storageFor(storage);
    if (!target) return copyDefaults();
    try {
      return normalize(JSON.parse(target.getItem(STORAGE_KEY) || "{}"));
    } catch (_error) {
      return copyDefaults();
    }
  }

  function save(preferences, storage) {
    var value = normalize(preferences);
    var target = storageFor(storage);
    if (target) {
      try {
        target.setItem(STORAGE_KEY, JSON.stringify(value));
      } catch (_error) {}
    }
    return value;
  }

  function reset(storage) {
    var target = storageFor(storage);
    if (target) {
      try {
        target.removeItem(STORAGE_KEY);
      } catch (_error) {}
    }
    return copyDefaults();
  }

  function apply(root, preferences) {
    if (!root) return normalize(preferences);
    var value = normalize(preferences);
    root.dataset.tableDensity = value.density;
    root.dataset.tableThumbnails = value.thumbnails ? "shown" : "hidden";
    root.dataset.tableDetails = value.detailsOpen ? "open" : "closed";
    root.dataset.tablePageSize = String(value.pageSize);

    root.querySelectorAll("[data-table-column]").forEach(function (element) {
      var name = element.dataset.tableColumn;
      element.hidden =
        value.columns.length > 0 && value.columns.indexOf(name) < 0;
    });
    root
      .querySelectorAll("details[data-operational-details]")
      .forEach(function (element) {
        element.open = value.detailsOpen;
      });
    return value;
  }

  function update(patch, storage) {
    return save(Object.assign(load(storage), patch || {}), storage);
  }

  global.InkDropOperationalTablePreferences = Object.freeze({
    defaults: copyDefaults,
    load: load,
    save: save,
    reset: reset,
    apply: apply,
    update: update,
    storageKey: STORAGE_KEY,
  });
})(typeof window !== "undefined" ? window : globalThis);
