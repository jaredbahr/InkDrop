// InkDrop mobile status view (/m). Standalone script -- not part of the
// desktop shell's JS bundle. It reads status and Manual Review from the same
// JSON APIs the desktop app uses, and writes through those same endpoints:
// adding a series goes to /api/{provider}/search + /api/{provider}/add, and a
// Manual Review decision goes to /api/manual-review/{approve,approve-pack,
// approve-local-file,bad-match,ignore}. No mobile-only backend behaviour
// exists -- every write here is a call the desktop shell already makes.
(() => {
  "use strict";

  const REFRESH_INTERVAL_MS = 45000;
  const MANUAL_REVIEW_LIMIT = 30;
  const SEARCH_LIMIT = 8;
  // The whole library in one read, matching the desktop shell's own
  // INKDROP_SERIES_FULL_LOAD_LIMIT: series has no server-side title search, so
  // both surfaces filter the full compact list client-side. Fetched once per
  // session here rather than on every screen load -- see loadCurrentScreen().
  const SERIES_LOAD_LIMIT = 5000;
  // The Home rail. The series endpoint already orders attention-first
  // (needs-you, then downloading, importing, wanted, then title), so a small
  // limit returns the series with the most outstanding work rather than an
  // arbitrary slice -- no client-side sort, and ~20KB instead of the Series
  // screen's whole-library read. Deliberately not sharing that screen's cache:
  // Home is the first thing that paints, and it must not wait on the 5000-row
  // response to show anything.
  const HOME_SERIES_LIMIT = 8;
  // Cap what is put in the DOM at once. The filter runs over every loaded row;
  // this only bounds how many cards a phone has to lay out for a broad query.
  const SERIES_RENDER_LIMIT = 40;
  const SEARCH_PROVIDERS = [
    { id: "mangadex", label: "MangaDex", endpoint: "/api/mangadex/search" },
    { id: "comicvine", label: "ComicVine", endpoint: "/api/comicvine/search" },
    { id: "metron", label: "Metron", endpoint: "/api/metron/search" },
  ];
  const ADD_ENDPOINTS = {
    mangadex: "/api/mangadex/add",
    metron: "/api/metron/add",
    comicvine: "/api/comicvine/add",
  };
  // Same status tokens the desktop add-series flow treats as "landed, but not
  // cleanly" / "did not land" -- copied so mobile reports the same outcome for
  // the same response rather than calling every 200 a success.
  const ADD_HARD_FAILURE_STATUSES = ["failed", "partial_failure", "add_incomplete"];
  const ADD_WARN_STATUSES = [
    "added_no_missing",
    "added_already_satisfied",
    "added_but_issue_fetch_failed",
    "added_but_queue_empty",
    "queued_autopilot_failed",
    "added_no_queue",
  ];

  const els = {};
  let currentScreen = "home";
  let refreshTimer = null;
  let loadToken = 0;
  let searchResults = [];
  let searchToken = 0;
  let stuckRows = [];
  let seriesRows = [];
  let seriesShown = [];
  let seriesTotal = 0;
  let seriesLoading = false;
  let homeSeriesRows = [];
  let homeSeriesTotal = 0;
  // review_id / series id -> the outcome sentence for an action already taken
  // this session, so it survives the re-render that follows the action.
  const reviewNotes = new Map();
  const seriesNotes = new Map();

  function byId(id) {
    return document.getElementById(id);
  }

  function cacheEls() {
    els.bootError = byId("mobileBootError");
    els.login = byId("mobileLogin");
    els.loginForm = byId("mobileLoginForm");
    els.loginError = byId("mobileLoginError");
    els.username = byId("mobileUsername");
    els.password = byId("mobilePassword");
    els.nav = byId("mobileNav");
    els.refreshBtn = byId("mobileRefreshBtn");
    els.desktopLink = byId("mobileDesktopLink");
    els.home = byId("mobileHome");
    els.homeContent = byId("mobileHomeContent");
    els.series = byId("mobileSeries");
    els.seriesForm = byId("mobileSeriesForm");
    els.seriesQuery = byId("mobileSeriesQuery");
    els.seriesStatus = byId("mobileSeriesStatus");
    els.seriesResults = byId("mobileSeriesResults");
    els.add = byId("mobileAdd");
    els.addForm = byId("mobileAddForm");
    els.addQuery = byId("mobileAddQuery");
    els.addSubmit = byId("mobileAddSubmit");
    els.addAuto = byId("mobileAddAuto");
    els.addStatus = byId("mobileAddStatus");
    els.addResults = byId("mobileAddResults");
    els.stuck = byId("mobileStuck");
    els.stuckContent = byId("mobileStuckContent");
    els.stuckBadge = byId("mobileStuckBadge");
    els.sheet = byId("mobileSheet");
    els.sheetTitle = byId("mobileSheetTitle");
    els.sheetCopy = byId("mobileSheetCopy");
    els.sheetSubject = byId("mobileSheetSubject");
    els.sheetConfirm = byId("mobileSheetConfirm");
    els.sheetCancel = byId("mobileSheetCancel");
    els.toast = byId("mobileToast");
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[ch]));
  }

  function humanizeToken(value) {
    const text = String(value || "").trim();
    if (!text) return "";
    return text.replace(/[_-]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  }

  function fmtMinutes(minutes) {
    const n = Number(minutes);
    if (!isFinite(n) || n < 0) return null;
    if (n < 1) return "just now";
    if (n < 60) return `${Math.round(n)}m ago`;
    const hours = n / 60;
    if (hours < 48) return `${Math.round(hours)}h ago`;
    return `${Math.round(hours / 24)}d ago`;
  }

  async function fetchJson(path) {
    const res = await fetch(path, {
      headers: { Accept: "application/json" },
      cache: "no-store",
    });
    if (res.status === 401) {
      const err = new Error("unauthenticated");
      err.unauthenticated = true;
      throw err;
    }
    let data = null;
    try {
      data = await res.json();
    } catch (parseErr) {
      throw new Error(`bad response from ${path}`);
    }
    if (!res.ok && !data) throw new Error(`request failed: ${path}`);
    return data;
  }

  // Write counterpart to fetchJson. Deliberately delegates to the shared
  // window.InkDropApi.request (/static/js/inkdrop-api.js, the same helper the
  // desktop shell mutates through) rather than calling fetch() directly:
  // every mutation has to carry the CSRF cookie back as the X-InkDrop-CSRF
  // header, and the cookie/header names come off /api/auth/status rather than
  // being fixed. A second, mobile-only copy of that contract is exactly how a
  // page ends up posting a request the server refuses. It also gives mobile
  // the same body-level `ok: false` handling -- these endpoints report a
  // refused action that way on HTTP 200, so an action that only checked
  // res.ok would report "done" for a refusal.
  async function postJson(path, body) {
    const api = window.InkDropApi;
    if (!api || typeof api.request !== "function") {
      throw new Error("InkDrop's API helper didn't load. Reload the page and try again.");
    }
    try {
      return await api.request(path, { method: "POST", body: body || {} });
    } catch (err) {
      if (err && (err.status === 401 || err.code === "session_expired")) {
        const wrapped = new Error(err.message || "unauthenticated");
        wrapped.unauthenticated = true;
        throw wrapped;
      }
      throw err;
    }
  }

  function setSpinning(spinning) {
    if (!els.refreshBtn) return;
    els.refreshBtn.classList.toggle("m-spinning", !!spinning);
  }

  let toastTimer = null;
  function toast(message, ok) {
    if (!els.toast || !message) return;
    els.toast.textContent = message;
    els.toast.classList.toggle("m-toast-bad", ok === false);
    els.toast.hidden = false;
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { els.toast.hidden = true; }, 5000);
  }

  // --- Confirm sheet ----------------------------------------------------

  let sheetResolve = null;

  function closeSheet(result) {
    if (!els.sheet) return;
    els.sheet.hidden = true;
    els.sheet.classList.remove("m-sheet-open");
    const resolve = sheetResolve;
    sheetResolve = null;
    if (resolve) resolve(!!result);
  }

  function confirmSheet(opts) {
    const config = opts || {};
    if (!els.sheet) return Promise.resolve(true);
    // A second confirm while one is open would strand the first promise.
    if (sheetResolve) closeSheet(false);
    els.sheetTitle.textContent = config.title || "Are you sure?";
    els.sheetSubject.textContent = config.subject || "";
    els.sheetSubject.hidden = !config.subject;
    els.sheetCopy.textContent = config.copy || "";
    els.sheetConfirm.textContent = config.confirmLabel || "Confirm";
    els.sheetConfirm.classList.toggle("m-btn-danger", config.tone === "bad");
    els.sheet.hidden = false;
    els.sheet.classList.add("m-sheet-open");
    els.sheetConfirm.focus();
    return new Promise((resolve) => { sheetResolve = resolve; });
  }

  // --- Auth -----------------------------------------------------------

  async function boot() {
    try {
      const data = await fetchJson("/api/auth/status");
      const auth = (data && data.auth) || {};
      // setup_required wins over everything else, including
      // required === false: that combination means no enforcement is
      // possible yet (no administrator exists, or external auth isn't
      // ready), not "operational APIs are open to anyone who can reach
      // this page." This page has no bootstrap form, so send them to "/"
      // where the desktop shell owns first-run setup. The inline script in
      // MOBILE_HTML already does this before this script even loads --
      // this is the fallback for a request whose server-rendered flag was
      // already stale by the time it reached the browser.
      if (auth.setup_required) {
        location.replace("/" + location.search);
        return;
      }
      if (auth.required === false || auth.session_authenticated) {
        showApp();
      } else {
        showLogin();
      }
    } catch (err) {
      showLogin();
    }
  }

  function showLogin() {
    stopAutoRefresh();
    closeSheet(false);
    els.login.hidden = false;
    els.home.hidden = true;
    els.series.hidden = true;
    els.add.hidden = true;
    els.stuck.hidden = true;
    els.nav.hidden = true;
    els.refreshBtn.hidden = true;
    els.username.focus();
  }

  function showApp() {
    els.login.hidden = true;
    els.nav.hidden = false;
    els.refreshBtn.hidden = false;
    switchScreen(currentScreen, { force: true });
    startAutoRefresh();
  }

  async function handleLogin(event) {
    event.preventDefault();
    els.loginError.hidden = true;
    const submitBtn = els.loginForm.querySelector("button[type=submit]");
    submitBtn.disabled = true;
    try {
      const res = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({
          username: els.username.value.trim(),
          password: els.password.value,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data && data.ok !== false) {
        els.password.value = "";
        showApp();
        return;
      }
      els.loginError.textContent = humanizeToken(data && data.error) || "Sign-in failed. Check your username and password.";
      els.loginError.hidden = false;
    } catch (err) {
      els.loginError.textContent = "Couldn't reach InkDrop. Check your connection and try again.";
      els.loginError.hidden = false;
    } finally {
      submitBtn.disabled = false;
    }
  }

  // --- Navigation -------------------------------------------------------

  function switchScreen(name, opts) {
    const force = !!(opts && opts.force);
    if (name === currentScreen && !force) return;
    currentScreen = name;
    els.home.hidden = name !== "home";
    els.series.hidden = name !== "series";
    els.add.hidden = name !== "add";
    els.stuck.hidden = name !== "stuck";
    els.nav.querySelectorAll(".m-nav-btn").forEach((btn) => {
      btn.classList.toggle("m-nav-active", btn.dataset.screen === name);
    });
    loadCurrentScreen();
  }

  function loadCurrentScreen(opts) {
    const force = !!(opts && opts.force);
    if (currentScreen === "home") loadHome();
    else if (currentScreen === "stuck") loadStuck();
    // The series list is the one expensive read on this page -- the whole
    // library in one response, the same full load the desktop does for its
    // own client-side search. It is fetched once and reused for the session,
    // so only an explicit refresh pays for it again; the 45s tick must not.
    else if (currentScreen === "series") loadSeries({ force });
    // The Add screen holds a half-typed query and a result list the operator
    // is still reading -- there is nothing to poll, and re-rendering it on the
    // 45s tick would throw both away mid-decision.
    if (currentScreen !== "stuck") refreshStuckBadge();
  }

  function startAutoRefresh() {
    stopAutoRefresh();
    refreshTimer = setInterval(() => {
      if (document.hidden) return;
      loadCurrentScreen();
    }, REFRESH_INTERVAL_MS);
    document.addEventListener("visibilitychange", onVisibilityChange);
  }

  function stopAutoRefresh() {
    if (refreshTimer) clearInterval(refreshTimer);
    refreshTimer = null;
    document.removeEventListener("visibilitychange", onVisibilityChange);
  }

  function onVisibilityChange() {
    if (!document.hidden) loadCurrentScreen();
  }

  // --- Home screen --------------------------------------------------------

  function statusDotClass(status) {
    const s = String(status || "").toLowerCase();
    if (s.includes("problem") || s.includes("fail") || s.includes("error")) return "m-status-problem";
    if (s.includes("warn") || s.includes("attention") || s.includes("degraded")) return "m-status-warn";
    if (s.includes("ok") || s.includes("healthy") || s.includes("good") || s.includes("idle") || s.includes("running")) return "m-status-ok";
    return "";
  }

  function tile(value, label, attention) {
    return `<div class="m-tile">
      <div class="m-tile-value${attention ? " m-tile-attn" : ""}">${escapeHtml(value)}</div>
      <div class="m-tile-label">${escapeHtml(label)}</div>
    </div>`;
  }

  async function loadHome() {
    const token = ++loadToken;
    setSpinning(true);
    if (!els.homeContent.dataset.loaded) {
      els.homeContent.innerHTML = '<div class="m-loading">Loading status&hellip;</div>';
    }
    try {
      // The rail is the only one of the three that Home can render without:
      // a failed series read drops the rail and still paints status and tiles,
      // rather than blanking the screen someone opened to check on things.
      const [status, activity, series] = await Promise.all([
        fetchJson("/status.json"),
        fetchJson("/api/inkdrop-activity/summary").catch(() => null),
        fetchJson(
          `/api/inkdrop-state/series?limit=${HOME_SERIES_LIMIT}&summary=compact&rows=compact`
        ).catch(() => null),
      ]);
      if (token !== loadToken) return;
      const seriesView = (series && series.view) || {};
      homeSeriesRows = Array.isArray(seriesView.rows) ? seriesView.rows : [];
      homeSeriesTotal = Number(seriesView.total_count ?? homeSeriesRows.length) || homeSeriesRows.length;
      renderHome(status, activity);
      els.homeContent.dataset.loaded = "1";
    } catch (err) {
      if (token !== loadToken) return;
      if (err.unauthenticated) { showLogin(); return; }
      if (!els.homeContent.dataset.loaded) {
        els.homeContent.innerHTML = '<p class="m-error">Couldn\'t load status. Pull down or tap refresh to try again.</p>';
      }
    } finally {
      if (token === loadToken) setSpinning(false);
    }
  }

  function renderHome(status, activity) {
    status = status || {};
    const dotClass = statusDotClass(status.status);
    const detail = status.detail ? `<span class="m-status-detail">${escapeHtml(status.detail)}</span>` : "";
    const lastImport = fmtMinutes(status.last_import_minutes);

    const tiles = [
      tile(status.inkdrop_state_series_count ?? "-", "Series"),
      tile(status.inkdrop_state_wanted_count ?? "-", "Wanted"),
      tile(activity && activity.active_total != null ? activity.active_total : (status.inkdrop_state_queue_count ?? "-"), "Active downloads"),
      tile(status.manual_review_actionable_count ?? status.manual_review_count ?? "-", "Needs attention", (status.manual_review_actionable_count || 0) > 0),
      tile((status.failed_download_count || 0) + (status.failed_import_count || 0), "Failed", ((status.failed_download_count || 0) + (status.failed_import_count || 0)) > 0),
      tile(activity && activity.ready_to_import != null ? activity.ready_to_import : "-", "Ready to import"),
    ].join("");

    const metaHtml = lastImport
      ? `<div class="m-meta-row"><span>Last import ${escapeHtml(lastImport)}</span></div>`
      : "";

    els.homeContent.innerHTML = `
      <div class="m-status-banner">
        <span class="m-status-dot ${dotClass}"></span>
        <span class="m-status-text">${escapeHtml(status.status ? humanizeToken(status.status) : "Status unavailable")}${detail}</span>
      </div>
      <div class="m-home-actions">
        <button type="button" class="m-btn-primary" data-mobile-goto="add">Add a series</button>
        <button type="button" class="m-btn-quiet" data-mobile-goto="series">Find a series</button>
      </div>
      <div class="m-tile-grid">${tiles}</div>
      ${renderHomeSeries()}
      ${metaHtml}
    `;
    setUpStatusDetailToggle();
  }

  // status.json's `detail` is written for the desktop status bar and can run
  // to a full paragraph. CSS clamps it to three lines; this adds the control
  // to see the rest -- but only when there IS a rest, so a one-line status
  // never grows a pointless "More" button. Measured rather than guessed from
  // string length: whether it overflows depends on the font and the device's
  // width, which only layout knows.
  function setUpStatusDetailToggle() {
    const banner = els.homeContent.querySelector(".m-status-banner");
    const detailEl = banner && banner.querySelector(".m-status-detail");
    if (!detailEl) return;
    if (detailEl.scrollHeight <= detailEl.clientHeight + 1) return;
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "m-status-more";
    toggle.setAttribute("aria-expanded", "false");
    toggle.textContent = "More";
    toggle.addEventListener("click", () => {
      const expanded = banner.classList.toggle("is-expanded");
      toggle.setAttribute("aria-expanded", expanded ? "true" : "false");
      toggle.textContent = expanded ? "Less" : "More";
    });
    detailEl.insertAdjacentElement("afterend", toggle);
  }

  // One pill per card, not the Series screen's full set: a rail card is a
  // glance, and the single most pressing number is what makes someone tap it.
  function homeSeriesPill(row) {
    const needsYou = Number(row.needs_you_count || 0);
    if (needsYou) return `<span class="m-pill m-pill-attn">${needsYou} needs you</span>`;
    const active = Number(row.active_queue_count || 0);
    if (active) return `<span class="m-pill">${active} in queue</span>`;
    const wanted = Number(row.wanted_count || 0);
    if (wanted) return `<span class="m-pill">${wanted} wanted</span>`;
    if (!row.monitored) return `<span class="m-pill">Not monitored</span>`;
    return `<span class="m-pill m-pill-quiet">Nothing outstanding</span>`;
  }

  function renderHomeSeries() {
    if (!homeSeriesRows.length) {
      // A real empty library, not a failed read -- loadHome() leaves the rows
      // empty for both, and pushing someone toward Add is the right answer
      // either way.
      return `<div class="m-section-head"><div class="m-section-title">Your series</div></div>
        <div class="m-empty">Nothing tracked yet. Tap <strong>Add a series</strong> to start one.</div>`;
    }
    const cards = homeSeriesRows
      .map((row) => {
        const title = String(row.title || "Untitled series");
        const cover = row.image
          ? `<img class="m-rail-img" src="${escapeHtml(row.image)}" alt="" loading="lazy" decoding="async">`
          : "";
        return `<button type="button" class="m-rail-card" data-home-series="${escapeHtml(title)}">
          <span class="m-rail-cover" data-letter="${escapeHtml(title.slice(0, 1).toUpperCase())}">${cover}</span>
          <span class="m-rail-title">${escapeHtml(title)}</span>
          <span class="m-rail-pills">${homeSeriesPill(row)}</span>
        </button>`;
      })
      .join("");
    const seeAll = homeSeriesTotal > homeSeriesRows.length
      ? `<button type="button" class="m-text-link" data-mobile-goto="series">All ${homeSeriesTotal}</button>`
      : "";
    return `<div class="m-section-head">
        <div class="m-section-title">Your series</div>
        ${seeAll}
      </div>
      <div class="m-rail">${cards}</div>`;
  }

  // --- Series screen ------------------------------------------------------

  // Mirrors the desktop's normalizeInkdropSeriesSearchText/
  // inkdropSeriesSearchHaystack pair so the phone finds a series on the same
  // typing the desktop does. Trimmed to the fields a compact row carries and
  // that someone would actually type on a phone -- library paths and reader
  // visibility counters are in the row but nobody searches by them.
  function normalizeSeriesSearchText(value) {
    return String(value || "").trim().toLowerCase().replace(/\s+/g, " ");
  }

  function seriesSearchHaystack(row) {
    const r = row || {};
    return normalizeSeriesSearchText([
      r.title,
      r.sort_title,
      r.publisher,
      r.metadata_provider,
      r.metadata_id,
      r.media_type,
      r.year,
      r.monitored ? "monitored" : "unmonitored paused",
      r.auto_grab ? "auto grab" : "",
      Number(r.wanted_count || 0) ? "wanted missing" : "",
      Number(r.active_queue_count || 0) ? "queue queued active" : "",
      Number(r.needs_you_count || 0) ? "attention manual review" : "",
    ].filter(Boolean).join(" "));
  }

  function filterSeriesRows(rows, query) {
    const normalized = normalizeSeriesSearchText(query);
    const source = Array.isArray(rows) ? rows.filter(Boolean) : [];
    if (!normalized) return source;
    const terms = normalized.split(" ").filter(Boolean);
    if (!terms.length) return source;
    return source.filter((row) => {
      const haystack = seriesSearchHaystack(row);
      return terms.every((term) => haystack.includes(term));
    });
  }

  function setSeriesStatus(message, tone) {
    if (!els.seriesStatus) return;
    els.seriesStatus.textContent = message || "";
    els.seriesStatus.hidden = !message;
    els.seriesStatus.className = "m-add-status" + (tone ? ` m-tone-${tone}` : "");
  }

  async function loadSeries(opts) {
    const force = !!(opts && opts.force);
    if (seriesRows.length && !force) {
      renderSeries();
      return;
    }
    if (seriesLoading) return;
    seriesLoading = true;
    setSpinning(true);
    if (!seriesRows.length) {
      els.seriesResults.innerHTML = '<div class="m-loading">Loading your library&hellip;</div>';
    }
    try {
      const payload = await fetchJson(
        `/api/inkdrop-state/series?limit=${SERIES_LOAD_LIMIT}&summary=compact&rows=compact`
      );
      const view = (payload && payload.view) || {};
      seriesRows = Array.isArray(view.rows) ? view.rows : [];
      seriesTotal = Number(view.total_count ?? seriesRows.length) || seriesRows.length;
      renderSeries();
    } catch (err) {
      if (err.unauthenticated) { showLogin(); return; }
      if (!seriesRows.length) {
        els.seriesResults.innerHTML = '<p class="m-error">Couldn\'t load your series. Tap refresh to try again.</p>';
      }
    } finally {
      seriesLoading = false;
      setSpinning(false);
    }
  }

  function seriesCountPills(row) {
    return [
      Number(row.wanted_count || 0) ? `<span class="m-pill">${Number(row.wanted_count)} wanted</span>` : "",
      Number(row.active_queue_count || 0) ? `<span class="m-pill">${Number(row.active_queue_count)} in queue</span>` : "",
      Number(row.needs_you_count || 0) ? `<span class="m-pill m-pill-attn">${Number(row.needs_you_count)} needs you</span>` : "",
      row.monitored ? "" : `<span class="m-pill">Not monitored</span>`,
    ].filter(Boolean).join("");
  }

  function renderSeries() {
    const query = els.seriesQuery ? els.seriesQuery.value : "";
    const matches = filterSeriesRows(seriesRows, query);
    if (!seriesRows.length) {
      els.seriesResults.innerHTML = '<div class="m-empty">No series are tracked yet. Use the Add tab to start one.</div>';
      setSeriesStatus("", "");
      return;
    }
    if (!matches.length) {
      const typed = String(query || "").trim();
      els.seriesResults.innerHTML = `<div class="m-empty">
        Nothing in your library matches that.
        ${typed ? `<div class="m-actions"><button type="button" class="m-btn-primary" data-series-add="1">Search metadata sources for &ldquo;${escapeHtml(typed)}&rdquo;</button></div>` : ""}
      </div>`;
      setSeriesStatus(`0 of ${seriesTotal} series`, "warn");
      return;
    }
    const shown = matches.slice(0, SERIES_RENDER_LIMIT);
    // Index against the filtered list, not seriesRows -- the click handler
    // reads the same array back, so the two must be built from one filter.
    seriesShown = shown;
    els.seriesResults.innerHTML = shown
      .map((row, index) => {
        const meta = [row.year || "", row.publisher || "", humanizeToken(row.media_type || "")]
          .filter(Boolean).join(" · ");
        const pills = seriesCountPills(row);
        const done = seriesNotes.get(String(row.id)) || "";
        return `<div class="m-item-card" data-series-card="${index}">
          <div class="m-item-title">${escapeHtml(row.title || "Untitled series")}</div>
          ${meta ? `<div class="m-item-reason">${escapeHtml(meta)}</div>` : ""}
          ${pills ? `<div class="m-item-row">${pills}</div>` : ""}
          <div class="m-actions">
            <button type="button" class="m-btn-primary" data-series-action="run" data-series-index="${index}">Search now</button>
            <button type="button" class="m-btn-quiet" data-series-action="monitor" data-series-index="${index}">${row.monitored ? "Stop monitoring" : "Monitor"}</button>
          </div>
          <p class="m-action-status${done ? " m-tone-good" : ""}" data-series-status="${index}"${done ? "" : " hidden"}>${escapeHtml(done)}</p>
        </div>`;
      })
      .join("");
    const suffix = matches.length > shown.length
      ? ` &mdash; showing the first ${shown.length}, keep typing to narrow it down`
      : "";
    setSeriesStatus("", "");
    els.seriesResults.insertAdjacentHTML(
      "beforeend",
      `<div class="m-more-note">${matches.length} of ${seriesTotal} series${suffix}</div>`
    );
  }

  async function runSeriesAction(action, index, button) {
    const row = seriesShown[index];
    const id = row && (row.id || row.series_id);
    if (!id) return;
    const title = row.title || "Series";
    const statusEl = els.seriesResults.querySelector(`[data-series-status="${index}"]`);
    const card = els.seriesResults.querySelector(`[data-series-card="${index}"]`);
    const buttons = card ? Array.from(card.querySelectorAll("button")) : [];
    const original = button ? button.textContent : "";
    let confirmed = true;
    if (action === "monitor" && row.monitored) {
      confirmed = await confirmSheet({
        title: "Stop monitoring this series?",
        subject: title,
        copy: "InkDrop stops looking for its missing issues. Nothing already in your library is removed, and you can monitor it again at any time.",
        confirmLabel: "Stop monitoring",
      });
    }
    if (!confirmed) return;
    buttons.forEach((b) => { b.disabled = true; });
    if (button) button.textContent = "Working…";
    if (statusEl) {
      statusEl.hidden = false;
      statusEl.className = "m-action-status m-tone-warn";
      statusEl.textContent = action === "run" ? "Queueing a search…" : "Saving…";
    }
    try {
      let message;
      if (action === "run") {
        const data = await postJson("/api/inkdrop-state/series/run", { id });
        const result = (data && data.result) || {};
        // Same fields the desktop's runSeriesSearch() reports from, so the
        // phone and the desktop describe the same run the same way.
        const dbQueue = result.dbQueue || {};
        const queue = result.queue || {};
        const queued = Number(queue.created || queue.updated || queue.resurrected || 0)
          || Number(dbQueue.queued || (dbQueue.queue_id ? 1 : 0));
        message = queued
          ? `Search queued (${queued} item${queued === 1 ? "" : "s"}).`
          : "Search queued. Nothing was missing, so no new rows were created.";
      } else {
        const next = !row.monitored;
        await postJson("/api/inkdrop-state/series/update", { id, monitored: next });
        row.monitored = next;
        message = next
          ? "Monitoring resumed. InkDrop will look for what is missing."
          : "No longer monitored.";
      }
      seriesNotes.set(String(id), message);
      toast(`${title}: ${message}`, true);
      renderSeries();
    } catch (err) {
      if (err.unauthenticated) { showLogin(); return; }
      buttons.forEach((b) => { b.disabled = false; });
      if (button) button.textContent = original;
      if (statusEl) {
        statusEl.hidden = false;
        statusEl.className = "m-action-status m-tone-bad";
        statusEl.textContent = err.message || "That action failed.";
      }
      toast(err.message || "That action failed.", false);
    }
  }

  // --- Add series screen --------------------------------------------------

  // Mirrors the desktop shell's normalizeSeriesLookupInput(): the providers
  // match far better on a plain title than on a punctuated filename-ish query.
  function normalizeSeriesLookupInput(text) {
    const value = String(text || "").trim();
    if (!value) return "";
    return value
      .replace(/[./:]/g, " ")
      .replace(/-+/g, " ")
      .replace(/\s+/g, " ")
      .trim()
      .slice(0, 180);
  }

  function seriesResultProvider(item) {
    const row = item || {};
    return String(
      row.provider || row.metadataProvider ||
      (row.mangadexId ? "mangadex" : row.metronId ? "metron" : "comicvine")
    ).toLowerCase();
  }

  function seriesResultKey(item) {
    const row = item || {};
    const provider = seriesResultProvider(row);
    const providerId = String(row.comicvineId || row.mangadexId || row.metronId || row.metadataId || row.id || "").trim().toLowerCase();
    if (provider && providerId) return `${provider}:${providerId}`;
    return `${provider || "unknown"}:${String(row.name || "").trim().toLowerCase()}:${String(row.year || "").trim()}`;
  }

  // Same ranking the desktop "All sources" search uses, so the phone and the
  // desktop offer the same top match for the same query.
  function seriesResultRank(item) {
    const row = item || {};
    const provider = seriesResultProvider(row);
    const score = Number(row.matchScore || 0);
    const providerWeight = provider === "mangadex" ? 32 : provider === "comicvine" ? 8 : provider === "metron" ? 4 : 0;
    const canMonitorWeight = row.canMonitor === false ? -40 : 0;
    return score + providerWeight + canMonitorWeight;
  }

  function mergeSeriesResults(groups) {
    const merged = new Map();
    for (const group of groups || []) {
      for (const item of (group && group.results) || []) {
        const key = seriesResultKey(item);
        const existing = merged.get(key);
        if (!existing || seriesResultRank(item) > seriesResultRank(existing)) {
          merged.set(key, item);
        }
      }
    }
    return Array.from(merged.values()).sort((a, b) => {
      const diff = seriesResultRank(b) - seriesResultRank(a);
      if (diff) return diff;
      return String(a.name || "").localeCompare(String(b.name || ""), undefined, { numeric: true, sensitivity: "base" });
    });
  }

  function setAddStatus(message, tone) {
    if (!els.addStatus) return;
    els.addStatus.textContent = message || "";
    els.addStatus.hidden = !message;
    els.addStatus.className = "m-add-status" + (tone ? ` m-tone-${tone}` : "");
  }

  async function handleSearch(event) {
    if (event) event.preventDefault();
    const raw = els.addQuery.value;
    const query = normalizeSeriesLookupInput(raw);
    if (!query) {
      setAddStatus("Enter a series title first.", "warn");
      return;
    }
    if (query !== raw) els.addQuery.value = query;
    const token = ++searchToken;
    els.addSubmit.disabled = true;
    setAddStatus("Searching metadata sources…", "");
    els.addResults.innerHTML = '<div class="m-loading">Searching&hellip;</div>';
    try {
      const groups = await Promise.all(SEARCH_PROVIDERS.map(async (source) => {
        try {
          const data = await postJson(source.endpoint, { query, limit: SEARCH_LIMIT });
          // A provider that reports "unconfigured" (Metron, off by default)
          // contributed nothing on purpose. It must read the same as a source
          // that was never in the list, not as a failure on every search.
          if (data.providerStatus === "unconfigured") {
            return { label: source.label, status: "unconfigured", results: [] };
          }
          if (data.providerStatus === "failed") {
            return { label: source.label, status: "failed", error: data.error || "Search failed", results: [] };
          }
          return { label: source.label, status: "ok", results: data.results || [] };
        } catch (err) {
          if (err.unauthenticated) throw err;
          return { label: source.label, status: "failed", error: err.message || String(err), results: [] };
        }
      }));
      if (token !== searchToken) return;
      searchResults = mergeSeriesResults(groups).slice(0, SEARCH_LIMIT);
      renderSearchResults();
      const failures = groups.filter((g) => g.status === "failed");
      const contributing = groups.filter((g) => g.status !== "unconfigured");
      const suffix = failures.length ? ` ${failures.map((g) => `${g.label}: ${g.error}`).join("; ")}` : "";
      if (!searchResults.length && failures.length === contributing.length) {
        setAddStatus(`No metadata source could answer.${suffix}`, "bad");
      } else {
        setAddStatus(
          `${searchResults.length} match${searchResults.length === 1 ? "" : "es"} across metadata sources.${suffix}`,
          failures.length ? "warn" : "",
        );
      }
    } catch (err) {
      if (token !== searchToken) return;
      if (err.unauthenticated) { showLogin(); return; }
      els.addResults.innerHTML = "";
      setAddStatus(err.message || "Search failed.", "bad");
    } finally {
      if (token === searchToken) els.addSubmit.disabled = false;
    }
  }

  function renderSearchResults() {
    if (!searchResults.length) {
      els.addResults.innerHTML = '<div class="m-empty">No matches. Try a shorter title.</div>';
      return;
    }
    els.addResults.innerHTML = searchResults
      .map((item, index) => {
        const provider = seriesResultProvider(item);
        const meta = [
          item.year ? String(item.year) : "",
          item.publisher || "",
          item.issueCount ? `${item.issueCount} issues` : "",
        ].filter(Boolean).join(" · ");
        const warning = item.editionWarning
          ? `<div class="m-item-warning">${escapeHtml(item.editionWarning)}</div>`
          : "";
        const blocked = item.canMonitor === false;
        return `<div class="m-item-card" data-result-index="${index}">
          <div class="m-item-title">${escapeHtml(item.name || "Untitled series")}</div>
          ${meta ? `<div class="m-item-reason">${escapeHtml(meta)}</div>` : ""}
          <div class="m-item-row">
            <span class="m-pill">${escapeHtml(humanizeToken(provider))}</span>
            ${item.matchNote ? `<span class="m-item-source">${escapeHtml(item.matchNote)}</span>` : ""}
          </div>
          ${warning}
          <div class="m-actions">
            <button type="button" class="m-btn-primary" data-add-index="${index}"${blocked ? " disabled" : ""}>
              ${blocked ? "Can't monitor" : "Add series"}
            </button>
          </div>
          <p class="m-action-status" data-add-status="${index}" hidden></p>
        </div>`;
      })
      .join("");
  }

  async function addSeriesAt(index, button) {
    const item = searchResults[index];
    if (!item) return;
    const provider = seriesResultProvider(item);
    const endpoint = ADD_ENDPOINTS[provider] || ADD_ENDPOINTS.comicvine;
    const statusEl = els.addResults.querySelector(`[data-add-status="${index}"]`);
    const setRowStatus = (text, tone) => {
      if (!statusEl) return;
      statusEl.textContent = text || "";
      statusEl.hidden = !text;
      statusEl.className = "m-action-status" + (tone ? ` m-tone-${tone}` : "");
    };
    const original = button ? button.textContent.trim() : "Add series";
    if (button) {
      button.disabled = true;
      button.textContent = "Adding…";
    }
    setRowStatus("Saving the series and creating queue rows…", "warn");
    try {
      const data = await postJson(endpoint, {
        volume: item,
        autoGrab: els.addAuto ? els.addAuto.checked : true,
      });
      const result = (data && data.result) || {};
      const verify = result.postAddVerification || {};
      const status = String(verify.status || result.status || "");
      const hardFailed = ADD_HARD_FAILURE_STATUSES.includes(status);
      const warned = ADD_WARN_STATUSES.includes(status);
      const message = verify.message || result.message ||
        (hardFailed ? "The series was not fully added." : "Series added and monitored.");
      setRowStatus(message, hardFailed ? "bad" : warned ? "warn" : "good");
      if (button) button.textContent = hardFailed ? "Retry" : "Added";
      toast(message, !hardFailed);
      // A new series changes the Wanted/queue tiles and can raise a review
      // row, so the badge and Home tiles must not keep showing pre-add counts.
      refreshStuckBadge();
      delete els.homeContent.dataset.loaded;
    } catch (err) {
      if (err.unauthenticated) { showLogin(); return; }
      setRowStatus(err.message || "Add failed.", "bad");
      if (button) button.textContent = "Retry";
      toast(err.message || "Add failed.", false);
    } finally {
      if (button) {
        button.disabled = false;
        if (button.textContent === "Adding…") button.textContent = original;
      }
    }
  }

  // --- Needs Attention (Manual Review) screen -----------------------------

  function reviewRowTitle(row) {
    const issue = row.issue_number ? ` #${row.issue_number}` : "";
    return `${row.series || "Unknown series"}${issue}`;
  }

  function reviewRowState(row) {
    return row.display_state_label || row.display_state || row.state || row.status || "";
  }

  function reviewRowReason(row) {
    return row.review_reason || row.reason || row.why_not_grabbed || row.activity_summary || "";
  }

  // Which write endpoint an approval on this row goes to. Same precedence the
  // desktop decision panel uses (pack > staged local file > guarded grab), and
  // gated on the server-computed can_approve* flags rather than a client-side
  // copy of the allowed-source list -- the server re-checks the source itself
  // in approve_manual_review(), and a second, drifting copy here is exactly
  // how a button ends up enabled for an action the backend will refuse.
  function approveEndpointFor(row) {
    if (row.can_approve_pack) return "/api/manual-review/approve-pack";
    if (row.can_approve_local_file) return "/api/manual-review/approve-local-file";
    if (row.can_approve) return "/api/manual-review/approve";
    return "";
  }

  function approveLabelFor(row) {
    if (row.can_approve_pack) return "Approve pack";
    if (row.can_approve_local_file) return "Import this file";
    return "Use this candidate";
  }

  function approveDisabledReason(row) {
    if (row.local_file_missing) {
      return "The staged file is no longer on disk, so there is nothing to import.";
    }
    return "InkDrop has not retained a candidate it can safely grab for this row. Open it on desktop to search manually.";
  }

  // Mirrors the desktop reviewAllowsReject() fallback: a reject re-searches
  // the series, so a row with no series has nothing to search for.
  function canReject(row) {
    return Boolean(row.review_id && row.source && (row.matched_series || row.series));
  }

  // What actually happened, in a sentence, rather than the raw status token
  // ("rejected_retry_started") the endpoints return. Reject in particular has
  // to say that the row stays put, because it does.
  function reviewActionMessage(action, result) {
    const r = result || {};
    if (action === "ignore") return "Removed from Needs Attention. No files were deleted.";
    if (action === "reject") {
      const retry = r.retry || {};
      if (retry.started) return "Blocked that release and started a new search. This stays listed until something better turns up.";
      if (retry.already_running) return "Blocked that release. A search for this series is already running.";
      return "Blocked that release. It stays listed until the next scheduled search.";
    }
    const status = String(r.status || "");
    if (status === "approved_imported") return "Imported that file into your library.";
    if (status === "approved_grabbed") {
      return r.title ? `Sent ${r.title} to your download client.` : "Sent to your download client.";
    }
    if (r.message) return String(r.message);
    return status ? humanizeToken(status) + "." : "Done.";
  }

  async function loadStuck() {
    const token = ++loadToken;
    setSpinning(true);
    if (!els.stuckContent.dataset.loaded) {
      els.stuckContent.innerHTML = '<div class="m-loading">Loading&hellip;</div>';
    }
    try {
      const payload = await fetchJson(
        `/api/inkdrop-state/manual_review?summary=compact&rows=compact&limit=${MANUAL_REVIEW_LIMIT}`
      );
      const data = (payload && payload.view) || {};
      if (token !== loadToken) return;
      renderStuck(data);
      updateBadge(data);
      els.stuckContent.dataset.loaded = "1";
    } catch (err) {
      if (token !== loadToken) return;
      if (err.unauthenticated) { showLogin(); return; }
      if (!els.stuckContent.dataset.loaded) {
        els.stuckContent.innerHTML = '<p class="m-error">Couldn\'t load Manual Review. Pull down or tap refresh to try again.</p>';
      }
    } finally {
      if (token === loadToken) setSpinning(false);
    }
  }

  async function refreshStuckBadge() {
    try {
      const payload = await fetchJson(
        `/api/inkdrop-state/manual_review?summary=compact&rows=compact&limit=1`
      );
      updateBadge((payload && payload.view) || {});
    } catch (err) {
      // Badge is best-effort; a failed background check shouldn't surface an error.
    }
  }

  function updateBadge(data) {
    const count = Number((data && (data.total_count ?? data.count)) || 0);
    if (count > 0) {
      els.stuckBadge.hidden = false;
      els.stuckBadge.textContent = count > 99 ? "99+" : String(count);
    } else {
      els.stuckBadge.hidden = true;
    }
  }

  function renderStuck(data) {
    const rows = (data && Array.isArray(data.rows)) ? data.rows : [];
    stuckRows = rows;
    if (!rows.length) {
      els.stuckContent.innerHTML = '<div class="m-empty">Nothing needs your attention right now.</div>';
      return;
    }
    const cards = rows
      .map((row, index) => {
        const reason = reviewRowReason(row);
        const state = reviewRowState(row);
        const approveEndpoint = approveEndpointFor(row);
        const rejectable = canReject(row);
        const ignorable = Boolean(row.review_id);
        const approveBtn = `<button type="button" class="m-btn-primary" data-review-action="approve" data-review-index="${index}"${approveEndpoint ? "" : " disabled"}>${escapeHtml(approveLabelFor(row))}</button>`;
        const approveNote = approveEndpoint
          ? ""
          : `<p class="m-action-note">${escapeHtml(approveDisabledReason(row))}</p>`;
        const secondary = [
          rejectable
            ? `<button type="button" class="m-btn-danger" data-review-action="reject" data-review-index="${index}">Reject &amp; search again</button>`
            : "",
          ignorable
            ? `<button type="button" class="m-btn-quiet" data-review-action="ignore" data-review-index="${index}">Ignore</button>`
            : "",
        ].filter(Boolean).join("");
        const done = reviewNotes.get(row.review_id) || "";
        return `<div class="m-item-card" data-review-card="${index}">
          <div class="m-item-title">${escapeHtml(reviewRowTitle(row))}</div>
          ${reason ? `<div class="m-item-reason">${escapeHtml(humanizeToken(reason))}</div>` : ""}
          <div class="m-item-row">
            ${state ? `<span class="m-pill">${escapeHtml(humanizeToken(state))}</span>` : ""}
            ${row.current_source || row.source ? `<span class="m-item-source">${escapeHtml(humanizeToken(row.current_source || row.source))}</span>` : ""}
          </div>
          <div class="m-actions">${approveBtn}${secondary}</div>
          ${approveNote}
          <p class="m-action-status${done ? " m-tone-good" : ""}" data-review-status="${index}"${done ? "" : " hidden"}>${escapeHtml(done)}</p>
        </div>`;
      })
      .join("");
    const total = Number((data && (data.total_count ?? rows.length)) || rows.length);
    const more = total > rows.length
      ? `<div class="m-more-note">Showing ${rows.length} of ${total} &mdash; open InkDrop on desktop for the full list.</div>`
      : "";
    els.stuckContent.innerHTML = `<div class="m-item-list">${cards}</div>${more}`;
  }

  async function runReviewAction(action, index, button) {
    const row = stuckRows[index];
    if (!row || !row.review_id) return;
    const subject = reviewRowTitle(row);
    let endpoint = "";
    let confirmed = true;
    if (action === "approve") {
      endpoint = approveEndpointFor(row);
      if (!endpoint) return;
    } else if (action === "reject") {
      endpoint = "/api/manual-review/bad-match";
      confirmed = await confirmSheet({
        title: "Reject this candidate?",
        subject,
        copy: row.can_approve_local_file || (!row.can_approve && row.source)
          ? "The staged file moves to a rejected folder (it is not deleted) and a new search for this series starts in the background."
          : "This exact release is blocked from being offered again and a new search for this series starts in the background.",
        confirmLabel: "Reject & search again",
        tone: "bad",
      });
    } else if (action === "ignore") {
      endpoint = "/api/manual-review/ignore";
      confirmed = await confirmSheet({
        title: "Ignore this item?",
        subject,
        copy: "InkDrop stops surfacing this review row. No files are deleted and the history stays available.",
        confirmLabel: "Ignore item",
      });
    } else {
      return;
    }
    if (!confirmed) return;
    const statusEl = els.stuckContent.querySelector(`[data-review-status="${index}"]`);
    const card = els.stuckContent.querySelector(`[data-review-card="${index}"]`);
    const buttons = card ? Array.from(card.querySelectorAll("button")) : [];
    const original = button ? button.textContent : "";
    buttons.forEach((b) => { b.disabled = true; });
    if (button) button.textContent = "Working…";
    if (statusEl) {
      statusEl.hidden = false;
      statusEl.className = "m-action-status m-tone-warn";
      statusEl.textContent = "Sending your decision…";
    }
    try {
      const data = await postJson(endpoint, { review_id: row.review_id });
      const result = (data && data.result) || {};
      const message = reviewActionMessage(action, result);
      toast(message, true);
      // A rejected row deliberately stays in the list: mark_bad_match()
      // blocklists the release and kicks off a background re-search, but only
      // ignore_manual_review() removes a row from the view. Carrying the
      // outcome across the reload is what stops "I tapped Reject and nothing
      // happened" -- the row is still here on purpose, and now says why.
      reviewNotes.set(row.review_id, message);
      // Re-read the list from the server rather than patching it locally: one
      // decision can resolve sibling rows, and a hand-patched list would keep
      // showing rows the backend has already closed.
      delete els.stuckContent.dataset.loaded;
      await loadStuck();
    } catch (err) {
      if (err.unauthenticated) { showLogin(); return; }
      buttons.forEach((b) => { b.disabled = false; });
      if (button) button.textContent = original;
      if (statusEl) {
        statusEl.hidden = false;
        statusEl.className = "m-action-status m-tone-bad";
        statusEl.textContent = err.message || "That action failed.";
      }
      toast(err.message || "That action failed.", false);
    }
  }

  // --- Wire up --------------------------------------------------------

  function goToDesktop() {
    try { sessionStorage.setItem("inkdropViewPreference", "desktop"); } catch (e) {}
    // Deliberately not forwarding location.search: a lingering ?view=mobile
    // (from an earlier forced-mobile visit) would otherwise re-assert
    // itself the instant the desktop gate script reads the query string,
    // clobbering the preference this click just set.
    location.href = "/";
  }

  function init() {
    cacheEls();
    els.loginForm.addEventListener("submit", handleLogin);
    els.refreshBtn.addEventListener("click", () => loadCurrentScreen({ force: true }));
    els.desktopLink.addEventListener("click", goToDesktop);
    els.nav.querySelectorAll(".m-nav-btn").forEach((btn) => {
      btn.addEventListener("click", () => switchScreen(btn.dataset.screen));
    });
    els.addForm.addEventListener("submit", handleSearch);
    // Filtering is local to rows already in memory, so it can run on every
    // keystroke without a request; submit just dismisses the phone keyboard.
    els.seriesForm.addEventListener("submit", (event) => {
      event.preventDefault();
      if (els.seriesQuery) els.seriesQuery.blur();
    });
    els.seriesQuery.addEventListener("input", () => renderSeries());
    els.seriesResults.addEventListener("click", (event) => {
      const addBtn = event.target.closest("[data-series-add]");
      if (addBtn) {
        // Nothing tracked matches what they typed -- hand the same query to
        // the Add screen rather than making them retype it there.
        const query = els.seriesQuery ? els.seriesQuery.value : "";
        switchScreen("add");
        if (els.addQuery) {
          els.addQuery.value = query;
          handleSearch();
        }
        return;
      }
      const btn = event.target.closest("[data-series-action]");
      if (!btn || btn.disabled) return;
      runSeriesAction(btn.dataset.seriesAction, Number(btn.dataset.seriesIndex), btn);
    });
    // The result and review cards are re-rendered wholesale on every load, so
    // they are delegated rather than re-bound per render.
    els.addResults.addEventListener("click", (event) => {
      const btn = event.target.closest("[data-add-index]");
      if (!btn) return;
      addSeriesAt(Number(btn.dataset.addIndex), btn);
    });
    els.stuckContent.addEventListener("click", (event) => {
      const btn = event.target.closest("[data-review-action]");
      if (!btn || btn.disabled) return;
      runReviewAction(btn.dataset.reviewAction, Number(btn.dataset.reviewIndex), btn);
    });
    els.homeContent.addEventListener("click", (event) => {
      const card = event.target.closest("[data-home-series]");
      if (card) {
        // Hand the title to the Series screen's own filter rather than
        // building a second detail view here: that screen already carries
        // "Search now" and the Monitor toggle for the row this lands on.
        if (els.seriesQuery) els.seriesQuery.value = card.dataset.homeSeries;
        switchScreen("series");
        return;
      }
      const btn = event.target.closest("[data-mobile-goto]");
      if (!btn) return;
      const target = btn.dataset.mobileGoto;
      switchScreen(target);
      // Only the Add screen wants the keyboard up -- Series opens on a list
      // worth reading, and focusing its filter box hid that behind a keyboard.
      if (target === "add" && els.addQuery) els.addQuery.focus();
    });
    // Covers are provider-hosted (ComicVine, MangaDex), so a phone on a weak
    // connection will fail some of them. Drop the broken image and let the
    // initial-letter placeholder underneath show instead of a torn-image icon.
    // "error" does not bubble, hence the capture-phase listener.
    els.homeContent.addEventListener("error", (event) => {
      const img = event.target;
      if (img && img.classList && img.classList.contains("m-rail-img")) img.remove();
    }, true);
    els.sheetConfirm.addEventListener("click", () => closeSheet(true));
    els.sheetCancel.addEventListener("click", () => closeSheet(false));
    els.sheet.addEventListener("click", (event) => {
      if (event.target === els.sheet) closeSheet(false);
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && sheetResolve) closeSheet(false);
    });
    boot();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
