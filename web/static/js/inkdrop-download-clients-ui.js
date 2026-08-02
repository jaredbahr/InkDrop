(function (global) {
  "use strict";

  const GROUPS = [
    {key: "torrent", label: "Torrent", types: ["qbittorrent", "transmission", "deluge", "utorrent", "rtorrent"]},
    {key: "usenet", label: "Usenet", types: ["sabnzbd", "nzbget"]},
    {key: "soulseek", label: "Soulseek", types: ["slskd"]},
  ];
  const LABELS = {
    qbittorrent: "qBittorrent", transmission: "Transmission", deluge: "Deluge",
    utorrent: "uTorrent", rtorrent: "rTorrent", sabnzbd: "SABnzbd",
    nzbget: "NZBGet", slskd: "SLSKD",
  };
  const FALLBACK_FIELDS = {
    qbittorrent: {username: {}, password: {kind: "secret"}, api_key: {kind: "secret"}, verify_tls: {kind: "boolean", default: true}},
    sabnzbd: {api_key: {kind: "secret"}, verify_tls: {kind: "boolean", default: true}},
    slskd: {api_key: {kind: "secret"}, verify_tls: {kind: "boolean", default: true}},
    transmission: {username: {}, password: {kind: "secret"}, category: {}, label: {}, download_path: {}, verify_tls: {kind: "boolean", default: true}},
    deluge: {username: {}, password: {kind: "secret"}, category: {}, label: {}, download_path: {}, verify_tls: {kind: "boolean", default: true}},
    nzbget: {username: {}, password: {kind: "secret"}, api_key: {kind: "secret"}, category: {}, label: {}, download_path: {}, verify_tls: {kind: "boolean", default: true}},
    utorrent: {username: {}, password: {kind: "secret"}, category: {}, label: {}, download_path: {}, verify_tls: {kind: "boolean", default: true}},
    rtorrent: {username: {}, password: {kind: "secret"}, category: {}, label: {}, download_path: {}, verify_tls: {kind: "boolean", default: true}},
  };
  const SECRET_LABELS = {password: "Password", api_key: "API Key"};
  // Per-client notes for credentials that are alternatives rather than extras.
  const SECRET_HELP = {
    "qbittorrent:password": "Use this with the username for a normal Web UI login. Leave both blank if you are using an API key instead.",
    "qbittorrent:api_key": "qBittorrent 5.2.0 and newer can issue an API key (it starts with qbt_). A key replaces the username and password rather than adding to them, so fill in one or the other.",
    "nzbget:api_key": "Use this instead of the username and password if your NZBGet exposes an API key.",
  };
  const SLSKD_ADVANCED = [
    ["wait_seconds", "Search Wait Seconds", "How long InkDrop waits for each bounded SLSKD search to return results.", 1],
    ["max_queries", "Max Queries", "Maximum query variants InkDrop may send during one SLSKD probe.", 1],
    ["auto_grab_max", "Auto Grab Max", "Maximum safe SLSKD candidates InkDrop can start in one probe pass.", 0],
    ["probe_budget_seconds", "Probe Budget Seconds", "Maximum total time for one SLSKD probe before InkDrop returns to the queue.", 30],
    ["cooldown_hours", "Cooldown Hours", "Delay before InkDrop repeats an unsuccessful SLSKD probe for the same item.", 0],
    ["max_active_per_user", "Max Active Per User", "Per-user transfer cap so one Soulseek user cannot stall all active work.", 1],
  ];
  const MAX_MAPPINGS = 32;
  const stateByRoot = new WeakMap();

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function clientLabel(type, row) {
    return String(row?.display_name || LABELS[type] || type || "Download client");
  }

  function api(path, options) {
    if (!global.InkDropApi?.request) return Promise.reject(new Error("InkDrop API client is unavailable."));
    return global.InkDropApi.request(path, options || {method: "GET"});
  }

  function errorText(error, fallback) {
    return String(error?.detail || error?.message || fallback || "InkDrop could not complete this action.");
  }

  function setLive(state, message, tone) {
    state.live.textContent = String(message || "");
    state.live.dataset.tone = tone || "";
  }

  function registryRow(state, type) {
    return (state.registry || []).find(row => String(row?.client_type || row?.client_id || "").toLowerCase() === type) || null;
  }

  function selectableRegistry(state) {
    const addable = new Set(state.payload?.registry?.addable || []);
    const testable = new Set(state.payload?.registry?.testable || []);
    return (state.registry || []).filter(row => {
      const type = String(row?.client_type || row?.client_id || "").toLowerCase();
      return type && row?.implemented === true && row?.addable !== false && row?.testable !== false
        && (!addable.size || addable.has(type)) && (!testable.size || testable.has(type));
    });
  }

  function focusable(dialog) {
    return Array.from(dialog.querySelectorAll('button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [href], [tabindex]:not([tabindex="-1"])'))
      .filter(node => !node.hidden && node.offsetParent !== null);
  }

  function openDialog(state, content, returnFocus) {
    const dialog = state.dialog;
    dialog.replaceChildren(content);
    state.returnFocus = returnFocus || document.activeElement;
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
    requestAnimationFrame(() => focusable(dialog)[0]?.focus());
  }

  function closeDialog(state) {
    const dialog = state.dialog;
    if (dialog.open && typeof dialog.close === "function") dialog.close();
    else dialog.removeAttribute("open");
    const target = state.returnFocus;
    state.returnFocus = null;
    requestAnimationFrame(() => target?.isConnected && target.focus());
  }

  function dialogKeydown(state, event) {
    if (event.key === "Escape") {
      event.preventDefault();
      closeDialog(state);
      return;
    }
    if (event.key !== "Tab") return;
    const nodes = focusable(state.dialog);
    if (!nodes.length) return;
    const first = nodes[0], last = nodes[nodes.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault(); last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault(); first.focus();
    }
  }

  function shell(title, copy) {
    const root = el("div", "download-client-dialog-shell");
    const head = el("div", "download-client-dialog-head");
    const titleNode = el("h2", "", title);
    titleNode.id = `downloadClientDialogTitle-${Math.random().toString(36).slice(2)}`;
    const close = el("button", "download-client-dialog-close", "Close");
    close.type = "button";
    close.dataset.dialogClose = "1";
    head.append(titleNode, close);
    root.append(head);
    if (copy) root.append(el("p", "download-client-dialog-copy", copy));
    root.dataset.titleId = titleNode.id;
    return root;
  }

  function showChooser(state, trigger) {
    const available = new Map(selectableRegistry(state).map(row => [String(row.client_type || row.client_id).toLowerCase(), row]));
    const body = shell("Add Download Client", "Choose the client InkDrop should connect to. New clients start disabled until you finish configuration.");
    const form = el("form", "download-client-chooser");
    form.addEventListener("submit", event => {
      event.preventDefault();
      const type = new FormData(form).get("client_type");
      if (!type) return;
      showEditor(state, {client_type: String(type), enabled: false, priority: 100}, trigger);
    });
    for (const group of GROUPS) {
      const rows = group.types.map(type => available.get(type)).filter(Boolean);
      if (!rows.length) continue;
      const fieldset = el("fieldset", "download-client-choice-group");
      fieldset.append(el("legend", "", group.label));
      const grid = el("div", "download-client-choice-grid");
      for (const row of rows) {
        const type = String(row.client_type || row.client_id).toLowerCase();
        const label = el("label", "download-client-choice");
        const input = document.createElement("input");
        input.type = "radio"; input.name = "client_type"; input.value = type; input.required = true;
        const copy = el("span", "");
        copy.append(el("strong", "", clientLabel(type, row)), el("small", "", (row.supported_protocols || [group.key]).join(" · ")));
        label.append(input, copy); grid.append(label);
      }
      fieldset.append(grid); form.append(fieldset);
    }
    if (!available.size) form.append(el("p", "download-client-inline-error", "InkDrop has no download clients you can add or test yet."));
    const footer = el("div", "download-client-dialog-footer");
    const cancel = el("button", "", "Cancel"); cancel.type = "button"; cancel.dataset.dialogClose = "1";
    const next = el("button", "primary", "Continue"); next.type = "submit"; next.disabled = !available.size;
    footer.append(cancel, next); form.append(footer); body.append(form);
    body.querySelectorAll("[data-dialog-close]").forEach(button => button.addEventListener("click", () => closeDialog(state)));
    state.dialog.setAttribute("aria-labelledby", body.dataset.titleId);
    openDialog(state, body, trigger);
  }

  function field(form, labelText, name, options) {
    const row = el("div", `download-client-field ${options?.wide ? "wide" : ""}`.trim());
    const id = `dc-${name}-${Math.random().toString(36).slice(2)}`;
    const label = el("label", "", labelText); label.htmlFor = id;
    const input = document.createElement(options?.multiline ? "textarea" : "input");
    input.id = id; input.name = name;
    input.type = options?.type || "text";
    if (options?.value !== undefined && options?.value !== null) input.value = String(options.value);
    if (options?.required) input.required = true;
    if (options?.min !== undefined) input.min = String(options.min);
    if (options?.max !== undefined) input.max = String(options.max);
    if (options?.placeholder) input.placeholder = options.placeholder;
    if (options?.autocomplete) input.autocomplete = options.autocomplete;
    if (options?.readonly) input.readOnly = true;
    row.append(label, input);
    if (options?.help) {
      const help = el("p", "download-client-field-help", options.help);
      help.id = `${id}-help`; input.setAttribute("aria-describedby", help.id); row.append(help);
    }
    form.append(row);
    return input;
  }

  function checkbox(form, labelText, name, checked, help) {
    const row = el("div", "download-client-field wide");
    const label = el("label", "download-client-check");
    const input = document.createElement("input"); input.type = "checkbox"; input.name = name; input.checked = !!checked;
    label.append(input, document.createTextNode(labelText)); row.append(label);
    if (help) row.append(el("p", "download-client-field-help", help));
    form.append(row); return input;
  }

  function secretFields(type, registry) {
    const merged = {...(FALLBACK_FIELDS[type] || {}), ...(registry?.fields || {})};
    return Object.entries(merged).filter(([, meta]) => String(meta?.kind || "").toLowerCase() === "secret").map(([key]) => key);
  }

  function clientFields(type, registry) {
    return {...(FALLBACK_FIELDS[type] || {}), ...(registry?.fields || {})};
  }

  function appendSecret(form, instance, key, type) {
    const configured = !!instance?.secret_fields?.[key]?.configured;
    const specific = SECRET_HELP[`${type}:${key}`];
    const base = configured
      ? "A secret is saved. Leave this blank to keep it; InkDrop never returns the saved value."
      : "Stored separately and never returned by the API.";
    const input = field(form, SECRET_LABELS[key] || key.replace(/_/g, " "), `secret:${key}`, {
      type: "password", autocomplete: "new-password",
      placeholder: configured ? "Saved; leave blank to keep" : "Not set",
      help: specific ? `${specific} ${base}` : base,
    });
    input.dataset.secretName = key;
    if (configured) {
      const clear = checkbox(form, `Clear saved ${SECRET_LABELS[key] || key}`, `clear:${key}`, false, "Clearing a required secret also disables the client when you save.");
      clear.dataset.clearSecret = key;
    }
  }

  function appendMappings(form, instance) {
    const section = el("fieldset", "download-client-mappings wide");
    section.append(el("legend", "", "Remote Path Mappings"));
    section.append(el("p", "download-client-field-help", "Only add mappings when the download client and InkDrop see the same files under different absolute paths."));
    const rows = el("div", "download-client-mapping-rows");
    const addRow = (mapping) => {
      if (rows.children.length >= MAX_MAPPINGS) return;
      const row = el("div", "download-client-mapping-row");
      const remote = field(row, "Remote path", "mapping_remote", {value: mapping?.remote_path || "", placeholder: "/downloads/comics"});
      remote.dataset.mappingRemote = "1";
      const local = field(row, "Local path", "mapping_local", {value: mapping?.local_path || "", placeholder: "/mnt/downloads/comics"});
      local.dataset.mappingLocal = "1";
      const remove = el("button", "danger secondary", "Remove"); remove.type = "button";
      remove.addEventListener("click", () => row.remove());
      row.append(remove); rows.append(row);
    };
    (instance?.path_mappings || []).forEach(addRow);
    section.append(rows);
    const add = el("button", "secondary download-client-add-mapping", "Add Path Mapping"); add.type = "button";
    add.addEventListener("click", () => addRow({})); section.append(add); form.append(section);
  }

  function readMappings(form) {
    const out = [];
    for (const row of form.querySelectorAll(".download-client-mapping-row")) {
      const remote = row.querySelector("[data-mapping-remote]")?.value.trim() || "";
      const local = row.querySelector("[data-mapping-local]")?.value.trim() || "";
      if (remote || local) out.push({remote_path: remote, local_path: local});
    }
    return out;
  }

  function editorPayload(form, instance, type) {
    const data = new FormData(form);
    const payload = {
      name: String(data.get("name") || "").trim(), client_type: type,
      enabled: data.get("enabled") === "on", priority: Number(data.get("priority") || 100),
      base_url: String(data.get("base_url") || "").trim(), username: String(data.get("username") || "").trim(),
      category: String(data.get("category") || "").trim(), download_path: String(data.get("download_path") || "").trim(),
      categories: {}, download_paths: {}, path_mappings: readMappings(form), settings: {...(instance?.settings || {})},
    };
    for (const media of ["comics", "manga", "ebooks"]) {
      const category = String(data.get(`category:${media}`) || "").trim();
      const path = String(data.get(`download_path:${media}`) || "").trim();
      if (category) payload.categories[media] = category;
      if (path) payload.download_paths[media] = path;
    }
    const label = String(data.get("label") || "").trim();
    if (label) payload.settings.label = label; else delete payload.settings.label;
    if (form.elements.verify_tls) payload.settings.verify_tls = data.get("verify_tls") === "on";
    if (type === "slskd") {
      for (const [key] of SLSKD_ADVANCED) {
        const value = data.get(`setting:${key}`);
        if (value !== null && String(value).trim() !== "") payload.settings[key] = Number(value);
        else delete payload.settings[key];
      }
    }
    const secrets = {}, clear = [];
    form.querySelectorAll("[data-secret-name]").forEach(input => {
      if (input.value !== "") secrets[input.dataset.secretName] = input.value;
    });
    form.querySelectorAll("[data-clear-secret]").forEach(input => { if (input.checked) clear.push(input.dataset.clearSecret); });
    if (Object.keys(secrets).length) payload.secrets = secrets;
    if (clear.length) {
      payload.clear_secret_fields = clear;
      if (payload.enabled) payload.enabled = false;
    }
    return payload;
  }

  function validateEditor(form) {
    form.querySelectorAll("[aria-invalid]").forEach(node => node.removeAttribute("aria-invalid"));
    const first = Array.from(form.elements).find(node => typeof node.checkValidity === "function" && !node.checkValidity());
    if (first) {
      first.setAttribute("aria-invalid", "true"); first.focus(); first.reportValidity(); return false;
    }
    for (const row of form.querySelectorAll(".download-client-mapping-row")) {
      const remote = row.querySelector("[data-mapping-remote]"), local = row.querySelector("[data-mapping-local]");
      if (!!remote.value.trim() !== !!local.value.trim()) {
        const invalid = remote.value.trim() ? local : remote;
        invalid.setCustomValidity("Both remote and local absolute paths are required.");
        invalid.setAttribute("aria-invalid", "true"); invalid.focus(); invalid.reportValidity();
        invalid.addEventListener("input", () => invalid.setCustomValidity(""), {once: true}); return false;
      }
    }
    return true;
  }

  function showEditor(state, instance, trigger) {
    const editing = !!instance?.id;
    const type = String(instance.client_type || "").toLowerCase();
    const registry = registryRow(state, type);
    const fields = clientFields(type, registry);
    const body = shell(editing ? `Edit ${instance.name}` : `Add ${clientLabel(type, registry)}`, editing ? "Update this saved instance. The client type and instance ID cannot change." : "Configure the connection, test the draft, then save it. New instances are disabled by default.");
    const form = el("form", "download-client-editor");
    form.noValidate = true;
    const error = el("div", "download-client-inline-error wide"); error.hidden = true; error.setAttribute("role", "alert"); form.append(error);
    if (editing) field(form, "Instance ID", "instance_id", {value: instance.id, readonly: true, help: "Immutable identity used by queue and history records."});
    field(form, "Instance name", "name", {value: instance.name || `${clientLabel(type, registry)}`, required: true, help: "A unique name, such as qBittorrent Main or NZBGet Comics."});
    checkbox(form, "Enabled", "enabled", !!instance.enabled, "Disabled clients keep their settings but are skipped by routing and Test All.");
    field(form, "URL", "base_url", {type: "url", value: instance.base_url || "", required: !!instance.enabled, placeholder: "http://download-client:port", help: "HTTP or HTTPS endpoint without embedded credentials, query parameters, or fragments."});
    if (Object.prototype.hasOwnProperty.call(fields, "username")) field(form, "Username", "username", {value: instance.username || "", autocomplete: "username"});
    secretFields(type, registry).forEach(key => appendSecret(form, instance, key, type));
    field(form, "Priority", "priority", {type: "number", value: instance.priority || 100, min: 1, max: 1000, required: true, help: "Lower numbers are preferred when more than one enabled client can handle the same protocol."});
    if (Object.prototype.hasOwnProperty.call(fields, "category") || ["qbittorrent", "sabnzbd", "transmission", "deluge", "nzbget", "utorrent", "rtorrent"].includes(type)) field(form, "Default category", "category", {value: instance.category || "", help: "Category applied when a provider does not specify a media-specific category."});
    if (Object.prototype.hasOwnProperty.call(fields, "label")) field(form, "Label", "label", {value: instance.settings?.label || "", help: "Client label used to identify InkDrop-owned work."});
    if (Object.prototype.hasOwnProperty.call(fields, "download_path") || type !== "sabnzbd") field(form, "Default download path", "download_path", {value: instance.download_path || "", placeholder: "/downloads/comics"});
    if (Object.prototype.hasOwnProperty.call(fields, "verify_tls")) checkbox(form, "Verify TLS certificates", "verify_tls", instance.settings?.verify_tls !== false, "Keep enabled for HTTPS clients unless you intentionally use a trusted private certificate setup.");
    const routing = el("fieldset", "download-client-routing wide");
    routing.append(el("legend", "", "Media Routing"), el("p", "download-client-field-help", "Optional category and download-path overrides for comics, manga, and ebooks."));
    for (const media of ["comics", "manga", "ebooks"]) {
      field(routing, `${media[0].toUpperCase() + media.slice(1)} category`, `category:${media}`, {value: instance.categories?.[media] || ""});
      field(routing, `${media[0].toUpperCase() + media.slice(1)} path`, `download_path:${media}`, {value: instance.download_paths?.[media] || ""});
    }
    form.append(routing);
    appendMappings(form, instance);
    if (type === "slskd") {
      const advanced = el("details", "download-client-advanced wide");
      advanced.append(el("summary", "", "SLSKD Advanced Search Settings"));
      const grid = el("div", "download-client-advanced-grid");
      for (const [key, label, help, min] of SLSKD_ADVANCED) field(grid, label, `setting:${key}`, {type: "number", min, value: instance.settings?.[key] ?? "", help});
      advanced.append(grid); form.append(advanced);
    }
    const footer = el("div", "download-client-dialog-footer wide");
    const cancel = el("button", "", "Cancel"); cancel.type = "button"; cancel.dataset.dialogClose = "1";
    const draftTest = el("button", "secondary", "Test Draft"); draftTest.type = "button";
    draftTest.addEventListener("click", async () => {
      if (!validateEditor(form) || draftTest.disabled) return;
      draftTest.disabled = true; error.hidden = true;
      try {
        const result = await api("/api/download-clients/test", {method: "POST", body: editorPayload(form, instance, type)});
        const tested = result.result || {};
        error.hidden = false; error.dataset.tone = tested.ok ? "good" : "bad";
        error.textContent = tested.ok ? "Draft connection succeeded." : `Draft test failed: ${tested.reason || tested.error_type || "connection unavailable"}.`;
      } catch (err) {
        error.hidden = false; error.dataset.tone = "bad"; error.textContent = errorText(err, "Draft test failed.");
      } finally { draftTest.disabled = false; }
    });
    footer.append(cancel, draftTest);
    if (editing) {
      const savedTest = el("button", "secondary", "Test Saved"); savedTest.type = "button";
      savedTest.addEventListener("click", async () => {
        savedTest.disabled = true;
        try {
          const result = await api(`/api/download-clients/${encodeURIComponent(instance.id)}/test`, {method: "POST", body: {}});
          const tested = result.status?.result || {};
          error.hidden = false; error.dataset.tone = tested.ok ? "good" : (tested.skipped ? "warn" : "bad");
          error.textContent = tested.skipped ? "Saved test skipped because this instance is disabled." : (tested.ok ? "Saved connection succeeded." : `Saved test failed: ${tested.reason || tested.error_type || "connection unavailable"}.`);
        } catch (err) { error.hidden = false; error.dataset.tone = "bad"; error.textContent = errorText(err, "Saved test failed."); }
        finally { savedTest.disabled = false; }
      });
      footer.append(savedTest);
    }
    const save = el("button", "primary", editing ? "Save Changes" : "Add Client"); save.type = "submit"; footer.append(save); form.append(footer);
    form.addEventListener("submit", async event => {
      event.preventDefault(); if (!validateEditor(form) || save.disabled) return;
      save.disabled = true; error.hidden = true;
      const payload = editorPayload(form, instance, type);
      try {
        const path = editing ? `/api/download-clients/${encodeURIComponent(instance.id)}` : "/api/download-clients";
        const options = {method: editing ? "PATCH" : "POST", body: payload};
        if (editing) options.headers = {"If-Match": `W/"${instance.revision}"`};
        await api(path, options);
        closeDialog(state); await load(state);
        setLive(state, editing ? `${instance.name} updated.` : `${payload.name} added.`, "good");
      } catch (err) {
        error.hidden = false; error.dataset.tone = "bad"; error.textContent = errorText(err, "Could not save download client.");
        if (Number(err?.status) === 409) error.textContent += " Refresh the client list before retrying.";
        error.focus?.();
      } finally { save.disabled = false; }
    });
    body.append(form);
    body.querySelectorAll("[data-dialog-close]").forEach(button => button.addEventListener("click", () => closeDialog(state)));
    state.dialog.setAttribute("aria-labelledby", body.dataset.titleId); openDialog(state, body, trigger);
  }

  function statusText(status) {
    const result = status?.result || {};
    if (status?.skipped || result.skipped) return {text: `Skipped · ${status?.reason || result.reason || "not ready"}`, tone: "warn"};
    if (result.ok) return {text: "Connected", tone: "good"};
    if (status) return {text: result.reason || result.error_type || "Connection failed", tone: "bad"};
    return {text: "Not tested", tone: ""};
  }

  async function testSaved(state, instance, button) {
    if (state.testing.has(instance.id)) return;
    state.testing.add(instance.id); button.disabled = true; button.textContent = "Testing…";
    try {
      const payload = await api(`/api/download-clients/${encodeURIComponent(instance.id)}/test`, {method: "POST", body: {}});
      state.statuses.set(instance.id, payload.status || {}); renderCards(state);
      const result = payload.status?.result || {};
      setLive(state, result.skipped ? `${instance.name} skipped: ${result.reason || "not ready"}.` : (result.ok ? `${instance.name} connected.` : `${instance.name} failed: ${result.reason || result.error_type || "connection unavailable"}.`), result.ok ? "good" : (result.skipped ? "warn" : "bad"));
    } catch (err) { setLive(state, errorText(err, `Could not test ${instance.name}.`), "bad"); }
    finally { state.testing.delete(instance.id); }
  }

  async function toggleEnabled(state, instance, button) {
    if (button.disabled) return; button.disabled = true;
    try {
      await api(`/api/download-clients/${encodeURIComponent(instance.id)}`, {method: "PATCH", headers: {"If-Match": `W/"${instance.revision}"`}, body: {enabled: !instance.enabled}});
      await load(state); setLive(state, `${instance.name} ${instance.enabled ? "disabled" : "enabled"}.`, "good");
    } catch (err) { setLive(state, errorText(err, `Could not ${instance.enabled ? "disable" : "enable"} ${instance.name}.`), "bad"); button.disabled = false; }
  }

  function confirmDelete(state, instance, trigger) {
    const body = shell(`Delete ${instance.name}?`, "Delete this download-client configuration from InkDrop.");
    const warning = el("div", "download-client-delete-warning");
    warning.append(el("strong", "", "Configuration only"), el("p", "", "Downloaded files, queue history, and client-side jobs are not deleted. InkDrop will refuse deletion while provider mappings or active download tasks still reference this instance."));
    body.append(warning);
    const footer = el("div", "download-client-dialog-footer");
    const cancel = el("button", "", "Cancel"); cancel.type = "button"; cancel.dataset.dialogClose = "1";
    const remove = el("button", "danger", "Delete Client"); remove.type = "button";
    remove.addEventListener("click", async () => {
      remove.disabled = true;
      try {
        await api(`/api/download-clients/${encodeURIComponent(instance.id)}`, {method: "DELETE", headers: {"If-Match": `W/"${instance.revision}"`}, body: {}});
        closeDialog(state); await load(state); setLive(state, `${instance.name} deleted.`, "good");
      } catch (err) {
        remove.disabled = false;
        const message = errorText(err, `Could not delete ${instance.name}.`);
        let alert = body.querySelector(".download-client-inline-error");
        if (!alert) { alert = el("div", "download-client-inline-error"); alert.setAttribute("role", "alert"); body.insertBefore(alert, footer); }
        alert.textContent = Number(err?.status) === 409 ? `${message} Remove mappings, wait for active work to finish, or refresh after another edit.` : message;
      }
    });
    footer.append(cancel, remove); body.append(footer);
    body.querySelectorAll("[data-dialog-close]").forEach(button => button.addEventListener("click", () => closeDialog(state)));
    state.dialog.setAttribute("aria-labelledby", body.dataset.titleId); openDialog(state, body, trigger);
    requestAnimationFrame(() => cancel.focus());
  }

  function renderCards(state) {
    state.cards.replaceChildren();
    if (!state.instances.length) {
      const empty = el("div", "download-client-empty");
      empty.append(el("strong", "", "No additional download client instances"), el("p", "", "Your qBittorrent, SABnzbd, and SLSKD cards below already handle one instance of each. Add one here only if you need a second instance of the same client."));
      state.cards.append(empty);
    }
    for (const instance of state.instances) {
      const card = el("article", "download-client-instance-card"); card.dataset.downloadClientInstance = instance.id; card.dataset.clientType = instance.client_type;
      const head = el("div", "download-client-instance-head");
      const title = el("div", ""); title.append(el("h3", "", instance.name), el("p", "", `${clientLabel(instance.client_type, registryRow(state, instance.client_type))} · ${instance.base_url || "URL not configured"}`));
      const enabled = el("span", `download-client-instance-state ${instance.enabled ? "good" : ""}`, instance.enabled ? "Enabled" : "Disabled"); head.append(title, enabled); card.append(head);
      const facts = el("dl", "download-client-instance-facts");
      for (const [label, value] of [["Priority", instance.priority], ["Host", instance.base_url || "Not configured"], ["Revision", instance.revision]]) {
        const wrap = el("div", ""); wrap.append(el("dt", "", label), el("dd", "", value)); facts.append(wrap);
      }
      const status = statusText(state.statuses.get(instance.id));
      const statusWrap = el("div", ""); statusWrap.append(el("dt", "", "Health"), el("dd", `download-client-health ${status.tone}`, status.text)); facts.append(statusWrap); card.append(facts);
      if ((instance.path_mappings || []).length) card.append(el("p", "download-client-instance-note", `${instance.path_mappings.length} remote path mapping${instance.path_mappings.length === 1 ? "" : "s"}`));
      const actions = el("div", "download-client-instance-actions");
      const edit = el("button", "", "Edit"); edit.type = "button"; edit.addEventListener("click", () => showEditor(state, instance, edit));
      const test = el("button", "", "Test"); test.type = "button"; test.addEventListener("click", () => testSaved(state, instance, test));
      const toggle = el("button", "", instance.enabled ? "Disable" : "Enable"); toggle.type = "button"; toggle.addEventListener("click", () => toggleEnabled(state, instance, toggle));
      const remove = el("button", "danger", "Delete"); remove.type = "button"; remove.addEventListener("click", () => confirmDelete(state, instance, remove));
      actions.append(edit, test, toggle, remove); card.append(actions); state.cards.append(card);
    }
    hideMigratedLegacyCards(state);
  }

  function hideMigratedLegacyCards(state) {
    const types = new Set(state.instances.map(item => String(item.client_type || "").toLowerCase()));
    const group = state.root.closest(".settings-group-body") || state.root.parentElement;
    group?.querySelectorAll?.(".settings-card[data-provider-id]").forEach(card => {
      const migrated = types.has(String(card.dataset.providerId || "").toLowerCase());
      card.hidden = migrated; card.dataset.downloadClientLegacyFallback = migrated ? "hidden-by-instance" : "visible";
    });
  }

  async function runTestAll(state) {
    if (state.testAllRunning) return;
    state.testAllRunning = true; state.testAll.disabled = true; state.testAll.textContent = "Starting tests…";
    setLive(state, "Starting download-client tests. Disabled clients will be skipped.", "");
    try {
      const accepted = await api("/api/download-clients/test-all", {method: "POST", body: {}});
      const runId = accepted.run_id;
      let run = null;
      for (let attempt = 0; attempt < 75; attempt += 1) {
        await new Promise(resolve => setTimeout(resolve, attempt ? 400 : 100));
        const payload = await api(`/api/download-clients/test-runs/${encodeURIComponent(runId)}`, {method: "GET", cache: "no-store"});
        run = payload.run || null;
        state.testAll.textContent = run?.state === "running" ? "Testing clients…" : "Waiting for tests…";
        if (run?.state === "completed") break;
      }
      if (!run || run.state !== "completed") throw new Error("Test All did not finish within 30 seconds.");
      let passed = 0, failed = 0, skipped = 0;
      for (const row of run.results || []) {
        const status = row.result ? row : (row.status || row);
        const id = row.instance_id || status.instance_id;
        if (id) state.statuses.set(id, status);
        const result = status.result || status;
        if (result.skipped || row.skipped) skipped += 1;
        else if (result.ok) passed += 1;
        else failed += 1;
      }
      renderCards(state); setLive(state, `Test All complete: ${passed} connected, ${failed} failed, ${skipped} skipped.`, failed ? "bad" : "good");
    } catch (err) { setLive(state, errorText(err, "Test All failed."), "bad"); }
    finally { state.testAllRunning = false; state.testAll.disabled = false; state.testAll.textContent = "Test All Clients"; }
  }

  async function load(state) {
    if (state.loading) return state.loading;
    state.root.dataset.loading = "true";
    state.loading = api("/api/download-clients", {method: "GET", cache: "no-store"})
      .then(payload => {
        state.payload = payload || {}; state.registry = payload.registry?.clients || []; state.instances = payload.instances || [];
        state.add.disabled = !selectableRegistry(state).length; state.testAll.disabled = !state.instances.length;
        renderCards(state); setLive(state, `${state.instances.length} additional download client instance${state.instances.length === 1 ? "" : "s"}.`, "");
      })
      .catch(err => {
        state.cards.replaceChildren(el("div", "download-client-inline-error", errorText(err, "Could not load download clients.")));
        setLive(state, errorText(err, "Could not load download clients."), "bad");
      })
      .finally(() => { state.loading = null; delete state.root.dataset.loading; });
    return state.loading;
  }

  function mount(parent) {
    if (!parent) return null;
    const previous = parent.querySelector(":scope > [data-download-client-manager]");
    if (previous) return previous;
    const root = el("section", "download-client-manager"); root.dataset.downloadClientManager = "1";
    const head = el("div", "download-client-manager-head");
    const copy = el("div", ""); copy.append(el("h3", "", "Additional Download Client Instances"), el("p", "", "For running more than one instance of the same client (e.g. two qBittorrent hosts). The single qBittorrent/SABnzbd/SLSKD cards below cover the common one-instance-each case."));
    const actions = el("div", "download-client-manager-actions");
    const add = el("button", "primary", "Add Download Client"); add.type = "button";
    const testAll = el("button", "", "Test All Clients"); testAll.type = "button"; testAll.disabled = true;
    const refresh = el("button", "", "Refresh Clients"); refresh.type = "button";
    actions.append(add, testAll, refresh); head.append(copy, actions);
    const live = el("div", "download-client-manager-live"); live.setAttribute("role", "status"); live.setAttribute("aria-live", "polite");
    const cards = el("div", "download-client-instance-grid");
    const dialog = document.createElement("dialog"); dialog.className = "download-client-dialog"; dialog.setAttribute("aria-modal", "true");
    root.append(head, live, cards, dialog); parent.prepend(root);
    const state = {root, add, testAll, refresh, live, cards, dialog, payload: {}, registry: [], instances: [], statuses: new Map(), testing: new Set(), testAllRunning: false, loading: null, returnFocus: null};
    stateByRoot.set(root, state);
    add.addEventListener("click", () => showChooser(state, add));
    testAll.addEventListener("click", () => runTestAll(state));
    refresh.addEventListener("click", () => load(state));
    dialog.addEventListener("keydown", event => dialogKeydown(state, event));
    dialog.addEventListener("cancel", event => { event.preventDefault(); closeDialog(state); });
    dialog.addEventListener("click", event => { if (event.target === dialog) closeDialog(state); });
    load(state);
    return root;
  }

  global.InkDropDownloadClients = {mount, _test: {GROUPS, LABELS, editorPayload, validateEditor}};
})(window);
