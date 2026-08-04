(function () {
  "use strict";

  const DEFAULT_CSRF_COOKIE = "inkdrop_csrf";
  const DEFAULT_CSRF_HEADER = "X-InkDrop-CSRF";
  const AUTH_STATUS_PATH = "/api/auth/status";
  const MUTATION_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);
  let mutationContractCache = null;
  let mutationContractPromise = null;

  function cookieValue(name) {
    const prefix = `${encodeURIComponent(name)}=`;
    for (const part of String(document.cookie || "").split(";")) {
      const value = part.trim();
      if (value.startsWith(prefix))
        return decodeURIComponent(value.slice(prefix.length));
    }
    return "";
  }

  function isPlainObject(value) {
    if (!value || Object.prototype.toString.call(value) !== "[object Object]")
      return false;
    const prototype = Object.getPrototypeOf(value);
    return prototype === Object.prototype || prototype === null;
  }

  function safePayloadDetail(payload, fallback) {
    return String(
      payload?.detail ||
        payload?.message ||
        payload?.error_description ||
        fallback ||
        "Request failed.",
    );
  }

  function defaultMutationContract() {
    return {
      csrfCookieName: DEFAULT_CSRF_COOKIE,
      csrfHeaderName: DEFAULT_CSRF_HEADER,
      csrfRequiredForCookieMutations: true,
      apiKeysRequireCsrf: false,
      protectedMethods: new Set(MUTATION_METHODS),
    };
  }

  function normalizedMutationContract(payload) {
    const root = payload && typeof payload === "object" ? payload : {};
    const auth = root.auth && typeof root.auth === "object" ? root.auth : root;
    const contract =
      auth.browser_mutation_contract &&
      typeof auth.browser_mutation_contract === "object"
        ? auth.browser_mutation_contract
        : root.browser_mutation_contract &&
            typeof root.browser_mutation_contract === "object"
          ? root.browser_mutation_contract
          : {};
    const protectedMethods =
      Array.isArray(contract.protected_methods) &&
      contract.protected_methods.length
        ? contract.protected_methods
        : Array.from(MUTATION_METHODS);
    return {
      csrfCookieName: String(
        contract.csrf_cookie_name ||
          auth.csrf_cookie_name ||
          root.csrf_cookie_name ||
          DEFAULT_CSRF_COOKIE,
      ),
      csrfHeaderName: String(
        contract.csrf_header_name ||
          auth.csrf_header_name ||
          root.csrf_header_name ||
          DEFAULT_CSRF_HEADER,
      ),
      csrfRequiredForCookieMutations:
        contract.csrf_required_for_cookie_mutations !== false &&
        auth.csrf_required_for_cookie_mutations !== false &&
        root.csrf_required_for_cookie_mutations !== false,
      apiKeysRequireCsrf: contract.api_keys_require_csrf === true,
      protectedMethods: new Set(
        protectedMethods
          .map((value) => String(value || "").toUpperCase())
          .filter(Boolean),
      ),
    };
  }

  async function loadMutationContract(force) {
    if (!force && mutationContractCache) return mutationContractCache;
    if (!force && mutationContractPromise) return mutationContractPromise;
    mutationContractPromise = (async () => {
      try {
        const response = await fetch(AUTH_STATUS_PATH, {
          method: "GET",
          headers: { Accept: "application/json" },
          credentials: "same-origin",
          cache: "no-store",
        });
        if (!response.ok) return defaultMutationContract();
        const payload = await response.json();
        return normalizedMutationContract(payload);
      } catch (_error) {
        return defaultMutationContract();
      }
    })();
    try {
      mutationContractCache = await mutationContractPromise;
      return mutationContractCache;
    } finally {
      mutationContractPromise = null;
    }
  }

  function requestUsesApiKey(headers) {
    const authorization = String(headers.get("Authorization") || "");
    return (
      /^Bearer\s+\S+/i.test(authorization) ||
      /^InkDrop-Key\s+\S+/i.test(authorization)
    );
  }

  function friendlyMessage(code, status, detail) {
    if (
      [
        "csrf_header_required",
        "csrf_cookie_required",
        "csrf_token_mismatch",
        "csrf_validation_failed",
      ].includes(code)
    ) {
      return "Your secure session needs to be refreshed. Sign in again, then retry this action.";
    }
    if (code === "session_expired" || status === 401)
      return "Your session expired. Sign in again, then retry this action.";
    if (code === "insufficient_scope")
      return "Your account does not have permission to perform this action.";
    if (
      [
        "invalid_origin",
        "origin_header_required",
        "origin_validation_failed",
      ].includes(code)
    ) {
      return "InkDrop blocked this request because it came from an untrusted origin.";
    }
    if (code === "rate_limited" || status === 429)
      return "Too many requests were made. Wait a moment, then retry.";
    if (status === 409)
      return (
        detail ||
        "InkDrop could not apply this change because the item changed. Refresh and retry."
      );
    if (status >= 500)
      return "InkDrop could not complete the request. Retry after the service recovers.";
    return detail || "InkDrop could not complete the request.";
  }

  class InkDropApiError extends Error {
    constructor(message, fields) {
      super(message);
      this.name = "InkDropApiError";
      Object.assign(this, fields || {});
    }
  }

  async function request(path, options) {
    const input = Object.assign({}, options || {});
    const method = String(input.method || "GET").toUpperCase();
    const headers = new Headers(input.headers || {});
    headers.set("Accept", "application/json");
    let body = input.body;
    if (isPlainObject(body)) {
      headers.set("Content-Type", "application/json");
      body = JSON.stringify(body);
    }
    if (MUTATION_METHODS.has(method)) {
      const contract = await loadMutationContract(
        Boolean(input.refreshAuthContract),
      );
      const protectedMethods = contract.protectedMethods || MUTATION_METHODS;
      const requiresCsrf =
        input.csrf !== false &&
        protectedMethods.has(method) &&
        contract.csrfRequiredForCookieMutations !== false &&
        (!requestUsesApiKey(headers) || contract.apiKeysRequireCsrf === true);
      if (requiresCsrf) {
        const csrf = cookieValue(
          input.csrfCookieName ||
            contract.csrfCookieName ||
            DEFAULT_CSRF_COOKIE,
        );
        if (csrf)
          headers.set(
            input.csrfHeaderName ||
              contract.csrfHeaderName ||
              DEFAULT_CSRF_HEADER,
            csrf,
          );
      }
    }
    let response;
    try {
      response = await fetch(
        path,
        Object.assign({}, input, {
          method,
          body: ["GET", "HEAD"].includes(method) ? undefined : body,
          headers,
          credentials: "same-origin",
          cache: input.cache || "no-store",
        }),
      );
    } catch (cause) {
      if (cause?.name === "AbortError") throw cause;
      throw new InkDropApiError(
        "InkDrop is unavailable. Check the connection, then retry.",
        {
          status: 0,
          code: "network_unavailable",
          detail: "Network request failed.",
          cause,
        },
      );
    }
    const requestId =
      response.headers.get("X-Request-ID") ||
      response.headers.get("X-InkDrop-Request-ID") ||
      "";
    const contentType = response.headers.get("Content-Type") || "";
    let payload = {};
    try {
      payload = contentType.includes("json")
        ? await response.json()
        : { detail: await response.text() };
    } catch (_error) {
      payload = {};
    }
    if (!response.ok || payload?.ok === false) {
      const code = String(
        payload?.code || payload?.error || `http_${response.status}`,
      );
      const detail = safePayloadDetail(payload, response.statusText);
      const error = new InkDropApiError(
        friendlyMessage(code, response.status, detail),
        {
          status: response.status,
          code,
          detail,
          retry_after: Number(
            payload?.retry_after || response.headers.get("Retry-After") || 0,
          ),
          fields: payload?.fields || payload?.validation_fields || null,
          request_id: payload?.request_id || requestId,
        },
      );
      error.retryAfter = error.retry_after;
      if (response.status === 401)
        window.dispatchEvent(
          new CustomEvent("inkdrop:session-expired", {
            detail: { path: location.hash || location.pathname },
          }),
        );
      throw error;
    }
    return payload;
  }

  window.InkDropApi = {
    request,
    Error: InkDropApiError,
    friendlyMessage,
    refreshAuthContract: () => loadMutationContract(true),
  };
})();
