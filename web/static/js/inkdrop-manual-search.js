(function () {
  "use strict";

  const ROOT_ID = "inkdropManualSearch";
  const ROUTE_KEY = "manual-search";
  const TERMINAL_STATES = new Set(["complete", "completed", "partial", "failed", "cancelled", "expired", "timed_out", "rate_limited", "unavailable"]);
  const state = {
    context: null,
    run: null,
    results: [],
    attempts: [],
    queries: [],
    includeRejected: true,
    filter: "all",
    provider: "",
    protocol: "",
    language: "",
    format: "",
    pack: "all",
    assisted: "all",
    minimumScore: "",
    sort: "decision",
    direction: "desc",
    pollTimer: 0,
    busy: false,
    feedback: null,
    grabbed: new Set(),
    pendingGrabs: new Set(),
    pushedRoute: false,
    returnFocus: null,
  };

  function api() {
    if (!window.InkDropApi || typeof window.InkDropApi.request !== "function") {
      throw Object.assign(new Error("InkDrop API client is unavailable."), { code: "api_client_unavailable" });
    }
    return window.InkDropApi;
  }

  function text(value, fallback = "") {
    return value === null || value === undefined ? fallback : String(value);
  }

  function escapeHtml(value) {
    return text(value).replace(/[&<>'"]/g, (char) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
    }[char]));
  }

  function humanize(value) {
    return text(value).replace(/[_-]+/g, " ").replace(/\b\w/g, (char) => char.toUpperCase());
  }

  function formatBytes(value) {
    const bytes = Number(value);
    if (!Number.isFinite(bytes) || bytes < 0) return "Unavailable";
    if (bytes === 0) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
    return `${(bytes / (1024 ** index)).toFixed(index > 1 ? 1 : 0)} ${units[index]}`;
  }

  function formatAge(seconds) {
    const amount = Number(seconds);
    if (!Number.isFinite(amount) || amount < 0) return "Unavailable";
    if (amount < 3600) return `${Math.max(1, Math.round(amount / 60))}m`;
    if (amount < 86400) return `${Math.round(amount / 3600)}h`;
    return `${Math.round(amount / 86400)}d`;
  }

  function root() {
    return document.getElementById(ROOT_ID);
  }

  function stopPolling() {
    if (state.pollTimer) window.clearTimeout(state.pollTimer);
    state.pollTimer = 0;
  }

  function subjectLabel() {
    const context = state.context || {};
    return context.unit_label || context.issue_label || context.title || "Missing item";
  }

  function statusLabel(raw) {
    const labels = {
      queued: "Planned", planned: "Planned", running: "Searching", searching: "Searching",
      complete: "Complete", completed: "Complete", zero_results: "Zero results",
      partial: "Complete with provider issues", expired: "Expired",
      timed_out: "Timed out", rate_limited: "Rate limited", unavailable: "Unavailable",
      cancelled: "Cancelled", failed: "Search failed",
    };
    return labels[text(raw).toLowerCase()] || humanize(raw || "Not started");
  }

  function attemptLabel(attempt) {
    const provider = attempt.provider_display_name || attempt.provider_name || attempt.provider_id || "Provider";
    const child = attempt.child_source_name || attempt.indexer_or_extension || attempt.child_source_id || "";
    return child && child.toLowerCase() !== text(provider).toLowerCase() ? `${provider} · ${child}` : provider;
  }

  function attemptState(attempt) {
    const label = statusLabel(attempt.state || attempt.status || "planned");
    const duration = Number(attempt.duration_ms);
    const count = Number(attempt.result_count);
    const timing = Number.isFinite(duration) && duration >= 0 ? ` · ${duration} ms` : "";
    const results = Number.isFinite(count) && count >= 0 ? ` · ${count} result${count === 1 ? "" : "s"}` : "";
    return `${label}${timing}${results}`;
  }

  function attemptGuidance(attempt) {
    const rows = attempt?.diagnostics?.provider_rows;
    const codes = Array.isArray(rows)
      ? rows.map((row) => text(row?.failure_code || row?.reason_code).toLowerCase()).filter(Boolean)
      : [];
    const providerKey = text(attempt?.provider_id).toLowerCase();
    const isProwlarr = providerKey === "prowlarr" || providerKey.startsWith("prowlarr_") || providerKey.startsWith("prowlarr-");
    const genericMessages = {
      provider_credentials_unavailable: "Provider credentials unavailable. Check this provider's configuration in Settings.",
      provider_http_error: "Provider returned an HTTP error. Check its health in Settings > Download Sources.",
      provider_connection_failed: "InkDrop could not reach this provider. Check its configured address and container network.",
      provider_timeout: "Provider did not finish before the deadline. Check its health or adjust the timeout.",
      malformed_provider_response: "Provider returned an unreadable response. Check its logs and version.",
      provider_response_too_large: "The provider sent back more data than InkDrop will accept. Narrow the search, or check the provider.",
      provider_request_blocked: "InkDrop blocked this provider request because its configured destination is not allowed.",
      provider_configuration_required: "Provider needs configuration in Settings.",
      provider_request_failed: "A provider request failed. Check its health and configuration.",
      unsupported_adapter: "This provider is not supported in this version of InkDrop.",
      not_executable: "This provider cannot run in this deployment.",
    };
    const prowlarrMessages = {
      ...genericMessages,
      provider_credentials_unavailable: "Prowlarr credentials unavailable. Check its API key or config mount in Settings > Indexers.",
      provider_http_error: "Prowlarr returned an HTTP error. Check the affected indexer in Prowlarr.",
      provider_connection_failed: "InkDrop could not reach Prowlarr. Check its address and container network in Settings > Indexers.",
      provider_timeout: "Prowlarr did not finish before the provider deadline. Check indexer health or adjust the timeout.",
      malformed_provider_response: "Prowlarr returned an unreadable response. Check its logs and version.",
      provider_response_too_large: "Prowlarr sent back more data than InkDrop will accept. Narrow the search, or check the indexer.",
      provider_request_blocked: "InkDrop blocked the Prowlarr request because its configured destination is not allowed.",
      provider_configuration_required: "Prowlarr needs configuration in Settings > Indexers.",
      provider_request_failed: "A Prowlarr request failed. Check the provider-row details and Prowlarr indexer health.",
    };
    const messages = isProwlarr ? prowlarrMessages : genericMessages;
    const code = codes.find((value) => messages[value]) || text(attempt?.error_code).toLowerCase();
    if (messages[code]) return messages[code];
    const stateName = text(attempt?.state || attempt?.status).toLowerCase();
    if (["provider_failure", "provider_timeout", "timed_out"].includes(stateName)) {
      return isProwlarr
        ? "The Prowlarr search failed, which is not the same as finding nothing. Check Settings > Indexers."
        : "The search failed, which is not the same as finding nothing. Check this provider's settings.";
    }
    return "";
  }

  function decision(candidate) {
    const core = candidate.decision && typeof candidate.decision === "object" ? candidate.decision : {};
    const raw = text(core.decision || candidate.decision || "").toLowerCase();
    if (candidate.already_downloading || raw === "already_downloading") return { label: "Already Downloading", tone: "info", rank: 0 };
    if (candidate.already_imported || raw === "already_imported") return { label: "Already Imported", tone: "good", rank: 0 };
    if (candidate.acquisition_capability === "assisted" || candidate.assisted_only || raw === "assisted") return { label: "Assisted", tone: "warn", rank: 3 };
    if (raw === "source_unavailable") return { label: "Source Unavailable", tone: "bad", rank: 4 };
    if (candidate.accepted && candidate.pack_candidate) return { label: "Safe Pack", tone: "good", rank: 1 };
    if (candidate.accepted) return { label: "Safe Match", tone: "good", rank: 0 };
    if (["possible", "possible_match", "review"].includes(raw)) return { label: "Possible Match", tone: "warn", rank: 2 };
    return { label: "Rejected by policy", tone: "bad", rank: 4 };
  }

  function evidence(candidate) {
    const core = candidate.decision && typeof candidate.decision === "object" ? candidate.decision : {};
    const positive = core.positive_evidence || candidate.positive_evidence || [];
    const negative = core.negative_evidence || candidate.rejection_explanations || candidate.rejection_codes || [];
    return { positive: Array.from(positive), negative: Array.from(negative), explanation: core.explanation || candidate.explanation || "" };
  }

  function sourceLabel(candidate) {
    const provider = candidate.provider_display_name || candidate.provider_result_label || candidate.provider_id || "Unknown provider";
    const child = candidate.child_source_name || candidate.indexer_or_extension || "";
    return child && text(child).toLowerCase() !== text(provider).toLowerCase() ? `${provider} · ${child}` : provider;
  }

  function interpretedMatch(candidate) {
    return [candidate.interpreted_work, candidate.interpreted_edition, candidate.interpreted_unit_number ? `#${candidate.interpreted_unit_number}` : ""]
      .filter(Boolean).join(" · ") || "Not interpreted";
  }

  function candidateMatches(candidate) {
    const candidateDecision = decision(candidate);
    if (state.filter === "accepted" && !candidate.accepted) return false;
    if (state.filter === "rejected" && candidate.accepted) return false;
    if (state.provider && text(candidate.provider_id) !== state.provider) return false;
    if (state.protocol && text(candidate.protocol) !== state.protocol) return false;
    if (state.language && text(candidate.language) !== state.language) return false;
    if (state.format && text(candidate.format || candidate.file_archive_format) !== state.format) return false;
    if (state.pack === "pack" && !candidate.pack_candidate) return false;
    if (state.pack === "single" && candidate.pack_candidate) return false;
    if (state.assisted === "yes" && candidateDecision.label !== "Assisted") return false;
    if (state.assisted === "no" && candidateDecision.label === "Assisted") return false;
    if (state.minimumScore !== "" && Number(candidate.match_score || 0) < Number(state.minimumScore)) return false;
    return true;
  }

  function sortedResults() {
    const direction = state.direction === "asc" ? 1 : -1;
    const values = state.results.filter(candidateMatches);
    const get = (candidate) => {
      if (state.sort === "decision") return decision(candidate).rank;
      if (state.sort === "score") return Number(candidate.match_score || 0);
      if (state.sort === "size") return Number(candidate.size_bytes || 0);
      if (state.sort === "age") return Number(candidate.age_seconds || 0);
      if (state.sort === "seeders") return Number(candidate.seeders || 0);
      if (state.sort === "provider") return sourceLabel(candidate).toLowerCase();
      return text(candidate.original_title).toLowerCase();
    };
    return values.sort((a, b) => {
      const left = get(a);
      const right = get(b);
      if (state.sort === "decision") return Number(left) - Number(right);
      return left < right ? -direction : left > right ? direction : 0;
    });
  }

  function optionValues(key) {
    return Array.from(new Set(state.results.map((item) => text(item[key])).filter(Boolean))).sort();
  }

  function selectOptions(values, selected, emptyLabel) {
    return [`<option value="">${escapeHtml(emptyLabel)}</option>`, ...values.map((value) => `<option value="${escapeHtml(value)}"${value === selected ? " selected" : ""}>${escapeHtml(humanize(value))}</option>`)].join("");
  }

  function providerProgressHtml() {
    if (!state.run && !state.attempts.length) return '<p class="manual-search-muted">Search has not started.</p>';
    if (!state.attempts.length) return `<p class="manual-search-muted">${escapeHtml(statusLabel(state.run?.state))}</p>`;
    return state.attempts.map((attempt) => {
      const guidance = attemptGuidance(attempt);
      return `
      <label class="manual-search-provider">
        <input type="checkbox" data-provider-select value="${escapeHtml(attempt.provider_id || "")}" checked>
        <span><strong>${escapeHtml(attemptLabel(attempt))}</strong><small>${escapeHtml(attemptState(attempt))}</small>${guidance ? `<small class="manual-search-provider-guidance">${escapeHtml(guidance)}</small>` : ""}</span>
      </label>`;
    }).join("");
  }

  function resultDetails(candidate) {
    const proof = evidence(candidate);
    const facts = [
      ["Original title", candidate.original_title],
      ["Query", candidate.query_evidence?.query || candidate.query || "Not supplied"],
      ["Work", candidate.interpreted_work],
      ["Edition", candidate.interpreted_edition],
      ["Unit", candidate.interpreted_unit_number],
      ["Publisher / year", [candidate.interpreted_publisher || candidate.publisher, candidate.interpreted_year].filter(Boolean).join(" · ")],
      ["Source health", candidate.source_health_snapshot?.status || candidate.health_snapshot?.status],
      ["Acquisition", candidate.acquisition_capability],
    ].filter(([, value]) => value !== undefined && value !== null && value !== "");
    const factHtml = facts.map(([label, value]) => `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`).join("");
    const positive = proof.positive.length ? `<section><h4>Accepted evidence</h4><ul>${proof.positive.map((item) => `<li>${escapeHtml(humanize(item))}</li>`).join("")}</ul></section>` : "";
    const negative = proof.negative.length ? `<section><h4>Rejection / warning evidence</h4><ul>${proof.negative.map((item) => `<li>${escapeHtml(humanize(item))}</li>`).join("")}</ul></section>` : "";
    return `<div class="manual-search-evidence"><dl>${factHtml}</dl>${positive}${negative}${proof.explanation ? `<p>${escapeHtml(proof.explanation)}</p>` : ""}</div>`;
  }

  function grabAllowed(candidate) {
    return Boolean(candidate.accepted && candidate.acquisition_capability !== "assisted" && !candidate.assisted_only && !candidate.already_downloading && !candidate.already_imported);
  }

  function forceGrabAllowed(candidate) {
    return Boolean(!candidate.accepted && candidate.force_grab?.eligible && !candidate.already_downloading && !candidate.already_imported);
  }

  function resultRow(candidate) {
    const itemDecision = decision(candidate);
    const candidateId = text(candidate.candidate_id);
    const assisted = itemDecision.label === "Assisted";
    const availability = candidate.seeders !== undefined && candidate.seeders !== null
      ? `${candidate.seeders} seeds`
      : candidate.remote_availability || candidate.remote_queue_state || "Unavailable";
    const pack = candidate.pack_candidate ? `${humanize(candidate.pack_type || "Pack")}${candidate.likely_wanted_coverage ? ` · ${candidate.likely_wanted_coverage} wanted` : ""}` : "Single";
    let action = "";
    if (state.grabbed.has(candidateId)) action = '<span class="manual-search-chip good">Sent to queue</span>';
    else if (grabAllowed(candidate)) action = `<button type="button" class="manual-search-button primary" data-grab="${escapeHtml(candidateId)}">Grab</button>`;
    else if (forceGrabAllowed(candidate)) action = `<button type="button" class="manual-search-button danger" data-force-grab="${escapeHtml(candidateId)}">Force grab…</button>`;
    else if (assisted) action = '<span class="manual-search-assisted">Manual retrieval required</span>';
    else action = `<button type="button" class="manual-search-button" disabled title="${escapeHtml(candidate.force_grab?.reason || "Core rejected this candidate. Open Details to review the policy evidence.")}">Grab blocked by policy</button>`;
    return `
      <article class="manual-search-result" data-candidate-id="${escapeHtml(candidateId)}">
        <div class="manual-search-result-grid" role="group" aria-label="Search result">
          <div role="group" aria-label="Decision" data-label="Decision"><span class="manual-search-chip ${itemDecision.tone}">${escapeHtml(itemDecision.label)}</span></div>
          <div class="manual-search-release" role="group" aria-label="Release" data-label="Release"><strong title="${escapeHtml(candidate.original_title || "Untitled result")}">${escapeHtml(candidate.original_title || "Untitled result")}</strong><small>${escapeHtml(interpretedMatch(candidate))}</small></div>
          <div role="group" aria-label="Source" data-label="Source">${escapeHtml(sourceLabel(candidate))}<small>${escapeHtml(humanize(candidate.protocol || "Unknown protocol"))}</small></div>
          <div role="group" aria-label="Language and format" data-label="Language / format">${escapeHtml(candidate.language || "Unknown")}<small>${escapeHtml(candidate.format || candidate.file_archive_format || "Unknown")}</small></div>
          <div role="group" aria-label="Size and age" data-label="Size / age">${escapeHtml(formatBytes(candidate.size_bytes))}<small>${escapeHtml(formatAge(candidate.age_seconds))}</small></div>
          <div role="group" aria-label="Availability" data-label="Availability">${escapeHtml(availability)}<small>${escapeHtml(pack)}</small></div>
          <div role="group" aria-label="Score" data-label="Score">${candidate.match_score === undefined || candidate.match_score === null ? "—" : escapeHtml(candidate.match_score)}</div>
          <div class="manual-search-result-actions" role="group" aria-label="Actions" data-label="Actions">${action}<button type="button" class="manual-search-icon-button" data-toggle-details aria-expanded="false" aria-label="Show result evidence">Details</button></div>
        </div>
        <div class="manual-search-result-details" hidden>${resultDetails(candidate)}</div>
      </article>`;
  }

  function render() {
    const element = root();
    if (!element) return;
    const runState = state.run?.state || "not_started";
    const isRunning = !TERMINAL_STATES.has(text(runState).toLowerCase()) && runState !== "not_started";
    const results = sortedResults();
    const allRejected = state.results.length > 0 && !state.results.some((item) => item.accepted);
    let resultBody = "";
    if (!state.run) resultBody = '<div class="manual-search-state"><h3>Search not started</h3><p>Select Search to query the configured providers for this unit.</p></div>';
    else if (isRunning && !state.results.length) resultBody = '<div class="manual-search-state loading"><h3>Searching providers</h3><p>Results will appear as each bounded provider attempt completes.</p></div>';
    else if (!state.results.length) resultBody = `<div class="manual-search-state"><h3>${runState === "cancelled" ? "Search cancelled" : "No results returned"}</h3><p>${runState === "cancelled" ? "Start another search when ready." : "Configured providers returned no candidates for this unit."}</p></div>`;
    else if (!results.length) resultBody = '<div class="manual-search-state"><h3>No results match these filters</h3><p>Clear filters or turn on Show rejected results.</p></div>';
    else resultBody = `${allRejected ? '<div class="manual-search-inline-notice">Results were returned, but Core rejected every candidate. Expand a row to review the evidence.</div>' : ""}${results.map(resultRow).join("")}`;

    element.innerHTML = `
      <div class="manual-search-backdrop" data-close-manual-search></div>
      <section class="manual-search-panel" role="dialog" aria-modal="true" aria-labelledby="manualSearchTitle">
        <header class="manual-search-header">
          <div><span class="manual-search-eyebrow">Manual Search</span><h2 id="manualSearchTitle">${escapeHtml(subjectLabel())}</h2><p>${escapeHtml(state.context?.series_title || state.context?.series || "")}</p></div>
          <button type="button" class="manual-search-icon-button" data-close-manual-search aria-label="Close Manual Search">Close</button>
        </header>
        <div class="manual-search-toolbar">
          <button type="button" class="manual-search-button primary" data-start-search ${state.busy || isRunning ? "disabled" : ""}>${state.run ? "Search Again" : "Search"}</button>
          <button type="button" class="manual-search-button" data-search-selected ${state.busy || isRunning || !state.attempts.length ? "disabled" : ""}>Search selected providers</button>
          <button type="button" class="manual-search-button" data-refresh-run ${!state.run || state.busy ? "disabled" : ""}>Refresh</button>
          <button type="button" class="manual-search-button danger" data-cancel-run ${!isRunning || state.busy ? "disabled" : ""}>Cancel</button>
          <label class="manual-search-toggle"><input type="checkbox" data-include-rejected ${state.includeRejected ? "checked" : ""}> Show rejected results</label>
          <span class="manual-search-run-state ${isRunning ? "searching" : ""}">${escapeHtml(statusLabel(runState))}</span>
        </div>
        <div class="manual-search-summary">
          <section><h3>Provider progress</h3><div class="manual-search-providers">${providerProgressHtml()}</div></section>
          <section><h3>Search profile</h3><p>${escapeHtml(state.run?.source_profile?.display_name || state.run?.source_profile?.name || state.context?.source_profile_summary || "Core default profile")}</p><p class="manual-search-muted">${escapeHtml(state.run?.candidate_counts ? `${state.run.candidate_counts.accepted || 0} accepted · ${state.run.candidate_counts.rejected || 0} rejected` : "No candidate counts yet")}</p></section>
        </div>
        <div class="manual-search-filters" aria-label="Manual Search filters">
          <label>Decision<select data-filter="filter"><option value="all">All results</option><option value="accepted"${state.filter === "accepted" ? " selected" : ""}>Accepted</option><option value="rejected"${state.filter === "rejected" ? " selected" : ""}>Rejected</option></select></label>
          <label>Provider<select data-filter="provider">${selectOptions(optionValues("provider_id"), state.provider, "All providers")}</select></label>
          <label>Protocol<select data-filter="protocol">${selectOptions(optionValues("protocol"), state.protocol, "All protocols")}</select></label>
          <label>Language<select data-filter="language">${selectOptions(optionValues("language"), state.language, "All languages")}</select></label>
          <label>Format<select data-filter="format">${selectOptions(Array.from(new Set(state.results.map((item) => text(item.format || item.file_archive_format)).filter(Boolean))).sort(), state.format, "All formats")}</select></label>
          <label>Pack<select data-filter="pack"><option value="all">Pack and single</option><option value="single"${state.pack === "single" ? " selected" : ""}>Single only</option><option value="pack"${state.pack === "pack" ? " selected" : ""}>Packs only</option></select></label>
          <label>Assisted<select data-filter="assisted"><option value="all">All capabilities</option><option value="no"${state.assisted === "no" ? " selected" : ""}>Automatic only</option><option value="yes"${state.assisted === "yes" ? " selected" : ""}>Assisted only</option></select></label>
          <label>Minimum score<input type="number" min="0" step="1" data-filter="minimumScore" value="${escapeHtml(state.minimumScore)}"></label>
          <label>Sort<select data-sort><option value="decision"${state.sort === "decision" ? " selected" : ""}>Decision</option><option value="score"${state.sort === "score" ? " selected" : ""}>Score</option><option value="size"${state.sort === "size" ? " selected" : ""}>Size</option><option value="age"${state.sort === "age" ? " selected" : ""}>Age</option><option value="seeders"${state.sort === "seeders" ? " selected" : ""}>Seeders</option><option value="provider"${state.sort === "provider" ? " selected" : ""}>Provider</option><option value="title"${state.sort === "title" ? " selected" : ""}>Title</option></select></label>
          <button type="button" class="manual-search-button" data-clear-filters>Clear filters</button>
        </div>
        <div class="manual-search-results-header"><span>${results.length} of ${state.results.length} results</span><span>Core decisions and scores are shown without client-side reinterpretation.</span></div>
        ${results.length ? `<div class="manual-search-results-columns" aria-label="Result columns"><span>Decision</span><span>Release</span><span>Source</span><span>Language / format</span><span>Size / age</span><span>Availability</span><span>Score</span><span>Actions</span></div>` : ""}
        <div class="manual-search-results" aria-label="Manual Search results">${resultBody}</div>
        <div class="manual-search-error" role="alert" data-error${state.feedback ? "" : " hidden"}>${state.feedback ? `<strong>${escapeHtml(state.feedback.message)}</strong>${state.feedback.code ? `<details><summary>Technical detail</summary><code>${escapeHtml(state.feedback.code)}</code></details>` : ""}` : ""}</div>
      </section>`;
    bind(element);
    const preferred = element.querySelector("[data-start-search], [data-close-manual-search]");
    if (!element.dataset.focused && preferred) {
      element.dataset.focused = "1";
      window.requestAnimationFrame(() => preferred.focus());
    }
  }

  function showError(error, fallback) {
    const element = root();
    if (!element) return;
    const box = element.querySelector("[data-error]");
    if (!box) return;
    const code = error?.code || error?.error || "request_failed";
    let message = error?.detail || error?.message || fallback;
    if (["csrf_header_required", "csrf_cookie_required", "csrf_validation_failed", "session_expired"].includes(code)) {
      message = "Your secure session needs to be refreshed. Sign in again, then retry this action.";
    }
    state.feedback = { message: message || fallback, code };
    box.hidden = false;
    box.innerHTML = `<strong>${escapeHtml(message || fallback)}</strong><details><summary>Technical detail</summary><code>${escapeHtml(code)}</code></details>`;
  }

  async function loadResults() {
    if (!state.run?.id && !state.run?.run_id) return;
    const runId = state.run.id || state.run.run_id;
    const query = new URLSearchParams({ include_rejected: state.includeRejected ? "1" : "0", limit: "500", offset: "0" });
    const data = await api().request(`/api/manual-search/runs/${encodeURIComponent(runId)}/results?${query}`);
    state.results = Array.isArray(data.results) ? data.results : [];
  }

  async function refreshRun({ poll = false } = {}) {
    if (!state.run?.id && !state.run?.run_id) return;
    const runId = state.run.id || state.run.run_id;
    try {
      const [runData, diagnosticData] = await Promise.all([
        api().request(`/api/manual-search/runs/${encodeURIComponent(runId)}`),
        api().request(`/api/manual-search/runs/${encodeURIComponent(runId)}/diagnostics`).catch(() => ({ provider_attempts: [], queries: [] })),
      ]);
      state.run = { ...(runData.run || {}), run_id: runId, candidate_counts: runData.candidate_counts || {}, source_profile: state.run.source_profile };
      const diagnosticAttempts = Array.isArray(diagnosticData.provider_attempts) ? diagnosticData.provider_attempts : [];
      if (diagnosticAttempts.length || !Array.isArray(state.attempts)) state.attempts = diagnosticAttempts;
      state.queries = diagnosticData.queries || [];
      await loadResults();
      render();
      if (!TERMINAL_STATES.has(text(state.run.state).toLowerCase())) {
        stopPolling();
        state.pollTimer = window.setTimeout(() => refreshRun({ poll: true }), 1500);
      }
    } catch (error) {
      if (!poll) showError(error, "Manual Search could not be refreshed.");
      else {
        stopPolling();
        render();
        showError(error, "Manual Search stopped updating. Use Refresh to try again.");
      }
    }
  }

  async function startSearch(providers) {
    if (state.busy) return;
    state.busy = true;
    state.feedback = null;
    render();
    try {
      const context = state.context || {};
      const body = {
        series_id: context.series_id || "",
        edition_id: context.edition_id || "",
        issue_id: context.issue_id || "",
        unit_id: context.unit_id || "",
        providers: Array.isArray(providers) ? providers.filter(Boolean) : [],
        source_profile_id: context.source_profile_id || "",
        force_refresh: Boolean(state.run),
        include_rejected: state.includeRejected,
        pack_allowed: context.pack_allowed !== false,
      };
      const data = await api().request("/api/manual-search/runs", { method: "POST", body });
      state.run = { ...(data.context || {}), ...(data.run || {}), run_id: data.run_id, state: data.state || "queued", source_profile: data.source_profile || {} };
      state.results = [];
      state.attempts = (data.providers || []).map((provider) => typeof provider === "string" ? { provider_id: provider, state: "planned" } : provider);
      state.grabbed.clear();
      state.pendingGrabs.clear();
      await refreshRun();
    } catch (error) {
      render();
      showError(error, "Manual Search could not be started.");
    } finally {
      state.busy = false;
      render();
    }
  }

  async function cancelSearch() {
    if (state.busy || (!state.run?.run_id && !state.run?.id)) return;
    state.busy = true;
    state.feedback = null;
    render();
    try {
      const runId = state.run.run_id || state.run.id;
      await api().request(`/api/manual-search/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST", body: {} });
      await refreshRun();
    } catch (error) {
      render();
      showError(error, "The search could not be cancelled.");
    } finally {
      state.busy = false;
      render();
    }
  }

  async function grab(candidateId, { forceRejected = false } = {}) {
    const candidate = state.results.find((item) => text(item.candidate_id) === text(candidateId));
    if (!candidate || state.busy || (forceRejected ? !forceGrabAllowed(candidate) : !grabAllowed(candidate))) return;
    const proof = evidence(candidate);
    if (forceRejected) {
      const reasons = proof.negative.length ? proof.negative.map(humanize).join("; ") : "Core could not safely match this result";
      const acknowledged = window.confirm(
        `Core rejected this candidate for: ${reasons}. Force-grabbing can download the wrong work, edition, language, unit, or pack and consume bandwidth and storage. Automatic Search policy is unchanged. Continue anyway?`
      );
      if (!acknowledged) return;
    }
    const warning = candidate.pack_candidate || proof.negative.includes("pack_size_warning") || ["possible", "possible_match"].includes(text(candidate.decision?.decision).toLowerCase());
    if (warning && !forceRejected) {
      const coverage = candidate.likely_wanted_coverage ? ` InkDrop estimates ${candidate.likely_wanted_coverage} wanted items may be included.` : "";
      if (!window.confirm(`${candidate.pack_candidate ? `Large pack: ${formatBytes(candidate.size_bytes)}.` : "This candidate carries a warning."}${coverage} Continue with this explicit grab?`)) return;
    }
    if (state.pendingGrabs.has(text(candidateId))) return;
    state.pendingGrabs.add(text(candidateId));
    state.busy = true;
    state.feedback = null;
    render();
    try {
      const body = {
        override_pack_warning: warning || forceRejected,
        override_hard_limit: forceRejected && proof.negative.includes("pack_hard_limit_exceeded"),
        force_rejected: forceRejected,
        confirm_rejected_risk: forceRejected,
      };
      const data = await api().request(`/api/manual-search/candidates/${encodeURIComponent(candidateId)}/grab`, { method: "POST", body });
      state.grabbed.add(text(candidateId));
      const successMessage = data.idempotent_reuse ? "This candidate was already sent to the acquisition queue." : forceRejected ? "Rejected candidate force-grab sent to InkDrop’s acquisition queue." : "Candidate sent to InkDrop’s acquisition queue.";
      state.feedback = { message: successMessage, code: "" };
      render();
      const box = root()?.querySelector("[data-error]");
      if (box) {
        box.hidden = false;
        box.innerHTML = `<strong>${escapeHtml(successMessage)}</strong>`;
      }
    } catch (error) {
      render();
      showError(error, "The candidate could not be grabbed.");
    } finally {
      state.pendingGrabs.delete(text(candidateId));
      state.busy = false;
      render();
    }
  }

  function bind(element) {
    element.querySelectorAll("[data-close-manual-search]").forEach((button) => button.addEventListener("click", () => close()));
    element.querySelector("[data-start-search]")?.addEventListener("click", () => startSearch([]));
    element.querySelector("[data-search-selected]")?.addEventListener("click", () => {
      const providers = Array.from(element.querySelectorAll("[data-provider-select]:checked")).map((input) => input.value);
      if (providers.length) startSearch(providers);
    });
    element.querySelector("[data-refresh-run]")?.addEventListener("click", () => refreshRun());
    element.querySelector("[data-cancel-run]")?.addEventListener("click", cancelSearch);
    element.querySelector("[data-include-rejected]")?.addEventListener("change", async (event) => {
      state.includeRejected = event.target.checked;
      if (state.run) {
        await loadResults().catch((error) => showError(error, "Rejected candidates could not be loaded."));
      }
      render();
    });
    element.querySelectorAll("[data-filter]").forEach((control) => control.addEventListener("change", (event) => {
      state[event.target.dataset.filter] = event.target.value;
      render();
    }));
    element.querySelector("[data-sort]")?.addEventListener("change", (event) => {
      state.sort = event.target.value;
      render();
    });
    element.querySelector("[data-clear-filters]")?.addEventListener("click", () => {
      Object.assign(state, { filter: "all", provider: "", protocol: "", language: "", format: "", pack: "all", assisted: "all", minimumScore: "" });
      render();
    });
    element.querySelectorAll("[data-toggle-details]").forEach((button) => button.addEventListener("click", () => {
      const article = button.closest(".manual-search-result");
      const details = article?.querySelector(".manual-search-result-details");
      if (!details) return;
      const expanded = button.getAttribute("aria-expanded") === "true";
      button.setAttribute("aria-expanded", String(!expanded));
      button.textContent = expanded ? "Details" : "Hide details";
      details.hidden = expanded;
    }));
    element.querySelectorAll("[data-grab]").forEach((button) => button.addEventListener("click", () => grab(button.dataset.grab)));
    element.querySelectorAll("[data-force-grab]").forEach((button) => button.addEventListener("click", () => grab(button.dataset.forceGrab, { forceRejected: true })));
  }

  function reset(nextContext) {
    stopPolling();
    Object.assign(state, {
      context: nextContext || {}, run: null, results: [], attempts: [], queries: [], includeRejected: true,
      filter: "all", provider: "", protocol: "", language: "", format: "", pack: "all", assisted: "all",
      minimumScore: "", sort: "decision", direction: "desc", busy: false, grabbed: new Set(),
      feedback: null,
    });
  }

  function open(context = {}) {
    if (!(context.issue_id || context.unit_id)) {
      window.dispatchEvent(new CustomEvent("inkdrop:manual-search-unavailable", { detail: { reason: "canonical_unit_id_required" } }));
      return false;
    }
    state.returnFocus = document.activeElement;
    reset(context);
    let element = root();
    if (!element) {
      element = document.createElement("div");
      element.id = ROOT_ID;
      element.className = "manual-search-root";
      document.body.appendChild(element);
    }
    document.body.classList.add("manual-search-open");
    const url = new URL(window.location.href);
    if (url.searchParams.get(ROUTE_KEY) !== "1") {
      url.searchParams.set(ROUTE_KEY, "1");
      window.history.pushState({ ...(window.history.state || {}), inkdropManualSearch: true }, "", url);
      state.pushedRoute = true;
    }
    render();
    return true;
  }

  function close({ fromPopState = false } = {}) {
    stopPolling();
    const element = root();
    if (element) element.remove();
    document.body.classList.remove("manual-search-open");
    if (!fromPopState && state.pushedRoute && new URL(window.location.href).searchParams.get(ROUTE_KEY) === "1") {
      state.pushedRoute = false;
      window.history.back();
    } else {
      state.pushedRoute = false;
      state.returnFocus?.focus?.();
    }
  }

  window.addEventListener("popstate", () => {
    if (root() && new URL(window.location.href).searchParams.get(ROUTE_KEY) !== "1") close({ fromPopState: true });
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && root()) close();
  });

  window.InkDropManualSearch = { open, close, refresh: refreshRun, _state: state };
})();
