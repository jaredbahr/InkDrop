(function (global) {
  "use strict";

  var STORAGE_KEY = "inkdrop.operationalTable.query.v1";

  function element(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function storageFor(candidate) {
    if (candidate) return candidate;
    try { return global.localStorage; } catch (_error) { return null; }
  }

  function read(storage) {
    var target = storageFor(storage);
    if (!target) return {};
    try { return JSON.parse(target.getItem(STORAGE_KEY) || "{}"); }
    catch (_error) { return {}; }
  }

  function write(value, storage) {
    var target = storageFor(storage);
    if (target) {
      try { target.setItem(STORAGE_KEY, JSON.stringify(value)); } catch (_error) {}
    }
  }

  function menu(label, title) {
    var root = element("details", "arr-table-menu inkdrop-query-menu");
    var summary = element("summary", "", label);
    summary.title = title;
    var panel = element("div", "arr-table-menu-panel inkdrop-query-menu-panel");
    root.append(summary, panel);
    return { root: root, summary: summary, panel: panel };
  }

  function choice(label, selected, disabled, reason, onSelect) {
    var button = element("button", "inkdrop-query-choice", label);
    button.type = "button";
    button.setAttribute("role", "menuitemradio");
    button.setAttribute("aria-checked", selected ? "true" : "false");
    button.disabled = disabled === true;
    if (reason) {
      button.title = reason;
      button.setAttribute("aria-description", reason);
    }
    button.onclick = function () { onSelect(button); };
    return button;
  }

  function mount(config) {
    config = config || {};
    var controlsApi = global.InkDropOperationalTableControls;
    var routeKey = config.routeKey || "operational";
    var saved = read(config.storage)[routeKey] || {};
    var state = {
      sort: saved.sort || config.defaultSort || "default",
      direction: saved.direction === "desc" ? "desc" : "asc",
      filters: Object.assign({}, config.defaultFilters || {}, saved.filters || {})
    };
    var status = config.status || element("span", "inkdrop-query-status");
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");

    function persist() {
      var all = read(config.storage);
      all[routeKey] = state;
      write(all, config.storage);
    }

    function announce(message) {
      status.textContent = message;
      if (typeof config.onChange === "function") config.onChange(Object.assign({}, state));
    }

    function invokeSort() {
      if (config.sortMode === "client" && config.fullSetLoaded !== true) {
        announce("Sort unavailable until the full result set is loaded.");
        return false;
      }
      persist();
      if (typeof config.onSort === "function") config.onSort(state.sort, state.direction);
      announce("Sorted by " + (labelFor(config.sorts, state.sort) || "default order") + " " + state.direction + ".");
      return true;
    }

    function invokeFilters() {
      persist();
      if (typeof config.onFilter === "function") config.onFilter(Object.assign({}, state.filters));
      var active = Object.keys(state.filters).filter(function (key) { return state.filters[key]; }).length;
      announce(active ? active + " filter" + (active === 1 ? "" : "s") + " applied." : "All filters cleared.");
    }

    function labelFor(items, key) {
      var match = (items || []).find(function (item) { return item.key === key; });
      return match ? match.label : "";
    }

    var sortMenu = menu("Sort", "Choose table sort field and direction");
    sortMenu.panel.setAttribute("role", "menu");
    (config.sorts || []).forEach(function (item) {
      var unsupported = item.supported === false || (config.sortMode === "client" && config.fullSetLoaded !== true);
      var reason = item.reason || (unsupported ? "This sort requires server support or the complete result set." : "");
      sortMenu.panel.append(choice(item.label, state.sort === item.key, unsupported, reason, function () {
        state.sort = item.key;
        invokeSort();
        sortMenu.root.open = false;
        sortMenu.root.querySelector('summary')?.focus();
        refresh();
      }));
    });
    ["asc", "desc"].forEach(function (direction) {
      sortMenu.panel.append(choice(direction === "asc" ? "Ascending" : "Descending", state.direction === direction, false, "", function () {
        state.direction = direction;
        invokeSort();
        sortMenu.root.open = false;
        sortMenu.root.querySelector('summary')?.focus();
        refresh();
      }));
    });

    var filterMenu = menu("Filter", "Filter this operational table");
    filterMenu.panel.setAttribute("role", "menu");
    (config.filters || []).forEach(function (item) {
      filterMenu.panel.append(choice(item.label, state.filters[item.key] === true, item.supported === false, item.reason || "", function () {
        state.filters[item.key] = state.filters[item.key] !== true;
        invokeFilters();
        filterMenu.root.querySelector('summary')?.focus();
        refresh();
      }));
    });
    var clear = element("button", "inkdrop-query-clear", "Clear filters");
    clear.type = "button";
    clear.onclick = function () {
      state.filters = Object.assign({}, config.defaultFilters || {});
      invokeFilters();
      filterMenu.root.open = false;
      filterMenu.root.querySelector('summary')?.focus();
      refresh();
    };
    filterMenu.panel.append(clear);

    function refresh() {
      sortMenu.panel.querySelectorAll("[role=menuitemradio]").forEach(function (button) {
        var text = button.textContent;
        var item = (config.sorts || []).find(function (candidate) { return candidate.label === text; });
        if (item) button.setAttribute("aria-checked", state.sort === item.key ? "true" : "false");
        if (text === "Ascending") button.setAttribute("aria-checked", state.direction === "asc" ? "true" : "false");
        if (text === "Descending") button.setAttribute("aria-checked", state.direction === "desc" ? "true" : "false");
      });
      filterMenu.panel.querySelectorAll("[role=menuitemradio]").forEach(function (button) {
        var item = (config.filters || []).find(function (candidate) { return candidate.label === button.textContent; });
        if (item) button.setAttribute("aria-checked", state.filters[item.key] === true ? "true" : "false");
      });
    }

    if (config.sortHost) config.sortHost.replaceChildren(sortMenu.root);
    if (config.filterHost) config.filterHost.replaceChildren(filterMenu.root);
    if (config.statusHost) config.statusHost.replaceChildren(status);
    var cleanupMenus = controlsApi ? controlsApi.enhanceMenus(config.menuRoot || document) : function () {};

    function reset() {
      state.sort = config.defaultSort || "default";
      state.direction = "asc";
      state.filters = Object.assign({}, config.defaultFilters || {});
      persist();
      if (typeof config.onReset === "function") config.onReset(Object.assign({}, state));
      announce("Default sort and filters restored.");
      refresh();
    }

    refresh();
    return Object.freeze({
      get value() { return Object.assign({}, state, { filters: Object.assign({}, state.filters) }); },
      reset: reset,
      status: status,
      destroy: cleanupMenus
    });
  }

  global.InkDropOperationalQueryControls = Object.freeze({ mount: mount, storageKey: STORAGE_KEY });
})(typeof window !== "undefined" ? window : globalThis);
