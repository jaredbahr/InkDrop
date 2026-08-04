(function (global) {
  "use strict";

  const ENDPOINTS = {
    summary: "/api/inkdrop-activity/summary",
    current: "/api/inkdrop-activity/current",
    detail: "/api/inkdrop-activity/",
    deferred: "/api/inkdrop-maintenance/deferred-queue-sync",
  };
  const mountedRoots = new WeakSet();
  let remountTimer = null;
  let loadingPromise = null;
  let activityObserver = null;

  function text(value) {
    return value == null ? "" : String(value);
  }

  function escapeHtml(value) {
    return text(value).replace(
      /[&<>"']/g,
      (char) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[char],
    );
  }

  function scrub(value) {
    return text(value)
      .replace(/https?:\/\/\S+/gi, "[url]")
      .replace(/[A-Za-z]:\\[^\s]+/g, "[path]")
      .replace(/\\\\[^\s]+/g, "[path]")
      .replace(/\b[a-f0-9]{32,}\b/gi, "[id]")
      .replace(/\b[A-Za-z0-9_-]{36,}\b/g, "[id]");
  }

  function normalize(value) {
    return text(value)
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "_")
      .replace(/^_+|_+$/g, "");
  }

  function first(row, names) {
    for (const name of names) {
      if (
        row &&
        row[name] !== undefined &&
        row[name] !== null &&
        row[name] !== ""
      )
        return row[name];
    }
    return "";
  }

  function number(value) {
    if (value === null || value === undefined || value === "") return null;
    const numeric = Number(value);
    return Number.isFinite(numeric) ? numeric : null;
  }

  function percent(value) {
    const numeric = number(value);
    if (numeric == null) return null;
    if (numeric <= 1 && numeric > 0) return numeric * 100;
    return Math.max(0, Math.min(100, numeric));
  }

  function formatBytes(value) {
    const numeric = number(value);
    if (numeric == null) return "";
    const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    let current = numeric;
    let index = 0;
    while (current >= 1024 && index < units.length - 1) {
      current /= 1024;
      index += 1;
    }
    return `${current.toFixed(index ? 1 : 0)} ${units[index]}`;
  }

  function formatDuration(value) {
    const seconds = number(value);
    if (seconds == null) return "";
    if (seconds < 60) return `${Math.max(0, Math.round(seconds))}s`;
    if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
    if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
    return `${Math.round(seconds / 86400)}d`;
  }

  function timestampMilliseconds(value) {
    if (value === null || value === undefined || value === "") return null;
    const numeric = number(value);
    if (numeric != null)
      return Math.abs(numeric) < 100000000000 ? numeric * 1000 : numeric;
    const parsed = Date.parse(value);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function formatRelative(value) {
    const time = timestampMilliseconds(value);
    if (time == null) return scrub(value);
    const delta = Math.max(0, (Date.now() - time) / 1000);
    return `${formatDuration(delta)} ago`;
  }

  function formatScheduled(value) {
    const time = timestampMilliseconds(value);
    if (time == null) return scrub(value);
    const delta = (time - Date.now()) / 1000;
    if (delta > 0) return `in ${formatDuration(delta)}`;
    if (delta > -60) return "due now";
    return `${formatDuration(Math.abs(delta))} overdue`;
  }

  function humanize(value) {
    return text(value)
      .trim()
      .replace(/[_-]+/g, " ")
      .replace(/\b\w/g, (letter) => letter.toUpperCase());
  }

  function formatSuccessfulAction(value) {
    if (!value) return "";
    if (typeof value !== "object" || Array.isArray(value)) return scrub(value);
    const kind = humanize(
      first(value, ["kind", "action", "type"]) || "Successful action",
    );
    const status = humanize(first(value, ["status", "result"]));
    const at = first(value, ["at", "completed_at", "created_at"]);
    return [kind, status, at ? formatRelative(at) : ""]
      .filter(Boolean)
      .join(" · ");
  }

  function apiGet(path) {
    if (global.InkDropApi && typeof global.InkDropApi.request === "function") {
      return global.InkDropApi.request(path, {
        method: "GET",
        cache: "no-store",
      });
    }
    return fetch(path, {
      method: "GET",
      credentials: "same-origin",
      cache: "no-store",
      headers: { Accept: "application/json" },
    }).then((response) => {
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return response.json();
    });
  }

  function fixture(root, attr) {
    const id = root.getAttribute(attr);
    const node = id ? document.getElementById(id) : null;
    if (!node) return null;
    return JSON.parse(node.textContent);
  }

  function rowsFrom(payload) {
    if (!payload) return [];
    if (Array.isArray(payload)) return payload;
    return (
      payload.activity ||
      payload.items ||
      payload.rows ||
      payload.activities ||
      payload.current ||
      payload.data ||
      []
    );
  }

  function countFrom(summary, names, rows, predicate) {
    const value = first(summary || {}, names);
    const numeric = number(value);
    if (numeric != null) return numeric;
    return rows.filter(predicate).length;
  }

  function activityStatus(summary, rows) {
    const raw = normalize(
      first(summary || {}, ["status", "state", "overall_status"]),
    );
    if (
      raw.includes("attention") ||
      rows.some(
        (row) => row.needs_user || normalize(row.stage).includes("attention"),
      )
    ) {
      return { label: "Needs Attention", tone: "danger" };
    }
    if (
      raw.includes("idle") ||
      (!rows.length &&
        !countFrom(summary, ["active_total", "active"], rows, () => true))
    ) {
      return { label: "Idle", tone: "muted" };
    }
    return { label: "Working", tone: "work" };
  }

  function completionLabel(row) {
    const nested =
      row && row.ownership_evidence && row.ownership_evidence.completion;
    const raw = normalize(
      first(row, [
        "completion_state",
        "reader_state",
        "library_state",
        "verification_state",
      ]) || first(nested || {}, ["state"]),
    );
    const labels = {
      downloaded: "Downloaded",
      missing: "File missing",
      file_present: "File present",
      archive_valid: "Archive valid",
      imported: "Imported",
      folder_verified: "Folder verified",
      folder_complete: "Folder verified",
      reader_scan_pending: "Reader scan pending",
      reader_visibility_pending: "Reader verification pending",
      kavita_scan_pending: "Reader scan pending",
      waiting_for_kavita: "Waiting for Kavita",
      reader_wait: "Waiting for Kavita",
      reader_visible: "Visible in Kavita",
      visible_in_kavita: "Visible in Kavita",
      kavita_unavailable: "Kavita unavailable",
      reader_unavailable: "Kavita unavailable",
      reader_visibility_unavailable: "Kavita unavailable",
      reader_visibility_mismatch: "Reader visibility mismatch",
      visibility_mismatch: "Reader visibility mismatch",
      identity_conflict: "Identity conflict",
    };
    return labels[raw] || "";
  }

  function sourceClient(row) {
    const child = first(row, [
      "child_source_name",
      "child_source_id",
      "child_source",
      "indexer",
      "provider_child",
      "source_child",
    ]);
    const parent = first(row, [
      "provider",
      "source",
      "client",
      "download_client",
      "source_client",
    ]);
    if (parent && child && text(parent) !== text(child))
      return scrub(`${parent} · ${child}`);
    return scrub(parent || child || "Not reported");
  }

  function stageLabel(row) {
    const explicit = first(row, ["stage_label", "display_stage"]);
    const raw = normalize(
      first(row, ["stage", "state", "queue_state", "transfer_state"]),
    );
    const client = normalize(sourceClient(row));
    const next = normalize(first(row, ["next_action", "next_step"]));

    if (explicit) return scrub(explicit);
    if (raw === "import_ready" || next.includes("ready_to_import"))
      return "Ready to import";
    if (raw.includes("importing")) {
      const owned =
        row.importer_active ||
        row.importer_owned ||
        row.import_owner ||
        row.importer ||
        row.import_started_at;
      return owned ? "Importing" : "Ready to import";
    }
    if (
      client.includes("slskd") &&
      (raw.includes("remote") || raw.includes("queued"))
    )
      return "Queued remotely in SLSKD";
    if (raw.includes("reader") && raw.includes("pending"))
      return "Reader scan pending";
    if (raw.includes("kavita") && raw.includes("wait"))
      return "Waiting for Kavita";
    if (raw.includes("download") && raw.includes("complete"))
      return "Client complete";
    if (raw.includes("seed")) return "Seeding";
    if (raw.includes("download")) return `Downloading in ${sourceClient(row)}`;
    if (raw.includes("client_queued") || raw.includes("queued_remote"))
      return `Queued in ${sourceClient(row)}`;
    if (raw.includes("search")) return `Searching ${sourceClient(row)}`;
    if (raw.includes("retry")) return "Retry scheduled";
    if (raw.includes("identity")) return "Identity conflict";
    if (raw.includes("visibility")) return "Reader visibility mismatch";
    if (raw)
      return raw
        .split("_")
        .map((part) => (part ? part[0].toUpperCase() + part.slice(1) : ""))
        .join(" ");
    return completionLabel(row) || "Waiting";
  }

  function progressModel(row) {
    const client = normalize(sourceClient(row));
    const raw = normalize(
      first(row, ["stage", "state", "queue_state", "transfer_state"]),
    );
    if (
      client.includes("slskd") &&
      (raw.includes("remote") || raw.includes("queued"))
    ) {
      return {
        kind: "stage",
        label: "Remote queue",
        detail: "No trusted percentage available",
      };
    }
    const kind = normalize(first(row, ["progress_kind", "progress_type"]));
    const corePercent = number(row && row.percent_complete);
    let pct =
      corePercent == null
        ? percent(
            first(row, ["progress_percent", "percent", "completion_percent"]),
          )
        : corePercent;
    const done = number(
      first(row, [
        "bytes_completed",
        "bytes_done",
        "completed_bytes",
        "downloaded_bytes",
      ]),
    );
    const total = number(
      first(row, ["bytes_total", "total_bytes", "size_bytes"]),
    );
    if (pct == null && done != null && total && total > 0)
      pct = (done / total) * 100;
    const speed = number(
      first(row, [
        "speed_bytes_per_second",
        "rate_bytes_per_second",
        "download_rate",
      ]),
    );
    const parts = [];
    if (done != null && total)
      parts.push(`${formatBytes(done)} / ${formatBytes(total)}`);
    if (speed != null) parts.push(`${formatBytes(speed)}/s`);
    if (kind === "determinate" || pct != null) {
      const displayPercent =
        Math.round(Math.max(0, Math.min(100, pct || 0)) * 10) / 10;
      return {
        kind: "determinate",
        percent: displayPercent,
        detail: parts.join(" · "),
      };
    }
    return {
      kind: "stage",
      label: stageLabel(row),
      detail: parts.join(" · ") || "Indeterminate",
    };
  }

  function etaAge(row) {
    const eta = number(first(row, ["eta_seconds", "eta", "seconds_remaining"]));
    if (eta != null && eta >= 0) return `ETA ${formatDuration(eta)}`;
    const elapsed = number(first(row, ["elapsed_seconds", "age_seconds"]));
    if (elapsed != null) return `${formatDuration(elapsed)} elapsed`;
    const updated = first(row, [
      "last_updated_at",
      "last_update",
      "updated_at",
      "last_seen_at",
    ]);
    if (updated) return formatRelative(updated);
    return "";
  }

  function nextText(row) {
    return scrub(
      first(row, ["next_action", "next", "next_step", "action_summary"]) ||
        "No action reported",
    );
  }

  function activityId(row, index) {
    return text(
      first(row, ["activity_id", "id", "queue_id", "row_id"]) ||
        `activity-${index}`,
    );
  }

  function title(row) {
    const series = scrub(
      first(row, [
        "display_title",
        "series_title",
        "series",
        "title",
        "work_title",
      ]) || "Unknown series",
    );
    const issue = scrub(
      first(row, [
        "issue_or_volume",
        "issue_label",
        "issue",
        "unit_label",
        "chapter_label",
      ]),
    );
    return issue ? `${series} ${issue}` : series;
  }

  function renderProgress(model) {
    if (model.kind === "determinate") {
      return `<div class="inkdrop-progress" role="progressbar" aria-label="Transfer progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${model.percent}"><span style="width:${model.percent}%"></span></div><div>${model.percent}%${model.detail ? ` · ${escapeHtml(model.detail)}` : ""}</div>`;
    }
    return `<div class="inkdrop-progress-stage">${escapeHtml(model.label)}</div><div class="muted">${escapeHtml(model.detail || "")}</div>`;
  }

  function renderSummary(summary, rows, deferred) {
    const status = activityStatus(summary, rows);
    const chips = [
      [
        "Active",
        countFrom(summary, ["active_total", "active"], rows, () => true),
      ],
      [
        "Searching",
        countFrom(summary, ["searching"], rows, (row) =>
          normalize(row.stage).includes("search"),
        ),
      ],
      [
        "Client queued",
        countFrom(
          summary,
          ["client_queued", "queued_in_clients"],
          rows,
          (row) => normalize(row.stage).includes("queued"),
        ),
      ],
      [
        "Downloading",
        countFrom(summary, ["downloading"], rows, (row) =>
          normalize(row.stage).includes("download"),
        ),
      ],
      [
        "Ready to import",
        countFrom(
          summary,
          ["ready_to_import", "import_ready"],
          rows,
          (row) => normalize(row.stage) === "import_ready",
        ),
      ],
      [
        "Importing",
        countFrom(
          summary,
          ["importing"],
          rows,
          (row) =>
            normalize(row.stage).includes("importing") &&
            (row.importer_active || row.importer_owned),
        ),
      ],
      [
        "Reader verification",
        countFrom(summary, ["reader_verification"], rows, (row) =>
          normalize(row.completion_state).includes("reader"),
        ),
      ],
      [
        "Retry scheduled",
        countFrom(summary, ["retry_scheduled"], rows, (row) =>
          normalize(row.stage).includes("retry"),
        ),
      ],
      [
        "Needs user",
        countFrom(summary, ["needs_user"], rows, (row) => row.needs_user),
      ],
    ];
    const last = formatSuccessfulAction(
      first(summary || {}, [
        "last_successful_action",
        "last_success",
        "last_action",
      ]),
    );
    const nextRunValue =
      first(deferred || {}, [
        "next_scheduled_worker_run",
        "next_worker_run",
        "next_run",
        "next_run_at",
      ]) ||
      first(summary || {}, [
        "next_scheduled_worker_run",
        "next_worker_run",
        "next_run",
        "next_run_at",
      ]) ||
      first((summary || {}).scheduler || {}, ["next_run_at"]);
    const nextRun = formatScheduled(nextRunValue);
    return `
      <section class="inkdrop-activity-summary" aria-label="Activity summary">
        <div class="inkdrop-activity-status ${status.tone}">
          <strong>${escapeHtml(status.label)}</strong>
          <span>${last ? `Last successful action: ${escapeHtml(last)}` : "No recent successful action reported"}</span>
          <span>${nextRun ? `Next work: ${escapeHtml(nextRun)}` : "Next work time unavailable"}</span>
        </div>
        <div class="inkdrop-activity-chips">
          ${chips.map(([label, value]) => `<span class="inkdrop-activity-chip"><b>${value}</b>${escapeHtml(label)}</span>`).join("")}
        </div>
      </section>`;
  }

  function renderRows(rows) {
    if (!rows.length) {
      return `<div class="inkdrop-activity-empty">InkDrop is idle. Nothing is running right now.</div>`;
    }
    return `
      <table class="inkdrop-activity-table">
        <thead><tr>
          <th>Series / Issue</th><th>Stage</th><th>Source / Client</th><th>Progress</th><th>ETA / Age</th><th>Next</th><th>Actions</th>
        </tr></thead>
        <tbody>
          ${rows
            .map((row, index) => {
              const id = activityId(row, index);
              const progress = progressModel(row);
              const completion = completionLabel(row);
              return `<tr class="inkdrop-activity-row" data-activity-id="${escapeHtml(id)}">
              <td data-label="Series / Issue"><strong title="${escapeHtml(title(row))}">${escapeHtml(title(row))}</strong><span>${escapeHtml(scrub(first(row, ["subtitle", "description", "issue_title"]) || ""))}</span></td>
              <td data-label="Stage"><span class="inkdrop-stage">${escapeHtml(stageLabel(row))}</span>${completion ? `<span class="inkdrop-completion">${escapeHtml(completion)}</span>` : ""}</td>
              <td data-label="Source / Client">${escapeHtml(sourceClient(row))}</td>
              <td data-label="Progress">${renderProgress(progress)}</td>
              <td data-label="ETA / Age">${escapeHtml(etaAge(row) || "Not reported")}</td>
              <td data-label="Next">${escapeHtml(nextText(row))}</td>
              <td data-label="Actions"><button type="button" class="inkdrop-activity-expand" aria-expanded="false" aria-controls="inkdrop-activity-detail-${escapeHtml(id)}">Details</button></td>
            </tr><tr id="inkdrop-activity-detail-${escapeHtml(id)}" class="inkdrop-activity-detail" hidden><td colspan="7"><div class="inkdrop-activity-detail-box">Details load on demand.</div></td></tr>`;
            })
            .join("")}
        </tbody>
      </table>`;
  }

  function render(root, state) {
    const rows = rowsFrom(state.current);
    root.innerHTML = `
      <div class="inkdrop-activity-dashboard">
        <div class="inkdrop-activity-heading">
          <h2>Activity</h2>
          <button type="button" class="inkdrop-activity-retry">Refresh</button>
        </div>
        ${renderSummary(state.summary || {}, rows, state.deferred || {})}
        ${renderRows(rows)}
      </div>`;
  }

  function renderError(root, error) {
    root.innerHTML = `<div class="inkdrop-activity-dashboard"><div class="inkdrop-activity-error" role="alert"><strong>Activity is unavailable.</strong><span>${escapeHtml(scrub(error && error.message ? error.message : error))}</span><button type="button" class="inkdrop-activity-retry">Retry Activity</button></div></div>`;
  }

  function load(root) {
    if (loadingPromise) return loadingPromise;
    root.innerHTML = `<div class="inkdrop-activity-dashboard"><div class="inkdrop-activity-loading">Loading Activity…</div></div>`;
    const fixtureState = {
      summary: fixture(root, "data-activity-summary-fixture"),
      current: fixture(root, "data-activity-current-fixture"),
      deferred: fixture(root, "data-activity-deferred-fixture"),
    };
    if (fixtureState.summary || fixtureState.current || fixtureState.deferred) {
      render(root, fixtureState);
      return Promise.resolve();
    }
    loadingPromise = Promise.all([
      apiGet(ENDPOINTS.summary),
      apiGet(ENDPOINTS.current),
      apiGet(ENDPOINTS.deferred),
    ])
      .then(([summary, current, deferred]) => {
        render(root, { summary, current, deferred });
        return null;
      })
      .catch((error) => {
        renderError(root, error);
        return null;
      })
      .finally(() => {
        loadingPromise = null;
      });
    return loadingPromise;
  }

  function bind(root) {
    root.addEventListener("click", (event) => {
      const retry = event.target.closest(".inkdrop-activity-retry");
      if (retry) {
        load(root);
        return;
      }
      const button = event.target.closest(".inkdrop-activity-expand");
      if (!button) return;
      const expanded = button.getAttribute("aria-expanded") === "true";
      button.setAttribute("aria-expanded", String(!expanded));
      const detail = root.querySelector(
        `#${CSS.escape(button.getAttribute("aria-controls"))}`,
      );
      if (!detail) return;
      detail.hidden = expanded;
      if (!expanded && !detail.dataset.loaded) {
        const row = button.closest("tr");
        const id = row ? row.getAttribute("data-activity-id") : "";
        const box = detail.querySelector(".inkdrop-activity-detail-box");
        box.textContent = "Loading details...";
        apiGet(`${ENDPOINTS.detail}${encodeURIComponent(id)}`)
          .then((payload) => {
            detail.dataset.loaded = "true";
            const safe = scrub(JSON.stringify(payload, null, 2));
            box.innerHTML = `<pre>${escapeHtml(safe)}</pre>`;
          })
          .catch((error) => {
            box.textContent = `Details unavailable: ${scrub(error.message || error)}`;
          });
      }
    });
    root.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      root
        .querySelectorAll(".inkdrop-activity-expand[aria-expanded='true']")
        .forEach((button) => {
          button.setAttribute("aria-expanded", "false");
          const detail = root.querySelector(
            `#${CSS.escape(button.getAttribute("aria-controls"))}`,
          );
          if (detail) detail.hidden = true;
        });
    });
  }

  function mount(scope) {
    const searchRoot = scope || document;
    searchRoot
      .querySelectorAll("[data-inkdrop-activity-dashboard]")
      .forEach((root) => {
        if (mountedRoots.has(root)) return;
        mountedRoots.add(root);
        bind(root);
        load(root);
      });
  }

  function shouldAutoMount() {
    const locationText = `${location.pathname} ${location.hash}`.toLowerCase();
    if (locationText.includes("activity") || locationText.includes("queue"))
      return true;
    const heading = document.querySelector("h1, h2, .page-title");
    return heading && /^(activity|queue)\b/i.test(heading.textContent.trim());
  }

  function ensureAutoRoot() {
    if (
      document.querySelector("[data-inkdrop-activity-dashboard]") ||
      !shouldAutoMount()
    )
      return;
    // These are ordered preferences, not a selector list. querySelector on a
    // comma list returns whichever match comes first in the *document*, and
    // body precedes main, so "main, #content, .content, body" always resolved
    // to body -- the panel was prepended outside the app shell and rendered
    // full document width across the sidebar.
    const host =
      document.querySelector("main") ||
      document.querySelector("#content") ||
      document.querySelector(".content") ||
      document.body;
    if (!host) return;
    const root = document.createElement("section");
    root.setAttribute("data-inkdrop-activity-dashboard", "auto");
    root.className = "inkdrop-activity-auto-root";
    host.prepend(root);
  }

  function scheduleMount() {
    clearTimeout(remountTimer);
    remountTimer = setTimeout(() => {
      ensureAutoRoot();
      mount(document);
    }, 80);
  }

  function disconnect() {
    if (activityObserver) {
      activityObserver.disconnect();
      activityObserver = null;
    }
  }

  global.InkDropActivityUi = {
    mount,
    renderFixture: render,
    endpoints: ENDPOINTS,
    disconnect,
  };
  document.addEventListener("DOMContentLoaded", scheduleMount);
  if (document.readyState !== "loading") scheduleMount();
  activityObserver = new MutationObserver(scheduleMount);
  activityObserver.observe(document.documentElement, {
    childList: true,
    subtree: true,
  });
})(window);
