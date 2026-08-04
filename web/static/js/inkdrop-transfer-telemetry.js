(function (global) {
  "use strict";

  function first(values) {
    for (var index = 0; index < values.length; index += 1) {
      if (
        values[index] !== undefined &&
        values[index] !== null &&
        values[index] !== ""
      )
        return values[index];
    }
    return null;
  }

  function finite(value) {
    if (value === null || value === undefined || value === "") return null;
    var number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function percent(value) {
    if (typeof value === "string" && value.indexOf("%") >= 0)
      value = value.replace("%", "").trim();
    var number = finite(value);
    if (number === null) return null;
    if (number > 0 && number <= 1) number *= 100;
    return Math.max(0, Math.min(100, number));
  }

  function formatBytes(value) {
    var bytes = finite(value);
    if (bytes === null || bytes < 0) return "";
    var units = ["B", "KB", "MB", "GB", "TB"];
    var unit = 0;
    while (bytes >= 1024 && unit < units.length - 1) {
      bytes /= 1024;
      unit += 1;
    }
    var precision = unit === 0 ? 0 : bytes >= 10 ? 1 : 2;
    return (
      bytes.toFixed(precision).replace(/(\.\d*?[1-9])0+$|\.0+$/, "$1") +
      " " +
      units[unit]
    );
  }

  function formatDuration(value) {
    var seconds = finite(value);
    if (seconds === null || seconds < 0) return "";
    seconds = Math.round(seconds);
    var hours = Math.floor(seconds / 3600);
    var minutes = Math.floor((seconds % 3600) / 60);
    var remaining = seconds % 60;
    if (hours) return hours + "h " + minutes + "m";
    if (minutes) return minutes + "m " + remaining + "s";
    return remaining + "s";
  }

  function stateLabel(rawState) {
    var state = String(rawState || "").toLowerCase();
    if (state.indexOf("stall") >= 0) return { label: "Stalled", tone: "error" };
    if (state.indexOf("unavailable") >= 0)
      return { label: "Client unavailable", tone: "error" };
    if (state.indexOf("missing") >= 0)
      return { label: "File missing", tone: "error" };
    if (state.indexOf("conflict") >= 0)
      return { label: "Import conflict", tone: "error" };
    if (
      state.indexOf("fail") >= 0 ||
      state.indexOf("error") >= 0 ||
      state.indexOf("blocked") >= 0
    )
      return { label: "Transfer failed", tone: "error" };
    if (state.indexOf("retry") >= 0)
      return { label: "Retrying", tone: "warning" };
    if (state.indexOf("need") >= 0 || state.indexOf("review") >= 0)
      return { label: "Needs user", tone: "error" };
    if (state.indexOf("import") >= 0 || state.indexOf("verify") >= 0)
      return { label: "Importing", tone: "active" };
    if (state.indexOf("download") >= 0 || state.indexOf("transfer") >= 0)
      return { label: "Downloading", tone: "active" };
    if (state.indexOf("search") >= 0)
      return { label: "Searching", tone: "active" };
    if (state.indexOf("queue") >= 0)
      return { label: "Queued", tone: "waiting" };
    if (state.indexOf("wait") >= 0)
      return { label: "Waiting", tone: "waiting" };
    return { label: "Active", tone: "active" };
  }

  function titleCase(value) {
    return String(value || "")
      .replace(/[_-]+/g, " ")
      .replace(/\s+/g, " ")
      .trim()
      .replace(/\b\w/g, function (letter) {
        return letter.toUpperCase();
      });
  }

  function normalizeContractState(transfer) {
    var raw = String(
      transfer.transfer_state || transfer.state || "",
    ).toLowerCase();
    if (transfer.client_error)
      return { label: "Client unavailable", tone: "error" };
    if (transfer.stalled === true || raw === "stalled")
      return { label: "Stalled", tone: "error" };
    if (raw === "failed") return { label: "Transfer failed", tone: "error" };
    if (raw === "completed")
      return { label: "Transfer complete", tone: "success" };
    if (raw === "downloading") return { label: "Downloading", tone: "active" };
    if (raw === "queued") return { label: "Queued", tone: "waiting" };
    if (raw === "unknown") return { label: "Status unknown", tone: "neutral" };
    return stateLabel(raw);
  }

  function normalizeContract(row, nowSeconds) {
    var transfer =
      row.transfer && typeof row.transfer === "object" ? row.transfer : row;
    var state = normalizeContractState(transfer);
    var progressKind = String(transfer.progress_kind || "").toLowerCase();
    var progress =
      progressKind === "determinate"
        ? percent(transfer.percent_complete)
        : null;
    var totalBytes = finite(transfer.bytes_total);
    var completedBytes = finite(transfer.bytes_completed);
    if (completedBytes !== null && totalBytes !== null)
      completedBytes = Math.min(completedBytes, totalBytes);
    var bytesPerSecond = finite(transfer.download_rate_bytes_per_second);
    var etaSeconds =
      state.label === "Transfer complete" ? null : finite(transfer.eta_seconds);
    var elapsedSeconds = finite(transfer.elapsed_seconds);
    var startedAt = finite(transfer.started_at);
    var updatedAt = finite(transfer.last_updated_at);
    var now = finite(nowSeconds);
    if (elapsedSeconds === null && startedAt !== null && now !== null)
      elapsedSeconds = Math.max(0, now - startedAt);
    var updatedAgoSeconds =
      updatedAt !== null && now !== null ? Math.max(0, now - updatedAt) : null;
    var stage = first([
      transfer.import_stage ? titleCase(transfer.import_stage) : null,
      transfer.next_step,
      transfer.stalled_reason,
      transfer.client_error,
    ]);
    var parts = [];
    if (progress !== null) parts.push(Math.round(progress) + "%");
    if (progress !== null && completedBytes !== null && totalBytes !== null) {
      parts.push(formatBytes(completedBytes) + " / " + formatBytes(totalBytes));
    }
    if (progress !== null && bytesPerSecond !== null && bytesPerSecond > 0)
      parts.push(formatBytes(bytesPerSecond) + "/s");
    if (progress !== null && etaSeconds !== null)
      parts.push(formatDuration(etaSeconds) + " remaining");
    if (progress === null && stage) parts.push(stage);
    if (progress === null && elapsedSeconds !== null)
      parts.push(formatDuration(elapsedSeconds) + " elapsed");
    else if (progress === null && updatedAgoSeconds !== null)
      parts.push("checked " + formatDuration(updatedAgoSeconds) + " ago");
    return {
      state: String(transfer.transfer_state || transfer.state || ""),
      nativeState: transfer.native_state || null,
      label: state.label,
      tone: state.tone,
      determinate: progress !== null,
      percent: progress,
      completedBytes: completedBytes,
      totalBytes: totalBytes,
      bytesPerSecond: bytesPerSecond,
      uploadBytesPerSecond: finite(transfer.upload_rate_bytes_per_second),
      etaSeconds: etaSeconds,
      elapsedSeconds: elapsedSeconds,
      updatedAgoSeconds: updatedAgoSeconds,
      client: String(transfer.client || "").trim(),
      clientItemId: transfer.client_item_id || null,
      detail: String(
        first([
          transfer.next_step,
          transfer.stalled_reason,
          transfer.client_error,
          transfer.native_state,
        ]) || "",
      ).trim(),
      source: transfer.source || null,
      provider: transfer.provider || null,
      displayTitle: transfer.display_title || null,
      updatedAt: updatedAt,
      summary: parts.join(" · "),
    };
  }

  function normalize(row, nowSeconds) {
    row = row && typeof row === "object" ? row : {};
    if (
      (row.transfer && typeof row.transfer === "object") ||
      row.progress_kind ||
      row.percent_complete !== undefined
    ) {
      return normalizeContract(row, nowSeconds);
    }
    var task =
      row.download_task && typeof row.download_task === "object"
        ? row.download_task
        : {};
    var rawState =
      first([
        row.transfer_state,
        task.transfer_state,
        row.state,
        task.state,
        row.status,
        task.status,
        row.display_state,
        row.queue_state,
        row.lifecycle_phase,
      ]) || "";
    var state = stateLabel(rawState);
    var progressScope = String(
      first([
        row.progress_scope,
        task.progress_scope,
        row.telemetry_scope,
        task.telemetry_scope,
      ]) || "",
    ).toLowerCase();
    var importProgressIsExplicit =
      progressScope === "import" || progressScope === "importing";
    var progress = percent(
      first([
        task.transfer_percent,
        row.transfer_percent,
        task.progress,
        row.progress,
        task.percentComplete,
        row.percentComplete,
        task.percentage,
        row.percentage,
      ]),
    );
    if (state.label === "Importing" && !importProgressIsExplicit)
      progress = null;
    var totalBytes = finite(
      first([
        task.total_bytes,
        row.total_bytes,
        task.size_bytes,
        row.size_bytes,
        task.total_size_bytes,
        row.total_size_bytes,
      ]),
    );
    var completedBytes = finite(
      first([
        task.completed_bytes,
        row.completed_bytes,
        task.downloaded_bytes,
        row.downloaded_bytes,
      ]),
    );
    if (completedBytes === null && totalBytes !== null && progress !== null)
      completedBytes = (totalBytes * progress) / 100;
    var bytesPerSecond = finite(
      first([
        task.bytes_per_second,
        row.bytes_per_second,
        task.download_rate_bytes,
        row.download_rate_bytes,
        task.speed_bytes,
        row.speed_bytes,
      ]),
    );
    var etaSeconds = finite(
      first([
        task.eta_seconds,
        row.eta_seconds,
        task.time_left_seconds,
        row.time_left_seconds,
      ]),
    );
    if (state.label === "Importing" && !importProgressIsExplicit) {
      completedBytes = null;
      totalBytes = null;
      bytesPerSecond = null;
      etaSeconds = null;
    }
    var startedAt =
      state.label === "Importing"
        ? finite(
            first([
              row.import_started_at,
              task.import_started_at,
              row.stage_started_at,
              task.stage_started_at,
            ]),
          )
        : finite(
            first([
              row.started_at,
              task.started_at,
              row.stage_started_at,
              task.stage_started_at,
            ]),
          );
    var updatedAt = finite(first([task.updated_at, row.updated_at]));
    var elapsedSeconds = finite(
      first([task.elapsed_seconds, row.elapsed_seconds]),
    );
    var now = finite(nowSeconds);
    if (elapsedSeconds === null && startedAt !== null && now !== null)
      elapsedSeconds = Math.max(0, now - startedAt);
    var updatedAgoSeconds =
      updatedAt !== null && now !== null ? Math.max(0, now - updatedAt) : null;
    var client = String(
      first([
        task.client,
        row.client,
        task.download_client,
        row.download_client,
        task.source,
        row.current_source,
        row.source,
      ]) || "",
    ).trim();
    var detail = String(
      first([
        task.stage_detail,
        row.stage_detail,
        row.next_action,
        row.activity_summary,
      ]) || "",
    ).trim();
    var parts = [];
    if (progress !== null) parts.push(Math.round(progress) + "%");
    if (completedBytes !== null && totalBytes !== null)
      parts.push(formatBytes(completedBytes) + " / " + formatBytes(totalBytes));
    if (bytesPerSecond !== null && bytesPerSecond > 0)
      parts.push(formatBytes(bytesPerSecond) + "/s");
    if (etaSeconds !== null)
      parts.push(formatDuration(etaSeconds) + " remaining");
    if (progress === null && elapsedSeconds !== null)
      parts.push(formatDuration(elapsedSeconds) + " elapsed");
    else if (progress === null && updatedAgoSeconds !== null)
      parts.push("updated " + formatDuration(updatedAgoSeconds) + " ago");
    return {
      state: String(rawState),
      label: state.label,
      tone: state.tone,
      determinate: progress !== null,
      percent: progress,
      completedBytes: completedBytes,
      totalBytes: totalBytes,
      bytesPerSecond: bytesPerSecond,
      etaSeconds: etaSeconds,
      elapsedSeconds: elapsedSeconds,
      updatedAgoSeconds: updatedAgoSeconds,
      client: client,
      detail: detail,
      updatedAt: updatedAt,
      summary: parts.join(" · "),
    };
  }

  global.InkDropTransferTelemetry = Object.freeze({
    normalize: normalize,
    normalizeContract: normalizeContract,
    formatBytes: formatBytes,
    formatDuration: formatDuration,
    percent: percent,
  });
})(typeof window !== "undefined" ? window : globalThis);
