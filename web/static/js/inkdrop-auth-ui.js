(function () {
  "use strict";

  const state = {status: null, principal: null, oneTimeKey: ""};
  const FIRST_RUN_SETUP_HASH = "#settings?area=setup";
  const root = () => document.getElementById("inkdropAuthRoot");
  const shell = () => document.getElementById("inkdropAppShell");

  function routeIncompleteSetup() {
    if (state.status?.setup_required !== true) return false;
    if (String(window.location.hash || "").replace(/^#/, "").trim()) return false;
    const target = new URL(window.location.href);
    target.hash = FIRST_RUN_SETUP_HASH;
    if (window.history?.replaceState) window.history.replaceState(window.history.state, "", target);
    else window.location.hash = FIRST_RUN_SETUP_HASH;
    return true;
  }

  function principalIsAdministrator(principal = state.principal) {
    const role = String(principal?.user?.role || principal?.role || "").trim().toLowerCase();
    const scopes = Array.isArray(principal?.scopes)
      ? principal.scopes
      : Array.isArray(principal?.user?.scopes)
        ? principal.user.scopes
        : [];
    return role === "admin" || scopes.some(scope => String(scope || "").trim().toLowerCase() === "admin");
  }

  function publishAuthorizationState() {
    const administrator = principalIsAdministrator();
    document.body.dataset.inkdropAdministrator = administrator ? "true" : "false";
    window.dispatchEvent(new CustomEvent("inkdrop-auth-state", {detail: {administrator}}));
  }

  function authArtworkAllowed() {
    if (window.matchMedia?.("(prefers-reduced-data: reduce)").matches) return false;
    return window.navigator?.connection?.saveData !== true;
  }

  function authArtworkVariant() {
    const value = new Uint32Array(1);
    if (window.crypto?.getRandomValues) window.crypto.getRandomValues(value);
    else value[0] = Math.floor(Math.random() * 0xffffffff);
    return String((value[0] % 4) + 1);
  }

  function applyAuthArtworkPreference(host) {
    if (!host) return;
    const allowed = authArtworkAllowed();
    host.dataset.authArt = allowed ? "project" : "off";
    if (allowed && !host.dataset.authArtVariant) host.dataset.authArtVariant = authArtworkVariant();
    if (!allowed) delete host.dataset.authArtVariant;
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, character => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"})[character]);
  }

  async function request(path, options) {
    return window.InkDropApi.request(path, options);
  }

  function passwordPolicy() {
    const policy = state.status?.password_policy || {};
    return {
      minimum: Number(policy.minimum_length || policy.min_length || 0),
      maximum: Number(policy.maximum_length || policy.max_length || 0),
      compositionRequired: Boolean(policy.composition_required),
      unicodeAllowed: policy.unicode_allowed !== false,
      spacesAllowed: policy.spaces_allowed !== false,
    };
  }

  function applyPasswordPolicy(form) {
    if (!form) return;
    const policy = passwordPolicy();
    form.querySelectorAll('input[type="password"][name="password"], input[type="password"][name="new_password"], input[type="password"][name="confirm"], input[type="password"][name="confirm_password"]').forEach(input => {
      if (policy.minimum) input.minLength = policy.minimum;
      if (policy.maximum) input.maxLength = policy.maximum;
    });
    const target = form.querySelector("[data-password-policy]");
    if (target) {
      const limits = policy.minimum ? `At least ${policy.minimum} characters` : "Use a strong password";
      const maximum = policy.maximum ? ` and no more than ${policy.maximum}` : "";
      const freedoms = !policy.compositionRequired ? " No special character or composition rule is required." : "";
      target.textContent = `${limits}${maximum}.${freedoms}${policy.spacesAllowed && policy.unicodeAllowed ? " Spaces and Unicode are allowed." : ""}`;
    }
  }

  function field(label, type, name, autocomplete) {
    return `<label class="inkdrop-auth-field"><span>${label}</span><input type="${type}" name="${name}" autocomplete="${autocomplete || "off"}" required></label>`;
  }

  function message(form, text, tone) {
    const node = form.querySelector("[data-auth-message]");
    if (!node) return;
    node.textContent = text || "";
    node.className = `inkdrop-auth-message ${tone || ""}`;
  }

  function authCard(title, copy, formHtml) {
    const host = root();
    if (!host) return null;
    applyAuthArtworkPreference(host);
    host.innerHTML = `<section class="inkdrop-auth-card" role="dialog" aria-modal="true" aria-labelledby="inkdropAuthTitle"><img src="/inkdrop-logo-mark.png?v=20260728-opaque-logo" alt="" class="inkdrop-auth-logo"><h1 id="inkdropAuthTitle">${escapeHtml(title)}</h1><p>${escapeHtml(copy)}</p>${formHtml}</section>`;
    host.hidden = false;
    return host.querySelector("form");
  }

  async function login(username, password) {
    await request("/api/auth/login", {method: "POST", body: {username, password}});
    await validateSession();
  }

  function renderBootstrap() {
    const form = authCard("Create the first administrator", "Set up the account that will manage this InkDrop instance.", `<form class="inkdrop-auth-form">${field("Setup code", "text", "credential", "one-time-code")}<p class="inkdrop-auth-secondary">Find the setup code in <code>bootstrap-token.txt</code> inside your InkDrop config folder.</p>${field("Username", "text", "username", "username")}${field("Password", "password", "password", "new-password")}${field("Confirm password", "password", "confirm", "new-password")}<p class="inkdrop-auth-secondary" data-password-policy></p><div class="inkdrop-auth-message" data-auth-message role="status"></div><button type="submit">Create administrator</button></form>`);
    applyPasswordPolicy(form);
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const submit = form.querySelector("button[type=submit]");
      const credential = form.elements.credential.value.trim();
      const username = form.elements.username.value.trim();
      const password = form.elements.password.value;
      if (!credential) return message(form, "Enter the setup code from bootstrap-token.txt in your InkDrop config folder.", "bad");
      if (password !== form.elements.confirm.value) return message(form, "Passwords do not match.", "bad");
      submit.disabled = true;
      message(form, "Creating administrator…");
      try {
        await request("/api/auth/bootstrap", {method: "POST", body: {credential, username, password, role: "admin"}});
        await login(username, password);
      } catch (error) {
        message(form, error.message, "bad");
        submit.disabled = false;
      } finally {
        form.reset();
      }
    });
  }

  function renderLogin(reason) {
    const external = state.status?.external_auth || {};
    const builtIn = state.status?.built_in_auth || {};
    if (!builtIn.enabled && external.enabled) {
      const form = authCard("External authentication", external.ready ? "Sign in through the configured identity proxy, then reload InkDrop." : external.next_action || "External authentication is not ready.", `<form class="inkdrop-auth-form"><div class="inkdrop-auth-message" data-auth-message role="status"></div><button type="submit">Check session</button></form>`);
      form.addEventListener("submit", event => { event.preventDefault(); validateSession(); });
      return;
    }
    const form = authCard("Sign in to InkDrop", reason || "Use your InkDrop administrator account.", `<form class="inkdrop-auth-form">${field("Username", "text", "username", "username")}${field("Password", "password", "password", "current-password")}<div class="inkdrop-auth-message" data-auth-message role="status"></div><button type="submit">Sign in</button>${external.enabled ? '<p class="inkdrop-auth-secondary">External authentication is also enabled.</p>' : ""}<p class="inkdrop-auth-secondary">Locked out? Recovering access needs local access to this server -- see the password recovery section of the install documentation.</p></form>`);
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const submit = form.querySelector("button[type=submit]");
      submit.disabled = true;
      message(form, "Signing in…");
      try {
        await login(form.elements.username.value.trim(), form.elements.password.value);
      } catch (error) {
        const wait = error.retryAfter ? ` Try again in ${error.retryAfter} seconds.` : "";
        message(form, `${error.message}.${wait}`.replace("..", "."), "bad");
        submit.disabled = false;
        form.elements.password.value = "";
        form.elements.password.focus();
      }
    });
  }

  function revealApplication(principal) {
    state.principal = principal || null;
    publishAuthorizationState();
    routeIncompleteSetup();
    root().hidden = true;
    shell().hidden = false;
    document.documentElement.classList.remove("inkdrop-auth-pending");
    installAccountButton();
    window.__inkdropAuthReady = true;
    window.dispatchEvent(new CustomEvent("inkdrop-auth-ready"));
  }

  async function validateSession() {
    try {
      const payload = await request("/api/auth/session");
      if (!payload.authenticated) throw Object.assign(new Error("Session expired"), {status: 401});
      revealApplication(payload.principal);
    } catch (error) {
      // A 401 here means "not signed in", which on a first-ever visit is not
      // an expiry -- there was never a session to expire. This path used to
      // say "Your session expired." to everyone opening InkDrop for the first
      // time. The two cases are indistinguishable from the browser (and this
      // module is deliberately barred from persisting anything to tell them
      // apart), so use the neutral prompt. The genuine expiry cases are the
      // two below, which only fire once the app is already open and running.
      if (error.status === 401) renderLogin();
      else renderFatal(error.message);
    }
  }

  function renderFatal(text) {
    const form = authCard("InkDrop authentication unavailable", text || "The authentication service did not respond.", `<form class="inkdrop-auth-form"><button type="submit">Retry</button></form>`);
    form.addEventListener("submit", event => { event.preventDefault(); start(); });
  }

  function installAccountButton() {
    if (document.querySelector("[data-inkdrop-account]")) return;
    const nav = document.querySelector(".arr-nav");
    if (!nav) return;
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.inkdropAccount = "true";
    button.dataset.arrIconKey = "account";
    button.textContent = "Account";
    button.addEventListener("click", openAccountDialog);
    nav.appendChild(button);
  }

  function accountDialog() {
    let dialog = document.getElementById("inkdropAccountDialog");
    if (dialog) return dialog;
    dialog = document.createElement("dialog");
    dialog.id = "inkdropAccountDialog";
    dialog.className = "inkdrop-account-dialog";
    dialog.innerHTML = `<div class="inkdrop-account-head"><div><strong>Account & Security</strong><span data-auth-mode></span></div><button type="button" data-close aria-label="Close account settings">×</button></div><div class="inkdrop-account-tabs"><button type="button" data-tab="account">Account</button><button type="button" data-tab="keys">API Keys</button></div><div data-account-panel></div>`;
    dialog.querySelector("[data-close]").addEventListener("click", closeAccountDialog);
    dialog.addEventListener("cancel", event => { event.preventDefault(); closeAccountDialog(); });
    dialog.querySelectorAll("[data-tab]").forEach(button => button.addEventListener("click", () => renderAccountPanel(button.dataset.tab)));
    document.body.appendChild(dialog);
    return dialog;
  }

  async function openAccountDialog() {
    const dialog = accountDialog();
    dialog.querySelector("[data-auth-mode]").textContent = `Mode: ${(state.status?.mode || "unknown").replaceAll("_", " ")}`;
    renderAccountPanel("account");
    dialog.showModal();
  }

  function closeAccountDialog() {
    state.oneTimeKey = "";
    const dialog = document.getElementById("inkdropAccountDialog");
    if (dialog) { dialog.querySelector("[data-account-panel]").textContent = ""; dialog.close(); }
  }

  function renderAccountPanel(tab) {
    const panel = accountDialog().querySelector("[data-account-panel]");
    if (tab === "keys") return renderApiKeys(panel);
    const user = state.principal?.user || {};
    const sessionLogin = state.principal?.method === "session";
    const expiresAt = state.principal?.session?.expires_at;
    const sessionDetail = sessionLogin && expiresAt
      ? `<span> · Current session expires ${escapeHtml(new Date(Number(expiresAt) * 1000).toLocaleString())}</span>`
      : "";
    panel.innerHTML = `<section class="inkdrop-account-section"><div class="inkdrop-account-summary"><h2>${escapeHtml(user.username || "Authenticated user")}</h2><p><span>Authentication: ${escapeHtml(state.principal?.method || state.status?.mode || "unknown")}</span>${sessionDetail}</p></div>${sessionLogin ? `<form class="inkdrop-password-form">${field("Current password", "password", "current_password", "current-password")}${field("New password", "password", "new_password", "new-password")}${field("Confirm new password", "password", "confirm_password", "new-password")}<p class="inkdrop-auth-secondary" data-password-policy></p><label class="inkdrop-auth-check"><input type="checkbox" name="revoke_other_sessions" checked> Sign out other sessions</label><div data-auth-message role="status"></div><button type="submit">Change password</button></form>` : '<p>Password changes require a built-in InkDrop session.</p>'}<button type="button" data-logout>Sign out</button></section>`;
    panel.querySelector("[data-logout]").addEventListener("click", logout);
    const form = panel.querySelector(".inkdrop-password-form");
    if (form) {
      applyPasswordPolicy(form);
      form.addEventListener("submit", changePassword);
    }
  }

  async function changePassword(event) {
    event.preventDefault();
    const form = event.currentTarget;
    if (form.elements.new_password.value !== form.elements.confirm_password.value) return message(form, "New passwords do not match.", "bad");
    try {
      await request("/api/auth/password", {method: "POST", body: {current_password: form.elements.current_password.value, new_password: form.elements.new_password.value, revoke_other_sessions: form.elements.revoke_other_sessions.checked}});
      form.reset();
      message(form, "Password changed.", "good");
    } catch (error) { message(form, error.message, "bad"); }
  }

  async function logout() {
    try { await request("/api/auth/logout", {method: "POST", body: {}}); } catch (_error) {}
    state.principal = null;
    publishAuthorizationState();
    closeAccountDialog();
    shell().hidden = true;
    document.documentElement.classList.add("inkdrop-auth-pending");
    renderLogin();
  }

  function maskedFingerprint(value) {
    const text = String(value || "");
    return text ? `${text.slice(0, 6)}…${text.slice(-6)}` : "Unavailable";
  }

  async function renderApiKeys(panel) {
    panel.innerHTML = '<section class="inkdrop-account-section"><h2>API Keys</h2><p>Loading API keys…</p></section>';
    try {
      const payload = await request("/api/auth/api-keys");
      const keys = payload.api_keys || [];
      panel.innerHTML = `<section class="inkdrop-account-section"><h2>API Keys</h2><p>Keys authenticate API clients. Raw keys are shown only once.</p><form class="inkdrop-api-key-form">${field("Name", "text", "name", "off")}<label class="inkdrop-auth-field"><span>Description</span><input name="description" type="text"></label><label class="inkdrop-auth-field"><span>Expiration</span><select name="expiration"><option value="2592000">30 days</option><option value="7776000">90 days</option><option value="15552000">180 days</option><option value="31622400">1 year</option><option value="none">No expiration</option></select></label><fieldset><legend>Scopes</legend>${["read", "settings", "acquisition", "maintenance", "admin"].map(scope => `<label class="inkdrop-auth-check"><input type="checkbox" name="scope" value="${scope}" ${scope === "read" ? "checked" : ""}> ${scope}</label>`).join("")}</fieldset><div data-auth-message role="status"></div><button type="submit">Create API key</button></form><div class="inkdrop-api-key-list">${keys.length ? keys.map(key => {
        const stateLabel = key.revoked_at ? "Revoked" : (key.expired ? "Expired" : "Active");
        const expiration = key.expires_at ? `Expires ${new Date(Number(key.expires_at) * 1000).toLocaleDateString()}` : "No expiration";
        return `<article><div><strong>${escapeHtml(key.name)}</strong><span>${escapeHtml(key.preview || maskedFingerprint(key.fingerprint))} · ${escapeHtml(maskedFingerprint(key.fingerprint))}</span><small>${escapeHtml((key.scopes || []).join(", "))} · ${escapeHtml(stateLabel)} · ${escapeHtml(expiration)}</small></div>${key.enabled ? `<button type="button" data-revoke-key="${escapeHtml(key.id)}">Revoke</button>` : ""}</article>`;
      }).join("") : "<p>No API keys have been created.</p>"}</div></section>`;
      panel.querySelector(".inkdrop-api-key-form").addEventListener("submit", createApiKey);
      panel.querySelectorAll("[data-revoke-key]").forEach(button => button.addEventListener("click", () => revokeApiKey(button.dataset.revokeKey, panel)));
    } catch (error) { panel.innerHTML = `<section class="inkdrop-account-section"><h2>API Keys</h2><p class="bad">${escapeHtml(error.message)}</p></section>`; }
  }

  async function createApiKey(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const scopes = Array.from(form.querySelectorAll('input[name="scope"]:checked')).map(input => input.value);
    const expiration = form.elements.expiration.value;
    const body = {name: form.elements.name.value.trim(), description: form.elements.description.value.trim(), scopes};
    if (expiration !== "none") body.expires_in_seconds = Number(expiration);
    try {
      const payload = await request("/api/auth/api-keys", {method: "POST", body});
      state.oneTimeKey = payload.api_key?.key || "";
      form.reset();
      showOneTimeKey(form.closest("[data-account-panel]"));
    } catch (error) { message(form, error.message, "bad"); }
  }

  function showOneTimeKey(panel) {
    panel.innerHTML = `<section class="inkdrop-account-section inkdrop-one-time-key"><h2>Copy this key now</h2><p>InkDrop will not show this key again after this screen closes.</p><output data-one-time-key></output><div data-auth-message role="status"></div><button type="button" data-copy-key>Copy key</button><button type="button" data-key-done>Done</button></section>`;
    panel.querySelector("[data-one-time-key]").textContent = state.oneTimeKey;
    panel.querySelector("[data-copy-key]").addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(state.oneTimeKey); message(panel, "Key copied. Store it securely.", "good"); }
      catch (_error) { message(panel, "Copy failed. Select the key manually before leaving this screen.", "bad"); }
    });
    panel.querySelector("[data-key-done]").addEventListener("click", () => { state.oneTimeKey = ""; renderApiKeys(panel); });
  }

  async function revokeApiKey(id, panel) {
    if (!id || !window.confirm("Revoke this API key? Clients using it will stop working.")) return;
    try { await request(`/api/auth/api-keys/${encodeURIComponent(id)}/revoke`, {method: "POST", body: {}}); renderApiKeys(panel); }
    catch (error) { panel.querySelector(".inkdrop-account-section").insertAdjacentHTML("afterbegin", `<p class="inkdrop-auth-message bad">${escapeHtml(error.message)}</p>`); }
  }

  async function start() {
    const host = root();
    if (!host || !shell()) return;
    applyAuthArtworkPreference(host);
    host.hidden = false;
    host.innerHTML = '<section class="inkdrop-auth-card"><p>Checking InkDrop authentication…</p></section>';
    try {
      const payload = await request("/api/auth/status");
      state.status = payload.auth || payload;
      if (state.status.built_in_auth?.bootstrap_required) return renderBootstrap();
      if (!state.status.required && !state.status.setup_required) return revealApplication(null);
      await validateSession();
    } catch (error) { renderFatal(error.message); }
  }

  window.InkDropAuthUI = {
    start,
    validateSession,
    openAccountDialog,
    authArtworkAllowed,
    authArtworkVariant,
    isAdministrator: principalIsAdministrator,
  };
  window.addEventListener("inkdrop:session-expired", () => {
    if (!window.__inkdropAuthReady) return;
    state.principal = null;
    publishAuthorizationState();
    shell().hidden = true;
    document.documentElement.classList.add("inkdrop-auth-pending");
    renderLogin("Your session expired. Sign in again, then retry what you were doing.");
  });
  document.addEventListener("DOMContentLoaded", start, {once: true});
  window.setInterval(() => {
    if (!window.__inkdropAuthReady || document.hidden || state.status?.required === false) return;
    request("/api/auth/session").catch(error => {
      if (error.status !== 401) return;
      state.principal = null;
      publishAuthorizationState();
      shell().hidden = true;
      document.documentElement.classList.add("inkdrop-auth-pending");
      renderLogin("Your session expired. Sign in again.");
    });
  }, 60000);
})();
