(function (global) {
  "use strict";

  function element(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function field(labelText, control) {
    var label = element("label", "inkdrop-table-preference-field");
    label.append(element("span", "inkdrop-table-preference-label", labelText), control);
    return label;
  }

  function enhanceMenu(menu, root) {
    if (!menu || menu.dataset.inkdropMenuEnhanced === "true") return function () {};
    menu.dataset.inkdropMenuEnhanced = "true";
    root = root || document;
    var summary = menu.querySelector("summary");
    function closeOtherMenus() {
      if (!menu.open) return;
      root.querySelectorAll("details.arr-table-menu[open]").forEach(function (other) {
        if (other !== menu) other.open = false;
      });
    }
    function closeOutside(event) {
      if (menu.open && !menu.contains(event.target)) menu.open = false;
    }
    function closeOnEscape(event) {
      if (event.key !== "Escape" || !menu.open) return;
      event.preventDefault();
      menu.open = false;
      if (summary) summary.focus();
    }
    menu.addEventListener("toggle", closeOtherMenus);
    document.addEventListener("pointerdown", closeOutside);
    document.addEventListener("keydown", closeOnEscape);
    return function () {
      menu.removeEventListener("toggle", closeOtherMenus);
      document.removeEventListener("pointerdown", closeOutside);
      document.removeEventListener("keydown", closeOnEscape);
      delete menu.dataset.inkdropMenuEnhanced;
    };
  }

  function enhanceMenus(root) {
    root = root || document;
    var cleanups = Array.from(root.querySelectorAll("details.arr-table-menu")).map(function (menu) {
      return enhanceMenu(menu, root);
    });
    return function () { cleanups.forEach(function (cleanup) { cleanup(); }); };
  }

  function mount(config) {
    config = config || {};
    var preferencesApi = global.InkDropOperationalTablePreferences;
    if (!preferencesApi) throw new Error("InkDropOperationalTablePreferences is required");
    var table = config.table;
    var current = preferencesApi.apply(table, preferencesApi.load(config.storage));
    var status = config.status || element("span", "inkdrop-table-preference-status");
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");

    function notify(message) {
      status.textContent = message;
      if (typeof config.onChange === "function") config.onChange(current);
    }

    function update(patch, message) {
      current = preferencesApi.apply(table, preferencesApi.update(patch, config.storage));
      notify(message || "Table preferences updated.");
      refreshViewButtons();
      return current;
    }

    var details = element("details", "arr-table-menu inkdrop-table-preference-menu");
    var summary = element("summary", "", "Options");
    summary.title = "Choose table columns, page size, thumbnails, and detail behavior";
    var panel = element("div", "arr-table-menu-panel inkdrop-table-preference-panel");
    details.append(summary, panel);
    var removeMenuBehavior = enhanceMenu(details, config.menuRoot || document);

    var columns = Array.isArray(config.columns) ? config.columns : [];
    if (columns.length) {
      var columnGroup = element("fieldset", "inkdrop-table-preference-group");
      columnGroup.append(element("legend", "", "Visible columns"));
      columns.forEach(function (column) {
        var checkbox = element("input");
        checkbox.type = "checkbox";
        checkbox.checked = current.columns.length === 0 || current.columns.indexOf(column.key) >= 0;
        checkbox.disabled = column.required === true;
        if (column.required === true) checkbox.title = "This identity column is required.";
        checkbox.onchange = function () {
          var selected = Array.from(columnGroup.querySelectorAll("input[type=checkbox]:checked")).map(function (input) { return input.value; });
          update({ columns: selected }, "Visible columns updated.");
        };
        checkbox.value = column.key;
        var wrapper = element("label", "inkdrop-table-preference-check");
        wrapper.append(checkbox, document.createTextNode(column.label));
        columnGroup.append(wrapper);
      });
      panel.append(columnGroup);
    }

    var pageSize = element("select");
    [24, 50, 100, 250].forEach(function (size) {
      var option = element("option", "", String(size));
      option.value = String(size);
      option.selected = current.pageSize === size;
      pageSize.append(option);
    });
    pageSize.onchange = function () { update({ pageSize: Number(pageSize.value) }, "Page size updated."); };
    panel.append(field("Rows per page", pageSize));

    var thumbnails = element("input");
    thumbnails.type = "checkbox";
    thumbnails.checked = current.thumbnails;
    thumbnails.onchange = function () { update({ thumbnails: thumbnails.checked }, thumbnails.checked ? "Thumbnails shown." : "Thumbnails hidden."); };
    panel.append(field("Show thumbnails", thumbnails));

    var detailsOpen = element("input");
    detailsOpen.type = "checkbox";
    detailsOpen.checked = current.detailsOpen;
    detailsOpen.onchange = function () { update({ detailsOpen: detailsOpen.checked }, detailsOpen.checked ? "Details open by default." : "Details collapsed by default."); };
    panel.append(field("Open row details by default", detailsOpen));

    if (config.optionsHost) {
      config.optionsHost.replaceChildren(details);
      config.optionsHost.append(status);
    }

    var compactButton = element("button", "inkdrop-table-view-choice", "Compact");
    compactButton.type = "button";
    compactButton.title = "Compact rows for fast scanning";
    compactButton.onclick = function () { update({ density: "compact" }, "Compact table view enabled."); };
    var detailedButton = element("button", "inkdrop-table-view-choice", "Detailed");
    detailedButton.type = "button";
    detailedButton.title = "Taller rows with more operational context";
    detailedButton.onclick = function () { update({ density: "detailed" }, "Detailed table view enabled."); };
    var futureButton = element("button", "inkdrop-table-view-choice", "Cards");
    futureButton.type = "button";
    futureButton.disabled = true;
    futureButton.title = "Card view is unavailable on operational pages; use the Series library for posters.";
    futureButton.setAttribute("aria-label", "Cards unavailable: use the Series library for posters.");

    function refreshViewButtons() {
      compactButton.setAttribute("aria-pressed", current.density === "compact" ? "true" : "false");
      detailedButton.setAttribute("aria-pressed", current.density === "detailed" ? "true" : "false");
    }
    refreshViewButtons();

    if (config.viewHost) {
      var viewGroup = element("div", "inkdrop-table-view-choices");
      viewGroup.setAttribute("role", "group");
      viewGroup.setAttribute("aria-label", "Table view");
      viewGroup.append(compactButton, detailedButton, futureButton);
      config.viewHost.replaceChildren(viewGroup);
    }

    if (config.resetButton) {
      config.resetButton.onclick = function () {
        current = preferencesApi.apply(table, preferencesApi.reset(config.storage));
        pageSize.value = String(current.pageSize);
        thumbnails.checked = current.thumbnails;
        detailsOpen.checked = current.detailsOpen;
        panel.querySelectorAll("input[type=checkbox][value]").forEach(function (input) { input.checked = true; });
        refreshViewButtons();
        if (typeof config.onReset === "function") config.onReset();
        notify("Default table order and view restored.");
      };
    }

    return Object.freeze({
      get value() { return current; },
      reset: function () { if (config.resetButton) config.resetButton.click(); },
      status: status,
      destroy: removeMenuBehavior
    });
  }

  global.InkDropOperationalTableControls = Object.freeze({
    mount: mount,
    enhanceMenu: enhanceMenu,
    enhanceMenus: enhanceMenus
  });
})(typeof window !== "undefined" ? window : globalThis);
