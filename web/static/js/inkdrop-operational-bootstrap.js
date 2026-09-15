(function (global) {
  "use strict";

  var ASSET_ORDER = Object.freeze([
    "inkdrop-operational-preferences.js",
    "inkdrop-operational-table-controls.js",
    "inkdrop-operational-query-controls.js",
    "inkdrop-operational-row-controls.js",
    "inkdrop-transfer-telemetry.js",
    "inkdrop-version-about.js",
    "inkdrop-operational-bootstrap.js"
  ]);

  var ASSET_GLOBALS = Object.freeze({
    "inkdrop-operational-preferences.js": "InkDropOperationalTablePreferences",
    "inkdrop-operational-table-controls.js": "InkDropOperationalTableControls",
    "inkdrop-operational-query-controls.js": "InkDropOperationalQueryControls",
    "inkdrop-operational-row-controls.js": "InkDropOperationalRowControls",
    "inkdrop-transfer-telemetry.js": "InkDropTransferTelemetry",
    "inkdrop-version-about.js": "InkDropVersionAbout",
    "inkdrop-operational-bootstrap.js": "InkDropOperationalBootstrap"
  });

  function asRoot(root) {
    return root || global.document;
  }

  function asDocument(documentOverride) {
    return documentOverride || global.document;
  }

  function normalizeBaseUrl(baseUrl) {
    var value = baseUrl || "/static/js/";
    return value.endsWith("/") ? value : value + "/";
  }

  function assetGlobalName(assetName) {
    return ASSET_GLOBALS[assetName] || "";
  }

  function assetLoaded(documentRef, assetName) {
    var apiName = assetGlobalName(assetName);
    if (apiName && global[apiName]) return true;
    if (!documentRef?.querySelectorAll) return false;
    return Array.from(documentRef.querySelectorAll("script[src]")).some(function (script) {
      var src = script.getAttribute("src") || script.src || "";
      src = src.split(/[?#]/)[0];
      return src.endsWith("/" + assetName) || src.endsWith(assetName);
    });
  }

  function appendScript(documentRef, assetName, options) {
    return new Promise(function (resolve, reject) {
      if (!documentRef?.createElement) {
        reject(new Error("A document with createElement is required to load InkDrop UI assets"));
        return;
      }
      var script = documentRef.createElement("script");
      script.src = normalizeBaseUrl(options.baseUrl) + assetName;
      script.async = false;
      if (options.defer !== false) script.defer = true;
      if (options.nonce) script.nonce = options.nonce;
      script.onload = function () { resolve({ asset: assetName, state: "loaded" }); };
      script.onerror = function () { reject(new Error("Failed to load " + assetName)); };
      var parent = documentRef.head || documentRef.body || documentRef.documentElement;
      if (!parent?.appendChild && !parent?.append) {
        reject(new Error("A document head or body is required to load InkDrop UI assets"));
        return;
      }
      if (parent.appendChild) parent.appendChild(script);
      else parent.append(script);
    });
  }

  // Every asset is appended now, and they execute in order regardless.
  //
  // This used to chain: append one, wait for its onload, append the next. Six
  // assets meant six sequential round trips before the last one even started
  // downloading, on a page that had already spent its budget getting here.
  // Appending them together lets the browser fetch all six at once;
  // appendScript sets script.async = false, which is exactly the flag that
  // makes dynamically inserted scripts still execute in insertion order, so the
  // ordering ASSET_ORDER encodes is preserved by the DOM rather than by the
  // promise chain.
  //
  // allSettled rather than all: a failure used to abandon every asset after it
  // in the chain. Now one that fails takes only itself out, the rest still
  // load, and the returned promise still rejects so an existing caller's catch
  // behaves as it did.
  function loadAssets(options) {
    options = options || {};
    var documentRef = asDocument(options.document);
    var assets = (options.assets || ASSET_ORDER).filter(function (name) {
      return name !== "inkdrop-operational-bootstrap.js";
    });
    var summary = { loaded: [], skipped: [] };
    var pending = [];
    assets.forEach(function (assetName) {
      if (assetLoaded(documentRef, assetName)) {
        summary.skipped.push(assetName);
        return;
      }
      pending.push(
        appendScript(documentRef, assetName, options).then(
          function () { return { asset: assetName, ok: true }; },
          function (error) { return { asset: assetName, ok: false, error: error }; }
        )
      );
    });
    return Promise.all(pending).then(function (results) {
      var failures = [];
      results.forEach(function (result) {
        if (result.ok) summary.loaded.push(result.asset);
        else failures.push(result.error || new Error("Failed to load " + result.asset));
      });
      // Reported in ASSET_ORDER rather than in completion order, so the summary
      // says what was loaded and not which happened to finish downloading first.
      summary.loaded.sort(function (left, right) {
        return ASSET_ORDER.indexOf(left) - ASSET_ORDER.indexOf(right);
      });
      if (failures.length) throw failures[0];
      return summary;
    });
  }

  function parseJson(value, fallback) {
    if (!value) return fallback || {};
    try { return JSON.parse(value); }
    catch (_error) { return fallback || {}; }
  }

  function once(node, key, mount) {
    if (!node || node.dataset[key] === "mounted") return null;
    node.dataset[key] = "mounted";
    return mount(node);
  }

  function requireApi(name) {
    var api = global[name];
    if (!api) throw new Error(name + " is required");
    return api;
  }

  function mountTableControls(root, adapters) {
    var api = requireApi("InkDropOperationalTableControls");
    return Array.from(root.querySelectorAll("[data-inkdrop-table-controls]")).map(function (node) {
      return once(node, "inkdropTableControlsMounted", function () {
        var tableSelector = node.getAttribute("data-table");
        var statusSelector = node.getAttribute("data-status");
        return api.mount(Object.assign({}, adapters.tableControls || {}, parseJson(node.getAttribute("data-config")), {
          root: node,
          table: tableSelector ? root.querySelector(tableSelector) : node.closest("[data-inkdrop-operational-region]")?.querySelector("table"),
          status: statusSelector ? root.querySelector(statusSelector) : undefined
        }));
      });
    });
  }

  function mountQueryControls(root, adapters) {
    var api = requireApi("InkDropOperationalQueryControls");
    return Array.from(root.querySelectorAll("[data-inkdrop-query-controls]")).map(function (node) {
      return once(node, "inkdropQueryControlsMounted", function () {
        var resetSelector = node.getAttribute("data-reset");
        return api.mount(Object.assign({}, adapters.queryControls || {}, parseJson(node.getAttribute("data-config")), {
          root: node,
          resetHost: resetSelector ? root.querySelector(resetSelector) : node.querySelector("[data-query-reset-host]") || undefined,
          routeKey: node.getAttribute("data-route-key") || undefined,
          fullSetLoaded: node.getAttribute("data-full-set-loaded") === "true"
        }));
      });
    });
  }

  function mountRowControls(root, adapters) {
    var api = requireApi("InkDropOperationalRowControls");
    return Array.from(root.querySelectorAll("[data-inkdrop-row-controls]")).map(function (node) {
      return once(node, "inkdropRowControlsMounted", function () {
        var detailSelector = node.getAttribute("data-details");
        var menuSelector = node.getAttribute("data-menu");
        var statusSelector = node.getAttribute("data-status");
        return api.mount(Object.assign({}, adapters.rowControls || {}, parseJson(node.getAttribute("data-config")), {
          details: detailSelector ? root.querySelector(detailSelector) : undefined,
          disclosureHost: node.querySelector("[data-row-disclosure-host]") || undefined,
          menuHost: menuSelector ? root.querySelector(menuSelector) : node.querySelector("[data-row-menu-host]") || undefined,
          statusHost: statusSelector ? root.querySelector(statusSelector) : node.querySelector("[data-row-status-host]") || undefined
        }));
      });
    });
  }

  function renderTransferTelemetry(root) {
    var api = requireApi("InkDropTransferTelemetry");
    return Array.from(root.querySelectorAll("[data-inkdrop-transfer-telemetry]")).map(function (node) {
      return once(node, "inkdropTransferTelemetryMounted", function () {
        return api.render(node, parseJson(node.getAttribute("data-transfer-row")));
      });
    });
  }

  function mountVersionAbout(root, adapters) {
    var api = requireApi("InkDropVersionAbout");
    return Array.from(root.querySelectorAll("[data-inkdrop-version-about]")).map(function (node) {
      return once(node, "inkdropVersionAboutMounted", function () {
        return api.mount(node, Object.assign({}, adapters.versionAbout || {}, parseJson(node.getAttribute("data-config"))));
      });
    });
  }

  function mount(root, adapters) {
    root = asRoot(root);
    adapters = adapters || {};
    return Object.freeze({
      tableControls: mountTableControls(root, adapters),
      queryControls: mountQueryControls(root, adapters),
      rowControls: mountRowControls(root, adapters),
      transferTelemetry: renderTransferTelemetry(root),
      versionAbout: mountVersionAbout(root, adapters)
    });
  }

  global.InkDropOperationalBootstrap = Object.freeze({
    assetOrder: ASSET_ORDER,
    assetLoaded: function (assetName, documentOverride) {
      return assetLoaded(asDocument(documentOverride), assetName);
    },
    loadAssets: loadAssets,
    mount: mount,
    parseJson: parseJson
  });
})(typeof window !== "undefined" ? window : globalThis);
