(function (global) {
  "use strict";

  function element(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function mount(config) {
    config = config || {};
    var details = config.details;
    var status = config.status || element("span", "inkdrop-row-action-status");
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");

    var disclosure =
      config.disclosure ||
      element("button", "inkdrop-row-disclosure", "Details");
    disclosure.type = "button";
    disclosure.setAttribute(
      "aria-expanded",
      details && details.open ? "true" : "false",
    );
    if (details && details.id)
      disclosure.setAttribute("aria-controls", details.id);
    disclosure.title = "Show operational details";

    function setOpen(open) {
      if (!details) return;
      details.open = open;
      disclosure.setAttribute("aria-expanded", open ? "true" : "false");
      disclosure.title = open
        ? "Hide operational details"
        : "Show operational details";
      status.textContent = open ? "Details expanded." : "Details collapsed.";
      if (typeof config.onDetailsChange === "function")
        config.onDetailsChange(open);
    }
    disclosure.onclick = function () {
      setOpen(!(details && details.open));
    };

    var actionMenu = element(
      "details",
      "arr-table-menu inkdrop-row-action-menu",
    );
    var summary = element("summary", "inkdrop-row-action-summary", "More");
    summary.setAttribute("aria-label", "More actions");
    summary.title = "More actions";
    var panel = element("div", "arr-table-menu-panel inkdrop-row-action-panel");
    panel.setAttribute("role", "menu");
    actionMenu.append(summary, panel);

    function finishAction(control, label, message) {
      control.disabled = false;
      control.removeAttribute("aria-busy");
      control.classList.remove("is-busy");
      control.textContent = label;
      if (message) status.textContent = message;
    }

    function addAction(action) {
      var control = action.href
        ? element("a", "inkdrop-row-action", action.label)
        : element("button", "inkdrop-row-action", action.label);
      control.setAttribute("role", "menuitem");
      if (action.href) control.href = action.href;
      else control.type = "button";
      if (action.disabled === true) {
        control.setAttribute("aria-disabled", "true");
        control.classList.add("is-disabled");
        control.title = action.reason || "This action is unavailable.";
        control.setAttribute("aria-description", control.title);
      } else if (!action.href) {
        control.onclick = function () {
          var result;
          actionMenu.open = false;
          control.disabled = true;
          control.setAttribute("aria-busy", "true");
          control.classList.add("is-busy");
          control.textContent = action.busyLabel || "Working...";
          status.textContent =
            action.pendingMessage || action.label + " started.";
          try {
            result = action.onSelect ? action.onSelect() : undefined;
          } catch (error) {
            finishAction(
              control,
              action.label,
              action.errorMessage || "Action failed.",
            );
            if (typeof config.onError === "function")
              config.onError(error, action);
            return;
          }
          Promise.resolve(result).then(
            function () {
              finishAction(
                control,
                action.label,
                action.successMessage || action.label + " completed.",
              );
            },
            function (error) {
              finishAction(
                control,
                action.label,
                action.errorMessage || "Action failed.",
              );
              if (typeof config.onError === "function")
                config.onError(error, action);
            },
          );
        };
      }
      panel.append(control);
    }
    (config.actions || []).forEach(addAction);
    if (!(config.actions || []).length) {
      var unavailable = element(
        "span",
        "inkdrop-row-action-empty",
        "No actions available",
      );
      unavailable.title = "This row has no available actions.";
      panel.append(unavailable);
    }

    if (config.disclosureHost)
      config.disclosureHost.replaceChildren(disclosure);
    if (config.menuHost) config.menuHost.replaceChildren(actionMenu);
    if (config.statusHost) config.statusHost.replaceChildren(status);
    var controlsApi = global.InkDropOperationalTableControls;
    var cleanup = controlsApi
      ? controlsApi.enhanceMenu(actionMenu, config.menuRoot || document)
      : function () {};

    return Object.freeze({
      setOpen: setOpen,
      status: status,
      destroy: cleanup,
    });
  }

  global.InkDropOperationalRowControls = Object.freeze({ mount: mount });
})(typeof window !== "undefined" ? window : globalThis);
