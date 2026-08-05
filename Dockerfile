# Built once, natively, and reused for every target platform below -- the
# output is plain static JS/CSS with no architecture-specific code, so
# running this stage under arm64 QEMU emulation as well would only double
# build time for no benefit.
FROM --platform=$BUILDPLATFORM node:20-slim AS frontend-builder

WORKDIR /app/web/frontend

COPY web/frontend/package.json web/frontend/package-lock.json ./
RUN npm ci

COPY web/frontend/ ./
RUN npm run build

FROM python:3.12-slim

ARG INKDROP_VERSION=dev
ARG INKDROP_COMMIT_SHA=unknown
ARG INKDROP_BUILD_DATE=unknown
ARG INKDROP_RELEASE_CHANNEL=dev
ARG INKDROP_QA_BUILD_NUMBER=0

LABEL org.opencontainers.image.title="InkDrop" \
    org.opencontainers.image.description="InkDrop comics and manga acquisition automation" \
    org.opencontainers.image.source="https://github.com/jaredbahr/InkDrop" \
    org.opencontainers.image.version="${INKDROP_VERSION}" \
    org.opencontainers.image.revision="${INKDROP_COMMIT_SHA}" \
    org.opencontainers.image.created="${INKDROP_BUILD_DATE}" \
    io.inkdrop.release.channel="${INKDROP_RELEASE_CHANNEL}" \
    io.inkdrop.qa.build-number="${INKDROP_QA_BUILD_NUMBER}"

# Every default landing spot lives under one of the four mounts the install
# documentation actually tells people to create: ./config, Comics, Manga,
# and downloads. The old defaults put the database, accounts, backups, and
# quarantine under an unmounted /state, so recreating the documented
# container destroyed everything a tester had done.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    INKDROP_CONFIG_DIR=/config \
    INKDROP_STATE_DIR=/config/state \
    INKDROP_LOG_DIR=/config/state/logs \
    INKDROP_CACHE_DIR=/config/state/cache \
    INKDROP_BACKUP_DIR=/config/state/backups \
    INKDROP_STAGING_DIR=/downloads/staging \
    INKDROP_MANUAL_INBOX_DIR=/config/manual-inbox \
    INKDROP_QUARANTINE_DIR=/config/state/quarantine \
    INKDROP_COMIC_ROOT=/data/comics \
    INKDROP_MANGA_ROOT=/data/manga \
    INKDROP_HOST=0.0.0.0 \
    INKDROP_PORT=8796 \
    INKDROP_VERSION=${INKDROP_VERSION} \
    INKDROP_COMMIT_SHA=${INKDROP_COMMIT_SHA} \
    INKDROP_BUILD_DATE=${INKDROP_BUILD_DATE} \
    INKDROP_RELEASE_CHANNEL=${INKDROP_RELEASE_CHANNEL} \
    INKDROP_QA_BUILD_NUMBER=${INKDROP_QA_BUILD_NUMBER}

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        p7zip-full \
        unrar-free \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY inkdrop-logo-mark.png ./
COPY web/static/css/inkdrop.css ./web/static/css/inkdrop.css
COPY web/static/img/inkdrop-auth-backdrop.webp ./web/static/img/inkdrop-auth-backdrop.webp
COPY web/static/js/ ./web/static/js/
COPY --from=frontend-builder /app/web/static/dist ./web/static/dist
COPY \
    inkdrop_acquire_adapter.py \
    inkdrop_archive_conversion.py \
    inkdrop_acquire.py \
    inkdrop_acquisition_funnel.py \
    inkdrop_backup_restore.py \
    inkdrop_auth.py \
    inkdrop_auth_contracts.py \
    inkdrop_auth_cli.py \
    inkdrop_artifact_acceptance.py \
    inkdrop_comicscodes_discovery.py \
    inkdrop_completed_import.py \
    inkdrop_incident_recovery.py \
    inkdrop_container_healthcheck.py \
    inkdrop_internal_jobs.py \
    inkdrop_container_scheduler.py \
    inkdrop_container_start.py \
    inkdrop_direct_downloader.py \
    inkdrop_download_clients.py \
    inkdrop_download_client_config.py \
    inkdrop_download_client_api.py \
    inkdrop_download_client_routing.py \
    inkdrop_qbittorrent_auth.py \
    inkdrop_secret_store.py \
    inkdrop_db.py \
    inkdrop_client_status.py \
    inkdrop_effective_config.py \
    inkdrop_language.py \
    inkdrop_log_retention.py \
    inkdrop_library_adoption.py \
    inkdrop_library_frontends.py \
    inkdrop_library_identity.py \
    inkdrop_manga_companion.py \
    inkdrop_manga_metadata_guard.py \
    inkdrop_manual_source_autoresolve.py \
    inkdrop_mangadex_direct.py \
    inkdrop_manual_search.py \
    inkdrop_manual_search_core.py \
    inkdrop_manual_search_executor.py \
    inkdrop_manual_search_worker.py \
    inkdrop_missing_acquire.py \
    inkdrop_missing_recovery_policy.py \
    inkdrop_notifications.py \
    inkdrop_notification_events.py \
    inkdrop_notification_store.py \
    inkdrop_nfo_parser.py \
    inkdrop_pack_import.py \
    inkdrop_page_pack_downloader.py \
    inkdrop_operator_contracts.py \
    inkdrop_opds.py \
    inkdrop_folder_cleanup.py \
    inkdrop_log_export.py \
    inkdrop_support_bundle.py \
    inkdrop_portability_export.py \
    inkdrop_process_lifecycle.py \
    inkdrop_preflight.py \
    inkdrop_public_contracts.py \
    inkdrop_reconcile_imports.py \
    inkdrop_release_calendar.py \
    inkdrop_runtime_config.py \
    inkdrop_settings_registry.py \
    inkdrop_rss_discovery.py \
    inkdrop_sab_failed_cleanup.py \
    inkdrop_slskd_search_cleanup.py \
    sab_rescue_server.py \
    inkdrop_series_autopilot.py \
    inkdrop_service_inventory.py \
    inkdrop_slskd_source_probe.py \
    inkdrop_slskd_root_health.py \
    inkdrop_slskd_staging_sweep.py \
    inkdrop_candidate_matching.py \
    inkdrop_source_catalog.py \
    inkdrop_source_providers.py \
    inkdrop_source_registry.py \
    inkdrop_source_suppression.py \
    inkdrop_source_worker_adapters.py \
    inkdrop_source_worker_batch.py \
    inkdrop_source_worker_cli.py \
    inkdrop_source_worker_coordinator.py \
    inkdrop_source_worker_http.py \
    inkdrop_source_worker_jobs.py \
    inkdrop_source_worker_plan.py \
    inkdrop_source_worker_recorder.py \
    inkdrop_source_worker_runtime.py \
    inkdrop_source_worker_scheduler.py \
    inkdrop_source_worker_service.py \
    inkdrop_sources.py \
    inkdrop_activity.py \
    inkdrop_staged_projection.py \
    inkdrop_deferred_sync.py \
    inkdrop_credential_evidence_cleanup.py \
    inkdrop_issue_identity.py \
    inkdrop_state.py \
    inkdrop_state_maintenance.py \
    inkdrop_transfer.py \
    inkdrop_suwayomi_managed_folder.py \
    inkdrop_version.py \
    inkdrop_web_config.py \
    inkdrop_web_state_views.py \
    inkdrop_web.py \
    ./
COPY docs/inkdrop-source-candidate-catalog-20260702.json ./docs/inkdrop-source-candidate-catalog-20260702.json
COPY tools/inkdrop_install_support_summary.py ./tools/inkdrop_install_support_summary.py
COPY \
    scripts/inkdrop-completion-identity-audit-diff-alert.sh \
    scripts/inkdrop-completion-identity-audit-diff-check.sh \
    scripts/inkdrop-import-ready-worker.sh \
    scripts/inkdrop-series-autopilot-cron.sh \
    scripts/inkdrop-source-worker-mangadex-cron.sh \
    scripts/inkdrop-source-worker-suwayomi-cron.sh \
    scripts/inkdrop-source-worker.sh \
    scripts/inkdrop-state-path-contract-sync-regression-alert.sh \
    scripts/inkdrop-state-path-contract-sync-regression-check.sh \
    ./

RUN chmod +x /app/inkdrop-*.sh

EXPOSE 8796

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -B inkdrop_container_healthcheck.py --timeout 5

CMD ["python", "-B", "inkdrop_container_start.py"]
