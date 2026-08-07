(() => {
  "use strict";

  const API = "/api/missing-recovery";
  // This status call aggregates the whole monitored missing backlog and can
  // legitimately take a long time on a large state database -- without a
  // bound here, a slow/cold read left the panel stuck on "Loading recovery
  // selection and progress..." forever with no feedback and no way to retry.
  const LOAD_TIMEOUT_MS = 20000;
  const state = {
    payload: null,
    loading: null,
    lastLoadedAt: 0,
    refreshTimer: 0,
    actionError: null,
    wantedActive: false,
    activationGeneration: 0,
    requestGeneration: 0,
    mutationGeneration: 0,
  };
  const progressLabels = Object.freeze({
    waiting_to_search: "Waiting to search",
    searching: "Searching",
    results_found: "Results found",
    downloading: "Downloading",
    checking_download: "Checking download",
    ready_to_import: "Ready to import",
    importing: "Importing",
    waiting_for_library_scan: "Waiting for library scan",
    complete: "Complete",
    needs_attention: "Needs attention",
  });
  // Each stage maps to the row status label(s) InkDrop already shows on the
  // Wanted table itself (coreStateLabel in the main app script) -- clicking a
  // tile reuses that same vocabulary rather than inventing a parallel one.
  const progressStageStatusTargets = Object.freeze({
    waiting_to_search: ["Waiting"],
    searching: ["Searching"],
    results_found: ["Searching"],
    downloading: ["Downloading"],
    checking_download: ["Downloading"],
    ready_to_import: ["Importing"],
    importing: ["Importing"],
    waiting_for_library_scan: ["Waiting"],
    complete: ["Complete"],
    needs_attention: ["Needs Review", "Failed", "Blocked"],
  });

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function surface() {
    return document.getElementById("inkdropMissingRecovery");
  }

  function metric(label, value, detail, tone="", onClick=null) {
    const card = element(onClick ? "button" : "div", `missing-recovery-metric ${tone}`.trim());
    if (onClick) {
      card.type = "button";
      card.addEventListener("click", onClick);
    }
    card.append(element("span", "missing-recovery-metric-label", label));
    card.append(element("strong", "missing-recovery-metric-value", value));
    if (detail) card.append(element("span", "missing-recovery-metric-detail", detail));
    return card;
  }

  function openWantedStage(stageKey, label) {
    const nav = window.InkDropWantedNav;
    if (!nav || typeof nav.openStage !== "function") return;
    nav.openStage(stageKey, label);
  }

  function renderMessage(host, message, tone="", onRetry=null) {
    if (!state.wantedActive) return;
    const wrap = element("div", `missing-recovery-message ${tone}`.trim(), message);
    if (onRetry) {
      const retry = element("button", "missing-recovery-retry", "Retry");
      retry.type = "button";
      retry.addEventListener("click", onRetry);
      wrap.appendChild(document.createTextNode(" "));
      wrap.appendChild(retry);
    }
    host.replaceChildren(wrap);
    host.hidden = false;
  }

  function activationIsCurrent(generation) {
    return state.wantedActive && state.activationGeneration === generation;
  }

  function stateLabel(payload) {
    if (state.actionError) return {label: state.actionError.action === "start" ? "Pass did not start" : "Pause did not apply", tone: "bad"};
    if (!payload.enabled) return {label: "Not configured", tone: "warn"};
    if (payload.control?.valid === false) return {label: "Control needs attention", tone: "warn"};
    if (payload.control?.paused) return {label: "Paused", tone: "warn"};
    if (payload.control?.one_shot_requested) return {label: "Pass requested", tone: "active"};
    // A one-shot request that never got a turn (the ordinary Autopilot lock
    // stayed busy the whole retry window, or the pass itself errored) used
    // to clear back to this exact same "Ready to run a pass" state with no
    // visible difference from a pass that actually completed. Surface it.
    if (payload.control?.last_outcome === "busy_exhausted") return {label: "Pass didn't run — Autopilot stayed busy", tone: "bad"};
    if (payload.control?.last_outcome === "failed") return {label: "Pass failed to start", tone: "bad"};
    return {label: "Ready to run a pass", tone: "good"};
  }

  function scheduleRefresh(payload) {
    window.clearTimeout(state.refreshTimer);
    if (payload?.control?.one_shot_requested) {
      state.refreshTimer = window.setTimeout(() => load(true), 5000);
      return;
    }
    // The status endpoint never blocks on its expensive aggregate anymore:
    // it hands back "computing" (nothing cached) or a stale snapshot while a
    // background recompute runs. Repoll until a fresh snapshot lands, so the
    // panel converges on real counts without the user touching anything.
    if (payload?.computing || payload?.stale) {
      state.refreshTimer = window.setTimeout(() => load(true), 6000);
    }
  }

  function renderPayload(payload) {
    const host = surface();
    if (!host || !state.wantedActive) return;
    host.replaceChildren();
    host.hidden = false;
    host.dataset.recoveryEnabled = payload.enabled ? "true" : "false";
    host.dataset.recoveryPaused = payload.control?.paused ? "true" : "false";

    const heading = element("div", "missing-recovery-heading");
    const headingMain = element("div", "missing-recovery-heading-main");
    const icon = element("span", "missing-recovery-icon");
    icon.setAttribute("aria-hidden", "true");
    headingMain.append(icon);
    const copy = element("div", "missing-recovery-copy");
    copy.append(element("h3", "", "Recover Missing"));
    copy.append(element("p", "mini", "Make one limited pass over old misses, stalled downloads, and unfinished imports."));
    headingMain.append(copy);
    const mode = stateLabel(payload);
    heading.append(headingMain, element("span", `missing-recovery-state ${mode.tone}`.trim(), mode.label));
    host.append(heading);

    // A snapshot served from cache while a slow recompute is still in
    // flight (missing_recovery_dashboard_status()'s bounded-lock fallback)
    // used to render identically to a fresh one -- same numbers, same
    // tone, no way to tell the counts might be seconds to minutes old.
    if (payload.stale) {
      const staleNotice = element("div", "missing-recovery-notice warn");
      staleNotice.append(element("strong", "", "Showing cached data."));
      staleNotice.append(element("span", "", "A refresh is already in progress; these counts may be a little behind and update on their own in a few seconds."));
      host.append(staleNotice);
    }

    if (!state.actionError && !payload.control?.one_shot_requested && payload.control?.last_outcome
        && (payload.control.last_outcome === "busy_exhausted" || payload.control.last_outcome === "failed")) {
      const outcomeNotice = element("div", "missing-recovery-notice bad");
      outcomeNotice.append(element("strong", "", mode.label));
      outcomeNotice.append(element("span", "", payload.control.last_outcome_note || "The requested pass did not run. Try again."));
      host.append(outcomeNotice);
    }

    if (!payload.enabled) {
      const notice = element("div", "missing-recovery-notice warn");
      notice.append(element("strong", "", "Recovery mode is off by default."));
      notice.append(element("span", "", payload.config_action || "Enable missing recovery and restart the web and worker services."));
      host.append(notice);
    } else if (payload.control?.valid === false) {
      const notice = element("div", "missing-recovery-notice warn");
      notice.append(element("strong", "", "Recovery is paused fail-closed."));
      notice.append(element("span", "", "Repair or remove the invalid missing-recovery control file, then refresh."));
      host.append(notice);
    } else if (!payload.status_available) {
      host.append(element("div", "missing-recovery-notice warn", payload.error || "Recovery counts are currently unknown. Refresh after state storage is available."));
    }

    if (state.actionError) {
      const failure = element("div", "missing-recovery-notice bad");
      failure.append(element("strong", "", state.actionError.action === "start"
        ? "InkDrop did not start the pass. Nothing was selected and nothing is running."
        : "InkDrop did not pause new recovery. Admission is still open."));
      failure.append(element("span", "", state.actionError.message));
      failure.append(element("span", "mini", "Try again. If it keeps failing, check the InkDrop web log for the reason."));
      host.append(failure);
    }

    const details = element("details", "missing-recovery-details");
    details.append(element("summary", "", "Recovery details"));
    details.append(element("p", "mini", "Stays inside today's data cap, staging space, and quiet hours. For an immediate sweep of every Wanted issue instead, use Search All in the table below."));

    const progress = element("div", "missing-recovery-progress");
    progress.append(element("strong", "missing-recovery-subtitle", "Where the missing issues are now"));
    const progressGrid = element("div", "missing-recovery-progress-grid");
    for (const [key, label] of Object.entries(progressLabels)) {
      const value = payload.status_available ? Number(payload.progress?.[key] || 0) : "—";
      const tone = key === "needs_attention" && Number(value) ? "bad" : key === "complete" && Number(value) ? "good" : "";
      const clickable = payload.status_available && Number(value) > 0;
      progressGrid.append(metric(
        label,
        value,
        clickable ? "View in Wanted" : "",
        tone,
        clickable ? () => openWantedStage(key, label) : null,
      ));
    }
    progress.append(progressGrid);
    details.append(progress);

    const advanced = element("details", "missing-recovery-advanced");
    advanced.append(element("summary", "", "Advanced: next-pass planning and admission limits"));
    const selection = element("div", "missing-recovery-metrics");
    selection.append(
      metric("Next-pass selection", payload.status_available ? Number(payload.selected_count || 0) : "Unknown", payload.status_available ? `${payload.selection_estimated ? "Estimate · " : ""}${Number(payload.eligible_count || 0)} look eligible right now. InkDrop picks the final list when the pass actually starts` : "Selection data unavailable"),
      metric("Estimated provider calls", payload.estimated_provider_calls?.label || "Unknown", payload.estimated_provider_calls?.detail || "Calculated after selection"),
      metric("New handoff limit", payload.active_download_limit?.label || "Unknown", payload.active_download_limit?.detail || "No reliable limit reported"),
      metric("Data cap", payload.data_cap?.label || "Unknown", payload.data_cap?.detail || "No reliable cap reported"),
      metric("Staging reserve", payload.staging_space?.label || "Unknown", payload.staging_space?.detail || "Available space unknown"),
      metric("Failure pause", payload.failure_pause?.label || "Unknown", payload.failure_pause?.detail || "Failure policy unknown"),
      metric("Quiet hours", payload.quiet_hours?.label || "Unknown", payload.quiet_hours?.detail || "Quiet-hours policy unknown"),
    );
    advanced.append(selection);

    const categories = element("div", "missing-recovery-categories");
    categories.append(element("strong", "", "Selection mix"));
    const categoryList = element("div", "missing-recovery-category-list");
    const categoryRows = Array.isArray(payload.selection_categories) ? payload.selection_categories : [];
    if (categoryRows.length) {
      for (const row of categoryRows) {
        const chip = element("span", "missing-recovery-category");
        chip.append(element("span", "", row.label || "Recovery work"), element("strong", "", Number(row.count || 0)));
        categoryList.append(chip);
      }
    } else {
      categoryList.append(element("span", "mini", payload.status_available ? "Nothing is picked for the next pass yet." : "Selection categories are unknown."));
    }
    categories.append(categoryList);
    advanced.append(categories);
    details.append(advanced);
    host.append(details);

    const footer = element("div", "missing-recovery-footer");
    const result = element("div", "missing-recovery-result");
    result.append(
      element("strong", "", `${payload.status_available ? Number(payload.completed_imports || 0) : "Unknown"} completed imports`),
      element("span", "", `${payload.status_available ? Number(payload.progress?.needs_attention || 0) : "Unknown"} need attention`),
    );
    const live = element("span", "missing-recovery-live");
    live.setAttribute("role", "status");
    live.setAttribute("aria-live", "polite");
    const actions = element("div", "missing-recovery-actions");
    const start = element("button", "primary", payload.control?.one_shot_requested ? "Pass requested" : "Start one pass");
    start.type = "button";
    start.disabled = !payload.can_manage || !payload.enabled || payload.control?.valid === false || payload.control?.one_shot_requested;
    start.title = !payload.can_manage ? "You need to be signed in as an administrator." : !payload.enabled ? payload.config_action : "Run one recovery pass.";
    const pause = element("button", "", "Pause new recovery");
    pause.type = "button";
    pause.disabled = !payload.can_manage || payload.control?.valid === false || payload.control?.paused;
    pause.title = !payload.can_manage ? "You need to be signed in as an administrator." : "Prevents new recovery admission without cancelling active transfers or imports.";
    start.addEventListener("click", () => mutate("start", live, [start, pause]));
    pause.addEventListener("click", () => mutate("pause", live, [start, pause]));
    actions.append(start, pause);
    if (state.actionError) {
      live.classList.add("bad");
      live.textContent = state.actionError.message;
    }
    else if (!payload.can_manage) live.textContent = "Sign in as an administrator to start or pause recovery.";
    else if (payload.control?.paused) live.textContent = "New recovery admission is paused. Active transfers and imports continue.";
    else if (payload.control?.requested_at) live.textContent = `Last requested ${payload.control.requested_at}.`;
    footer.append(result, live, actions);
    host.append(footer);
    scheduleRefresh(payload);
  }

  async function mutate(action, live, buttons) {
    const activationGeneration = state.activationGeneration;
    const mutationGeneration = ++state.mutationGeneration;
    for (const button of buttons) button.disabled = true;
    state.actionError = null;
    live.classList.remove("bad");
    live.textContent = action === "start" ? "Starting one recovery pass…" : "Pausing new recovery work…";
    try {
      await window.InkDropApi.request("/api/series-autopilot/run", {method: "POST", body: {missingRecoveryAction: action}});
      state.payload = null;
      state.lastLoadedAt = 0;
      if (!activationIsCurrent(activationGeneration) || mutationGeneration !== state.mutationGeneration) {
        if (state.wantedActive) load(true);
        return;
      }
      live.textContent = action === "start"
        ? "Bounded recovery pass requested."
        : "New recovery admission paused. Active transfers and imports continue.";
      await load(true);
    } catch (error) {
      if (!activationIsCurrent(activationGeneration) || mutationGeneration !== state.mutationGeneration) return;
      state.actionError = {
        action,
        message: error?.detail || error?.message || "InkDrop could not reach the recovery control.",
      };
      live.classList.add("bad");
      live.textContent = state.actionError.message;
      for (const button of buttons) button.disabled = false;
      if (state.payload) renderPayload(state.payload);
    }
  }

  async function load(force=false) {
    const host = surface();
    if (!host || host.hidden) return;
    if (!force && state.payload && Date.now() - state.lastLoadedAt < 30000) {
      renderPayload(state.payload);
      return;
    }
    const activationGeneration = state.activationGeneration;
    if (!force && state.loading?.activationGeneration === activationGeneration) return state.loading.promise;
    const requestGeneration = ++state.requestGeneration;
    const controller = typeof AbortController !== "undefined" ? new AbortController() : null;
    const timeoutId = controller ? window.setTimeout(() => controller.abort(), LOAD_TIMEOUT_MS) : 0;
    const promise = window.InkDropApi.request(API, controller ? {signal: controller.signal} : {})
      .then(payload => {
        if (!activationIsCurrent(activationGeneration) || requestGeneration !== state.requestGeneration) return payload;
        state.payload = payload;
        state.lastLoadedAt = Date.now();
        renderPayload(payload);
        return payload;
      })
      .catch(error => {
        if (!activationIsCurrent(activationGeneration) || requestGeneration !== state.requestGeneration) return;
        const timedOut = error?.name === "AbortError";
        const message = timedOut
          ? "Recovery status is taking longer than expected."
          : (error?.message || "Recovery status is unavailable.");
        renderMessage(host, message, "bad", () => load(true));
      })
      .finally(() => {
        window.clearTimeout(timeoutId);
        if (state.loading?.requestGeneration === requestGeneration) state.loading = null;
      });
    state.loading = {activationGeneration, requestGeneration, promise};
    return promise;
  }

  function deactivate(targetView="") {
    if (String(targetView || "").trim().toLowerCase() === "wanted") return false;
    const host = surface();
    if (state.wantedActive) state.activationGeneration += 1;
    state.wantedActive = false;
    window.clearTimeout(state.refreshTimer);
    state.actionError = null;
    if (host) {
      host.hidden = true;
      host.replaceChildren();
    }
    return true;
  }

  function render(viewPayload={}) {
    const host = surface();
    if (!host) return;
    if (String(viewPayload?.view || "") !== "wanted") {
      deactivate(viewPayload?.view);
      return;
    }
    if (!state.wantedActive) {
      state.activationGeneration += 1;
      state.payload = null;
      state.lastLoadedAt = 0;
    }
    state.wantedActive = true;
    host.hidden = false;
    if (!state.payload) renderMessage(host, "Loading recovery selection and progress…");
    load(false);
  }

  window.InkDropMissingRecovery = Object.freeze({deactivate, render, refresh: () => load(true)});
})();
