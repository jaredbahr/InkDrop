#!/usr/bin/env python3
"""Machine-readable authentication contract for InkDrop mutation routes."""

from __future__ import annotations


COOKIE_MUTATION_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _record(route, method, action, scope, *, public=False, admin=False, destructive=False, high_impact=False, idempotent=False):
    return {
        "route": route,
        "method": method,
        "action": action,
        "authentication_methods": ["public"] if public else ["session", "api_key", "external"],
        "required_api_key_scope": None if public else scope,
        "scope": scope,
        "csrf_required_for_cookie_sessions": not public and method in COOKIE_MUTATION_METHODS,
        "admin_only": bool(admin),
        "destructive": bool(destructive),
        "high_impact": bool(high_impact),
        "idempotent": bool(idempotent),
    }


MUTATION_ROUTE_INVENTORY = {}


def _add(method, routes, action, scope, **flags):
    for route in routes:
        MUTATION_ROUTE_INVENTORY[(method, route)] = _record(route, method, action, scope, **flags)


_add("POST", {
    "/api/auth/bootstrap", "/api/inkdrop-auth/bootstrap",
    "/api/auth/login", "/api/inkdrop-auth/login",
    "/api/auth/recovery/reset", "/api/inkdrop-auth/recovery/reset",
}, "authentication_public", None, public=True, high_impact=True)

_add("POST", {
    "/api/auth/logout", "/api/inkdrop-auth/logout",
    "/api/auth/password", "/api/auth/sessions/revoke",
    "/api/auth/api-keys", "/api/inkdrop-auth/api-keys/create",
    "/api/auth/api-keys/revoke", "/api/inkdrop-auth/api-keys/revoke",
    "/api/auth/recovery/token", "/api/inkdrop-auth/recovery/token",
}, "authentication_administration", "admin", admin=True, high_impact=True)
_add("POST", {
    "/api/auth/maintenance/cleanup", "/api/inkdrop-auth/maintenance/cleanup",
}, "auth_state_cleanup", "maintenance", idempotent=True)

_add("POST", {
    "/api/inkdrop-settings/sync", "/api/inkdrop-settings/app/update",
    "/api/inkdrop-settings/provider/add", "/api/inkdrop-settings/provider/claim",
    "/api/inkdrop-settings/provider/update", "/api/inkdrop-settings/provider/test",
    "/api/inkdrop-settings/provider/recommendation/apply",
    "/api/notifications/settings/save",
    "/api/notifications/connector/create", "/api/notifications/connector/update",
    "/api/notifications/connector/test",
}, "settings_update", "settings")
_add("POST", {"/api/notifications/connector/delete"}, "settings_delete", "settings", destructive=True)
_add("POST", {
    "/api/download-clients", "/api/download-clients/test", "/api/download-clients/test-all",
}, "download_client_administration", "admin", admin=True, high_impact=True)
_add("POST", {"/api/inkdrop-settings/provider/delete"}, "settings_delete", "admin", admin=True, destructive=True, high_impact=True)
_add("POST", {
    "/api/inkdrop-settings/backup/export",
    "/api/inkdrop-settings/backup/preview",
    "/api/inkdrop-settings/portability/export",
    "/api/system/logs/download",
}, "settings_backup", "admin", admin=True, high_impact=True)
_add("GET", {
    "/api/inkdrop-settings/backup/archives",
    "/api/inkdrop-settings/backup/archives/download",
}, "full_backup_read", "admin", admin=True, high_impact=True)
_add("HEAD", {
    "/api/inkdrop-settings/backup/archives/download",
}, "full_backup_read", "admin", admin=True, high_impact=True)

_add("POST", {
    "/api/system/support-bundle/download",
}, "support_bundle_export", "admin", admin=True, high_impact=True)
_add("POST", {
    "/api/inkdrop-settings/backup/restore",
}, "settings_restore", "admin", admin=True, destructive=True, high_impact=True)
_add("POST", {
    "/api/inkdrop-settings/backup/archives/create",
}, "full_backup_create", "admin", admin=True, high_impact=True)
_add("POST", {
    "/api/inkdrop-settings/backup/archives/delete",
}, "full_backup_delete", "admin", admin=True, destructive=True, high_impact=True)
_add("POST", {
    "/api/inkdrop-settings/backup/archives/upload",
}, "full_backup_upload", "admin", admin=True, high_impact=True)
_add("POST", {
    "/api/inkdrop-settings/backup/archives/restore/preview",
}, "full_backup_restore_preview", "admin", admin=True, high_impact=True, idempotent=True)
_add("POST", {
    "/api/inkdrop-settings/backup/archives/restore/apply",
}, "full_backup_restore_apply", "admin", admin=True, destructive=True, high_impact=True)
_add("POST", {
    "/api/inkdrop-settings/backup/archives/merge/preview",
}, "full_backup_merge_preview", "admin", admin=True, high_impact=True, idempotent=True)
_add("POST", {
    "/api/library-adoption/plan",
    "/api/library-adoption/plan/start",
    "/api/library-adoption/plan/status",
    "/api/library-adoption/plan/latest",
}, "library_adoption_plan", "admin", admin=True, high_impact=True)
_add("POST", {
    "/api/library-adoption/apply",
}, "library_adoption_apply", "admin", admin=True, high_impact=True)
_add("POST", {
    "/api/inkdrop-library/convert-archives/plan",
    "/api/inkdrop-library/convert-archives/status",
    "/api/inkdrop-library/convert-archives/latest",
}, "archive_conversion_plan", "admin", admin=True)
_add("POST", {
    "/api/inkdrop-library/convert-archives/apply",
}, "archive_conversion_apply", "admin", admin=True, high_impact=True)

for method in ("PATCH", "PUT"):
    _add(method, {
        "/api/inkdrop-settings/app", "/api/inkdrop-settings/app/update",
        "/api/inkdrop-settings/provider", "/api/inkdrop-settings/provider/update",
    }, "settings_update", "settings")

_add("POST", {
    "/api/search", "/api/mangadex/search", "/api/mangadex/feed", "/api/metron/search",
    "/api/comicvine/search", "/api/comicvine/scan", "/api/watch/scan",
    "/api/comicvine/add", "/api/mangadex/add", "/api/metron/add", "/api/watch/add", "/api/watch/add-result",
    "/api/comicvine/update", "/api/watch/update", "/api/comicvine/replace-metadata",
    "/api/comicvine/grab", "/api/watch/grab", "/api/grab", "/api/import",
    "/api/inkdrop-state/series/run", "/api/inkdrop-state/series/update", "/api/inkdrop-state/series/manga-unit-override/set",
    "/api/inkdrop-state/series/media-type/set", "/api/inkdrop-state/series/edition-indifferent-override/set",
    "/api/inkdrop-state/wanted/run", "/api/inkdrop-state/queue/run", "/api/inkdrop-state/queue/remove",
    "/api/inkdrop-state/queue/retry-new-source", "/api/inkdrop-state/queue/reopen-import",
    "/api/inkdrop-state/series/covers/refresh", "/api/inkdrop-state/library-frontends/sync",
    "/api/inkdrop-state/sync", "/api/manga-unit/set", "/api/issue-monitor/set",
    "/api/missing/process", "/api/missing/recheck", "/api/missing/fresh", "/api/missing/hot",
    "/api/rss/discover", "/api/comicscodes/discover", "/api/slskd-source-probe/run",
    "/api/source-probe", "/api/series-autopilot/run", "/api/series-autopilot/normalize",
    "/api/manual-source/import-detected", "/api/manual-source/mark-waiting", "/api/manual-source/clear-waiting",
    "/api/sab-comic-failures/learn", "/api/unmatched-downloads/import",
    "/api/pack-review/refresh", "/api/pack-review/inspect", "/api/pack-review/import", "/api/pack-review/auto-import",
    "/api/manual-search/runs",
}, "acquisition", "acquisition")

_add("POST", {
    "/api/inkdrop-state/source-memory/allow",
    "/api/inkdrop-state/source-memory/remove",
    "/api/inkdrop-state/source-memory/block",
    # Both set a durable per-item override the acquisition passes then obey:
    # the hand-corrected search query, and "wanted, not pursued". Same
    # acquisition scope as the source-memory decisions above, and idempotent --
    # setting the same query or pausing an already-paused item is a no-op.
    "/api/inkdrop-state/reliability/search-query",
    "/api/inkdrop-state/reliability/pursuit",
}, "acquisition", "acquisition", idempotent=True)


def _manual_search_dynamic_policy(path, method):
    if method == "POST" and path.startswith("/api/manual-search/runs/") and path.endswith("/cancel"):
        return _record(path, method, "manual_search_cancel", "acquisition", idempotent=True)
    if method == "POST" and path.startswith("/api/manual-search/candidates/") and path.endswith("/grab"):
        return _record(path, method, "manual_search_grab", "acquisition")
    return None


def _download_client_dynamic_policy(path, method):
    base = "/api/download-clients"
    if path == base and method == "POST":
        return _record(path, method, "download_client_create", "admin", admin=True, high_impact=True)
    if path in {base + "/test", base + "/test-all"} and method == "POST":
        return _record(path, method, "download_client_test", "admin", admin=True, high_impact=True)
    if path.startswith(base + "/"):
        suffix = path.removeprefix(base + "/").strip("/")
        if method == "POST" and suffix.endswith("/test"):
            return _record(path, method, "download_client_test", "admin", admin=True, high_impact=True)
        if method == "PATCH" and suffix and "/" not in suffix:
            return _record(path, method, "download_client_update", "admin", admin=True, high_impact=True)
        if method == "DELETE" and suffix and "/" not in suffix:
            return _record(path, method, "download_client_delete", "admin", admin=True, destructive=True, high_impact=True)
    return None

_add("POST", {
    "/api/comicvine/delete", "/api/watch/delete", "/api/inkdrop-state/series/remove",
    "/api/inkdrop-state/series/library/migrate", "/api/inkdrop-state/import/mark-wrong",
    "/api/manual-review/approve", "/api/manual-review/approve-local-file", "/api/manual-review/approve-pack", "/api/manual-review/resolve-noop",
    "/api/manual-review/ignore", "/api/manual-review/bad-match", "/api/manual-review/add-alias",
    "/api/unmatched-downloads/quarantine", "/api/pack-review/clear",
    "/api/inkdrop-diagnostics/managed-library-duplicates/quarantine",
    "/api/inkdrop-diagnostics/managed_library_duplicates/quarantine",
    "/api/inkdrop-diagnostics/manga-chapter-artifacts/apply",
    "/api/inkdrop-diagnostics/manga_chapter_artifacts/apply",
    "/api/inkdrop-diagnostics/mixed-manga-units/apply",
    "/api/inkdrop-diagnostics/mixed_manga_units/apply",
    "/api/inkdrop-diagnostics/reader-frontend-orphan-cleanup/apply",
    "/api/inkdrop-diagnostics/reader-frontend-orphans/apply",
    "/api/inkdrop-diagnostics/reader_frontend_orphan_cleanup/apply",
    "/api/inkdrop-diagnostics/reader_frontend_orphans/apply",
    "/api/inkdrop-state/adapter-merge/apply", "/api/inkdrop-state/adapter_merge/apply",
    "/api/inkdrop-state/adapter-retire/apply", "/api/inkdrop-state/adapter_retire/apply",
    "/api/inkdrop-state/adapter-shadow-drain/apply", "/api/inkdrop-state/adapter_shadow_drain/apply",
    "/api/inkdrop-state/duplicate-series-ref-merge/apply", "/api/inkdrop-state/duplicate_series_ref_merge/apply",
    "/api/inkdrop-state/duplicate-series-retire/apply", "/api/inkdrop-state/duplicate_series_retire/apply",
    "/api/inkdrop-state/kapowarr-adapter-drain/apply", "/api/inkdrop-state/kapowarr_adapter_drain/apply",
    "/api/inkdrop-state/readiness-repair/apply", "/api/inkdrop-state/readiness_repair/apply",
    "/api/inkdrop-state/series-shadow-ref-merge/apply", "/api/inkdrop-state/series_shadow_ref_merge/apply",
    "/api/inkdrop-state/series-shadow-retire/apply", "/api/inkdrop-state/series_shadow_retire/apply",
    "/api/inkdrop-state/source-attempts/clear",
    "/api/inkdrop-state/series-merge/apply", "/api/inkdrop-state/series_merge/apply",
}, "destructive_maintenance", "admin", admin=True, destructive=True, high_impact=True)

# Gives a locked library folder to another work on an operator's ruling. It refuses a
# monitored owner, so it only ever moves a stale claim; admin because it decides which
# series every later import of that folder belongs to.
_add("POST", {
    "/api/inkdrop-state/series/folder-identity/transfer",
}, "series_folder_identity_transfer", "admin", admin=True, high_impact=True)

# Retires deferred queue-sync snapshots, but only after replaying the writes
# they still hold (see inkdrop_deferred_sync.reclaim_expired_replays), and a
# snapshot whose replay fails is left in place. Nothing is discarded on a
# failure path, so this is admin maintenance rather than a destructive apply.
_add("POST", {
    "/api/inkdrop-maintenance/deferred-queue-sync/reconcile",
}, "deferred_queue_sync_reconcile", "admin", admin=True, idempotent=True)

_add("POST", {
    "/api/inkdrop-state/duplicate-series-ref-merge/preview", "/api/inkdrop-state/duplicate_series_ref_merge/preview",
    "/api/inkdrop-state/series-shadow-ref-merge/preview", "/api/inkdrop-state/series_shadow_ref_merge/preview",
    "/api/inkdrop-state/series-merge/candidate", "/api/inkdrop-state/series_merge/candidate",
}, "maintenance_preview", "maintenance", idempotent=True)

_add("POST", {
    "/api/inkdrop-diagnostics/managed-library-audit/run",
    "/api/inkdrop-diagnostics/managed_library_audit/run",
    "/api/inkdrop-diagnostics/library-reconciliation/run",
    "/api/inkdrop-diagnostics/library_reconciliation/run",
}, "advanced_diagnostics_scan", "admin", admin=True, high_impact=True, idempotent=True)

_add("POST", {
    "/api/inkdrop-diagnostics/library-reconciliation/repair",
    "/api/inkdrop-diagnostics/library_reconciliation/repair",
}, "library_reconciliation_repair", "admin", admin=True, high_impact=True, idempotent=True)

_add("DELETE", {
    "/api/auth/api-keys/revoke", "/api/inkdrop-auth/api-keys/revoke",
}, "api_key_revoke", "admin", admin=True, destructive=True, high_impact=True)


def mutation_route_policy(path, method):
    method = str(method or "GET").upper()
    path = str(path or "")
    exact = MUTATION_ROUTE_INVENTORY.get((method, path))
    if exact:
        return dict(exact)
    dynamic = _manual_search_dynamic_policy(path, method)
    if dynamic:
        return dynamic
    dynamic = _download_client_dynamic_policy(path, method)
    if dynamic:
        return dynamic
    if method in {"POST", "DELETE"} and (
        path.startswith("/api/auth/api-keys/") or path.startswith("/api/inkdrop-auth/api-keys/")
    ):
        return _record(path, method, "api_key_revoke", "admin", admin=True, destructive=True, high_impact=True)
    return None


def public_inventory():
    # This endpoint inventories cookie-mutating routes for the CSRF contract.
    # Read contracts share the runtime table above but are intentionally not
    # mutations and therefore do not belong in this projection.
    return [
        dict(MUTATION_ROUTE_INVENTORY[key])
        for key in sorted(MUTATION_ROUTE_INVENTORY)
        if key[0] in COOKIE_MUTATION_METHODS
    ]
