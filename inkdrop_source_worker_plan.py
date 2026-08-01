"""Read-only worker planning for settings-backed InkDrop source integrations."""

from __future__ import annotations

import inkdrop_source_registry as registry
import inkdrop_sources


CONTRACT_VERSION = 1


BASE_CONTRACT = {
    "adapter_family": "unmapped_source",
    "adapter_id": "unmapped_source",
    "search_operation": "record_source_attempt_only",
    "candidate_parser": "",
    "verdict_helper": "",
    "attempt_seed_helper": "source_search_attempt_seed",
    "source_attempt_helper": "source_search_attempt_seed",
    "handoff_kind": "none",
    "can_emit_download_task": False,
    "manual_review_only": True,
    "evidence_only": True,
}


CONTRACTS_BY_PROVIDER_ID = {
    "standard_ebooks": {
        "adapter_family": "direct_catalog",
        "adapter_id": "standard_ebooks_opds",
        "search_operation": "search_opds_feed",
        "candidate_parser": "standard_ebooks_candidates_from_opds",
        "verdict_helper": "direct_artifact_verdict",
        "attempt_seed_helper": "direct_candidate_attempt_seed",
        "handoff_kind": "inkdrop_direct",
        "can_emit_download_task": True,
        "manual_review_only": False,
        "evidence_only": False,
    },
    "gutendex": {
        "adapter_family": "direct_catalog",
        "adapter_id": "gutendex_json_api",
        "search_operation": "search_json_api",
        "candidate_parser": "gutendex_candidates_from_payload",
        "verdict_helper": "direct_artifact_verdict",
        "attempt_seed_helper": "direct_candidate_attempt_seed",
        "handoff_kind": "inkdrop_direct",
        "can_emit_download_task": True,
        "manual_review_only": False,
        "evidence_only": False,
    },
    "internet_archive": {
        "adapter_family": "direct_archive_api",
        "adapter_id": "internet_archive_metadata",
        "search_operation": "search_archive_metadata_then_files",
        "candidate_parser": "internet_archive_candidates_from_metadata",
        "verdict_helper": "direct_artifact_verdict",
        "attempt_seed_helper": "direct_candidate_attempt_seed",
        "handoff_kind": "inkdrop_direct",
        "can_emit_download_task": True,
        "manual_review_only": False,
        "evidence_only": False,
    },
    "mangadex": {
        "adapter_family": "mangadex_api",
        "adapter_id": "mangadex_api_discovery",
        "search_operation": "search_manga_then_chapter_feed",
        "candidate_parser": "mangadex_candidates_from_payload",
        "verdict_helper": "mangadex_candidate_verdict",
        "attempt_seed_helper": "mangadex_candidate_attempt_seed",
        "handoff_kind": "none",
        "can_emit_download_task": False,
        "manual_review_only": True,
        "evidence_only": True,
    },
    "suwayomi": {
        "adapter_family": "suwayomi_api",
        "adapter_id": "suwayomi_api_discovery",
        "search_operation": "search_suwayomi_sources_then_chapters",
        "candidate_parser": "suwayomi_candidates_from_payload",
        "verdict_helper": "suwayomi_candidate_verdict",
        "attempt_seed_helper": "suwayomi_candidate_attempt_seed",
        "handoff_kind": "inkdrop_page_pack",
        "can_emit_download_task": True,
        "manual_review_only": False,
        "evidence_only": False,
    },
    "comicscodes": {
        "adapter_family": "feed_or_list_source",
        "adapter_id": "comicscodes_feed_list_assist",
        "search_operation": "poll_feed_and_list_pages_for_candidates",
        "candidate_parser": "comicscodes_candidates_from_payload",
        "verdict_helper": "manual_source_card_verdict",
        "attempt_seed_helper": "manual_source_card_attempt_seed",
        "handoff_kind": "none",
        "can_emit_download_task": False,
        "manual_review_only": True,
        "evidence_only": True,
    },
    "prowlarr": {
        "adapter_family": "prowlarr_indexer",
        "adapter_id": "prowlarr_native_indexer",
        "search_operation": "search_prowlarr_indexer",
        "candidate_parser": "prowlarr_candidates_from_results",
        "verdict_helper": "indexer_candidate_verdict",
        "attempt_seed_helper": "indexer_candidate_attempt_seed",
        "handoff_kind": "download_client",
        "can_emit_download_task": True,
        "manual_review_only": False,
        "evidence_only": False,
    },
    "rss": {
        "adapter_family": "rss_detail_direct_feed",
        "adapter_id": "rss_native_detail_direct_feed",
        "search_operation": "poll_feed_then_fetch_matching_detail_pages_for_direct_file_links",
        "candidate_parser": "direct_file_detail_candidates_from_payload",
        "verdict_helper": "direct_artifact_verdict",
        "attempt_seed_helper": "direct_candidate_attempt_seed",
        "handoff_kind": "inkdrop_direct",
        "can_emit_download_task": True,
        "manual_review_only": False,
        "evidence_only": False,
    },
    "prowlarr_internet_archive": {
        "adapter_family": "prowlarr_indexer",
        "adapter_id": "prowlarr_indexer_policy",
        "search_operation": "search_prowlarr_indexer",
        "candidate_parser": "prowlarr_candidates_from_results",
        "verdict_helper": "indexer_candidate_verdict",
        "attempt_seed_helper": "indexer_candidate_attempt_seed",
        "handoff_kind": "download_client",
        "can_emit_download_task": True,
        "manual_review_only": False,
        "evidence_only": False,
    },
    "prowlarr_nyaa": {
        "adapter_family": "prowlarr_indexer",
        "adapter_id": "prowlarr_indexer_policy",
        "search_operation": "search_prowlarr_indexer",
        "candidate_parser": "prowlarr_candidates_from_results",
        "verdict_helper": "indexer_candidate_verdict",
        "attempt_seed_helper": "indexer_candidate_attempt_seed",
        "handoff_kind": "download_client",
        "can_emit_download_task": True,
        "manual_review_only": False,
        "evidence_only": False,
    },
    "prowlarr_bitmagnet": {
        "adapter_family": "indexer_discovery",
        "adapter_id": "prowlarr_bitmagnet_discovery",
        "search_operation": "search_prowlarr_indexer_for_discovery",
        "candidate_parser": "indexer_discovery_cards_from_results",
        "verdict_helper": "manual_source_card_verdict",
        "attempt_seed_helper": "manual_source_card_attempt_seed",
        "handoff_kind": "none",
        "can_emit_download_task": False,
        "manual_review_only": True,
        "evidence_only": True,
    },
    "prowlarr_academic_torrents": {
        "adapter_family": "indexer_discovery",
        "adapter_id": "prowlarr_academic_torrents_discovery",
        "search_operation": "search_prowlarr_indexer_for_discovery",
        "candidate_parser": "indexer_discovery_cards_from_results",
        "verdict_helper": "manual_source_card_verdict",
        "attempt_seed_helper": "manual_source_card_attempt_seed",
        "handoff_kind": "none",
        "can_emit_download_task": False,
        "manual_review_only": True,
        "evidence_only": True,
    },
}


CONTRACTS_BY_SOURCE_KIND = {
    "archive_item_api": CONTRACTS_BY_PROVIDER_ID["internet_archive"],
    "direct_download_site": {
        "adapter_family": "direct_resolver",
        "adapter_id": "provider_specific_direct_resolver",
        "search_operation": "search_or_probe_provider_resolver",
        "candidate_parser": "provider_specific_direct_candidates",
        "verdict_helper": "direct_artifact_verdict",
        "attempt_seed_helper": "direct_candidate_attempt_seed",
        "handoff_kind": "inkdrop_direct",
        "can_emit_download_task": True,
        "manual_review_only": False,
        "evidence_only": False,
    },
    "direct_file_html_search": {
        "adapter_family": "direct_file_html_search",
        "adapter_id": "generic_direct_file_search",
        "search_operation": "fetch_configured_pages_for_direct_file_links",
        "candidate_parser": "direct_file_html_candidates_from_payload",
        "verdict_helper": "direct_artifact_verdict",
        "attempt_seed_helper": "direct_candidate_attempt_seed",
        "handoff_kind": "inkdrop_direct",
        "can_emit_download_task": True,
        "manual_review_only": False,
        "evidence_only": False,
    },
    "direct_file_detail_search": {
        "adapter_family": "direct_file_detail_search",
        "adapter_id": "generic_direct_file_detail_search",
        "search_operation": "fetch_search_pages_then_detail_pages_for_direct_file_links",
        "candidate_parser": "direct_file_detail_candidates_from_payload",
        "verdict_helper": "direct_artifact_verdict",
        "attempt_seed_helper": "direct_candidate_attempt_seed",
        "handoff_kind": "inkdrop_direct",
        "can_emit_download_task": True,
        "manual_review_only": False,
        "evidence_only": False,
    },
    "direct_file_probe_source": {
        "adapter_family": "direct_file_probe_source",
        "adapter_id": "generic_direct_file_probe_source",
        "search_operation": "fetch_search_pages_then_probe_candidate_file_headers",
        "candidate_parser": "direct_file_probe_candidates_from_payload",
        "verdict_helper": "direct_artifact_verdict",
        "attempt_seed_helper": "direct_candidate_attempt_seed",
        "handoff_kind": "inkdrop_direct",
        "can_emit_download_task": True,
        "manual_review_only": False,
        "evidence_only": False,
    },
    "rss_detail_probe_feed": {
        "adapter_family": "rss_detail_probe_feed",
        "adapter_id": "generic_rss_detail_probe_feed",
        "search_operation": "poll_feed_then_fetch_detail_pages_and_probe_candidate_file_headers",
        "candidate_parser": "direct_file_probe_candidates_from_payload",
        "verdict_helper": "direct_artifact_verdict",
        "attempt_seed_helper": "direct_candidate_attempt_seed",
        "handoff_kind": "inkdrop_direct",
        "can_emit_download_task": True,
        "manual_review_only": False,
        "evidence_only": False,
    },
    "reader_page_pack_source": {
        "adapter_family": "reader_page_pack_source",
        "adapter_id": "generic_reader_page_pack_source",
        "search_operation": "fetch_search_pages_then_reader_pages_for_image_sequences",
        "candidate_parser": "reader_page_pack_candidates_from_payload",
        "verdict_helper": "reader_page_pack_verdict",
        "attempt_seed_helper": "reader_page_pack_attempt_seed",
        "handoff_kind": "inkdrop_page_pack",
        "can_emit_download_task": True,
        "manual_review_only": False,
        "evidence_only": False,
    },
    "rss_reader_page_pack_feed": {
        "adapter_family": "rss_reader_page_pack_feed",
        "adapter_id": "generic_rss_reader_page_pack_feed",
        "search_operation": "poll_feed_then_fetch_reader_pages_for_image_sequences",
        "candidate_parser": "reader_page_pack_candidates_from_payload",
        "verdict_helper": "reader_page_pack_verdict",
        "attempt_seed_helper": "reader_page_pack_attempt_seed",
        "handoff_kind": "inkdrop_page_pack",
        "can_emit_download_task": True,
        "manual_review_only": False,
        "evidence_only": False,
    },
    "external_tool_bridge": {
        "adapter_family": "external_tool_bridge",
        "adapter_id": "external_tool_manual_bridge",
        "search_operation": "run_external_tool_search_or_dry_run",
        "candidate_parser": "external_tool_candidates_from_results",
        "verdict_helper": "external_tool_candidate_verdict",
        "attempt_seed_helper": "external_tool_candidate_attempt_seed",
        "handoff_kind": "staged_external_tool",
        "can_emit_download_task": True,
        "manual_review_only": False,
        "evidence_only": False,
    },
    "html_search_source": {
        "adapter_family": "html_search_source",
        "adapter_id": "generic_html_search_pages",
        "search_operation": "fetch_configured_search_pages",
        "candidate_parser": "html_search_candidates_from_payload",
        "verdict_helper": "manual_source_card_verdict",
        "attempt_seed_helper": "manual_source_card_attempt_seed",
        "handoff_kind": "none",
        "can_emit_download_task": False,
        "manual_review_only": True,
        "evidence_only": True,
    },
    "json_api_direct_catalog": CONTRACTS_BY_PROVIDER_ID["gutendex"],
    "json_direct_source": {
        "adapter_family": "json_direct_source",
        "adapter_id": "generic_json_direct_source",
        "search_operation": "fetch_configured_json_direct_source",
        "candidate_parser": "json_direct_candidates_from_payload",
        "verdict_helper": "direct_artifact_verdict",
        "attempt_seed_helper": "direct_candidate_attempt_seed",
        "handoff_kind": "inkdrop_direct",
        "can_emit_download_task": True,
        "manual_review_only": False,
        "evidence_only": False,
    },
    "local_dht_search": {
        "adapter_family": "indexer_discovery",
        "adapter_id": "local_dht_manual_policy",
        "search_operation": "search_local_dht_index",
        "candidate_parser": "indexer_discovery_cards_from_results",
        "verdict_helper": "manual_source_card_verdict",
        "attempt_seed_helper": "manual_source_card_attempt_seed",
        "handoff_kind": "none",
        "can_emit_download_task": False,
        "manual_review_only": True,
        "evidence_only": True,
    },
    "manual_bucket": {
        "adapter_family": "manual_source_cards",
        "adapter_id": "manual_source_card_bucket",
        "search_operation": "record_manual_source_cards",
        "candidate_parser": "manual_source_cards_from_results",
        "verdict_helper": "manual_source_card_verdict",
        "attempt_seed_helper": "manual_source_card_attempt_seed",
        "handoff_kind": "none",
        "can_emit_download_task": False,
        "manual_review_only": True,
        "evidence_only": True,
    },
    "manual_inbox_source": {
        "adapter_family": "local_manual_inbox",
        "adapter_id": "local_manual_inbox",
        "search_operation": "scan_bounded_local_manual_inbox_for_importable_archives",
        "candidate_parser": "local_manual_inbox_candidates_from_scan",
        "verdict_helper": "local_manual_inbox_verdict",
        "attempt_seed_helper": "local_manual_inbox_attempt_seed",
        "handoff_kind": "staged_local_file",
        "can_emit_download_task": True,
        "manual_review_only": False,
        "evidence_only": False,
    },
    "manga_api_page_provider": {
        "adapter_family": "manga_api",
        "adapter_id": "manga_api_assist",
        "search_operation": "search_manga_api_chapters",
        "candidate_parser": "mangadex_candidates_from_payload",
        "verdict_helper": "manual_source_card_verdict",
        "attempt_seed_helper": "manual_source_card_attempt_seed",
        "handoff_kind": "none",
        "can_emit_download_task": False,
        "manual_review_only": True,
        "evidence_only": True,
    },
    "suwayomi_api_page_provider": CONTRACTS_BY_PROVIDER_ID["suwayomi"],
    "opds_direct_catalog": CONTRACTS_BY_PROVIDER_ID["standard_ebooks"],
    "opds_acquisition_catalog": {
        "adapter_family": "opds_catalog",
        "adapter_id": "generic_opds_catalog",
        "search_operation": "fetch_opds_acquisition_catalog",
        "candidate_parser": "opds_catalog_candidates_from_payload",
        "verdict_helper": "direct_artifact_verdict",
        "attempt_seed_helper": "direct_candidate_attempt_seed",
        "handoff_kind": "inkdrop_direct",
        "can_emit_download_task": True,
        "manual_review_only": False,
        "evidence_only": False,
    },
    "newznab_indexer": {
        "adapter_family": "newznab_indexer",
        "adapter_id": "generic_newznab_indexer",
        "search_operation": "search_newznab_indexer",
        "candidate_parser": "newznab_candidates_from_payload",
        "verdict_helper": "indexer_candidate_verdict",
        "attempt_seed_helper": "indexer_candidate_attempt_seed",
        "handoff_kind": "download_client",
        "can_emit_download_task": True,
        "manual_review_only": False,
        "evidence_only": False,
    },
    "prowlarr_indexer": CONTRACTS_BY_PROVIDER_ID["prowlarr_nyaa"],
    "prowlarr_or_torrent_indexer": CONTRACTS_BY_PROVIDER_ID["prowlarr_nyaa"],
    "rss_direct_source": {
        "adapter_family": "rss_feed",
        "adapter_id": "rss_feed_assist",
        "search_operation": "poll_feed_for_candidates",
        "candidate_parser": "rss_feed_candidates_from_payload",
        "verdict_helper": "manual_source_card_verdict",
        "attempt_seed_helper": "manual_source_card_attempt_seed",
        "handoff_kind": "none",
        "can_emit_download_task": False,
        "manual_review_only": True,
        "evidence_only": True,
    },
    "rss_direct_feed": {
        "adapter_family": "rss_direct_feed",
        "adapter_id": "generic_rss_direct_feed",
        "search_operation": "poll_feed_for_direct_file_candidates",
        "candidate_parser": "direct_rss_candidates_from_payload",
        "verdict_helper": "direct_artifact_verdict",
        "attempt_seed_helper": "direct_candidate_attempt_seed",
        "handoff_kind": "inkdrop_direct",
        "can_emit_download_task": True,
        "manual_review_only": False,
        "evidence_only": False,
    },
    "rss_detail_direct_feed": {
        "adapter_family": "rss_detail_direct_feed",
        "adapter_id": "generic_rss_detail_direct_feed",
        "search_operation": "poll_feed_then_fetch_matching_detail_pages_for_direct_file_links",
        "candidate_parser": "direct_file_detail_candidates_from_payload",
        "verdict_helper": "direct_artifact_verdict",
        "attempt_seed_helper": "direct_candidate_attempt_seed",
        "handoff_kind": "inkdrop_direct",
        "can_emit_download_task": True,
        "manual_review_only": False,
        "evidence_only": False,
    },
    "torznab_indexer": {
        "adapter_family": "torznab_indexer",
        "adapter_id": "generic_torznab_indexer",
        "search_operation": "search_torznab_indexer",
        "candidate_parser": "torznab_candidates_from_payload",
        "verdict_helper": "indexer_candidate_verdict",
        "attempt_seed_helper": "indexer_candidate_attempt_seed",
        "handoff_kind": "download_client",
        "can_emit_download_task": True,
        "manual_review_only": False,
        "evidence_only": False,
    },
    "torrent_html_search": {
        "adapter_family": "torrent_html_search",
        "adapter_id": "generic_torrent_html_search",
        "search_operation": "fetch_configured_pages_for_torrent_or_magnet_links",
        "candidate_parser": "torrent_html_candidates_from_payload",
        "verdict_helper": "indexer_candidate_verdict",
        "attempt_seed_helper": "indexer_candidate_attempt_seed",
        "handoff_kind": "download_client",
        "can_emit_download_task": True,
        "manual_review_only": False,
        "evidence_only": False,
    },
    "torrent_detail_rss_feed": {
        "adapter_family": "torrent_detail_rss_feed",
        "adapter_id": "generic_torrent_detail_rss_feed",
        "search_operation": "poll_feed_then_fetch_matching_torrent_detail_pages",
        "candidate_parser": "torrent_detail_candidates_from_payload",
        "verdict_helper": "indexer_candidate_verdict",
        "attempt_seed_helper": "indexer_candidate_attempt_seed",
        "handoff_kind": "download_client",
        "can_emit_download_task": True,
        "manual_review_only": False,
        "evidence_only": False,
    },
    "torrent_detail_search": {
        "adapter_family": "torrent_detail_search",
        "adapter_id": "generic_torrent_detail_search",
        "search_operation": "fetch_search_pages_then_torrent_detail_pages",
        "candidate_parser": "torrent_detail_candidates_from_payload",
        "verdict_helper": "indexer_candidate_verdict",
        "attempt_seed_helper": "indexer_candidate_attempt_seed",
        "handoff_kind": "download_client",
        "can_emit_download_task": True,
        "manual_review_only": False,
        "evidence_only": False,
    },
    "torrent_rss_feed": {
        "adapter_family": "torrent_rss_feed",
        "adapter_id": "generic_torrent_rss_feed",
        "search_operation": "poll_torrent_rss_feed",
        "candidate_parser": "torrent_rss_candidates_from_payload",
        "verdict_helper": "indexer_candidate_verdict",
        "attempt_seed_helper": "indexer_candidate_attempt_seed",
        "handoff_kind": "download_client",
        "can_emit_download_task": True,
        "manual_review_only": False,
        "evidence_only": False,
    },
}


CONTRACTS_BY_INTEGRATION_CLASS = {
    "ArchiveItemProvider": CONTRACTS_BY_PROVIDER_ID["internet_archive"],
    "DirectFileProvider": CONTRACTS_BY_SOURCE_KIND["direct_download_site"],
    "ExternalToolProvider": CONTRACTS_BY_SOURCE_KIND["external_tool_bridge"],
    "ManualSourceBucket": CONTRACTS_BY_SOURCE_KIND["manual_bucket"],
    "OPDSProvider": CONTRACTS_BY_PROVIDER_ID["standard_ebooks"],
    "ProwlarrIndexerPolicy": CONTRACTS_BY_PROVIDER_ID["prowlarr_nyaa"],
    "DirectFileProbeProvider": CONTRACTS_BY_SOURCE_KIND["direct_file_probe_source"],
    "ReaderPagePackProvider": CONTRACTS_BY_SOURCE_KIND["reader_page_pack_source"],
    "NewznabIndexerPolicy": CONTRACTS_BY_SOURCE_KIND["newznab_indexer"],
    "TorznabIndexerPolicy": CONTRACTS_BY_SOURCE_KIND["torznab_indexer"],
    "TorrentDetailSearchPolicy": CONTRACTS_BY_SOURCE_KIND["torrent_detail_search"],
    "TorrentHtmlSearchPolicy": CONTRACTS_BY_SOURCE_KIND["torrent_html_search"],
    "TorrentRssFeedPolicy": CONTRACTS_BY_SOURCE_KIND["torrent_rss_feed"],
}


def _merge_contract(*contracts):
    out = dict(BASE_CONTRACT)
    for contract in contracts:
        if isinstance(contract, dict):
            out.update(contract)
    return out


def adapter_contract_for_row(row):
    row = row if isinstance(row, dict) else {}
    provider_id = inkdrop_sources.provider_key(row.get("provider_id"))
    source_kind = str(row.get("source_kind") or "").strip()
    integration_class = str(row.get("integration_class") or "").strip()
    contract = _merge_contract(
        CONTRACTS_BY_INTEGRATION_CLASS.get(integration_class),
        CONTRACTS_BY_SOURCE_KIND.get(source_kind),
        CONTRACTS_BY_PROVIDER_ID.get(provider_id),
    )
    if provider_id == "mangadex":
        policy = row.get("policy") if isinstance(row.get("policy"), dict) else {}
        at_home_enabled = any(
            policy.get(key) is True
            or str(policy.get(key) or "").strip().lower() in {"1", "true", "yes", "on"}
            for key in ("fetch_at_home_pages", "allow_at_home_page_pack", "enable_at_home_page_pack")
        )
        if at_home_enabled:
            contract["handoff_kind"] = "inkdrop_page_pack"
            contract["can_emit_download_task"] = True
            contract["manual_review_only"] = False
            contract["evidence_only"] = False
    return contract


def _schedule_state(row, contract, can_search, emits_download_task, requires_operator):
    if bool((row or {}).get("provider_health_problem")):
        return "provider_wait"
    registry_state = str((row or {}).get("registry_state") or "").strip().lower()
    if not can_search:
        return "blocked"
    if registry_state == "metadata_only":
        return "metadata_only"
    if emits_download_task:
        return "auto_download"
    if registry_state == "assist":
        return "assist_review"
    if requires_operator or registry_state == "manual_review":
        return "manual_review"
    if str(contract.get("handoff_kind") or "") == "none":
        return "evidence_only"
    return "search_only"


def _schedule_reason(row, contract, schedule_state, emits_download_task, requires_operator):
    row = row if isinstance(row, dict) else {}
    if schedule_state == "provider_wait":
        health = row.get("provider_health") if isinstance(row.get("provider_health"), dict) else {}
        label = str(health.get("label") or health.get("state") or "provider health").strip()
        detail = str(health.get("detail") or health.get("message") or "").strip()
        provider_id = inkdrop_sources.provider_key(health.get("provider_id") or row.get("provider_id"))
        bits = [inkdrop_sources.provider_label(provider_id), label]
        if detail:
            bits.append(detail)
        return ": ".join(bit for bit in bits if bit)
    if schedule_state == "blocked":
        reasons = row.get("block_reasons") or []
        return str(reasons[0]) if reasons else "source_not_schedulable"
    if schedule_state == "auto_download":
        return f"{contract.get('adapter_family')} can emit {contract.get('handoff_kind')} handoff after verdict"
    if schedule_state == "assist_review":
        return "assist source records candidates for operator action"
    if schedule_state == "manual_review":
        return "manual review required before any acquisition action"
    if requires_operator:
        return "operator action required"
    return "search evidence only"


def source_worker_plan_for_row(row):
    row = row if isinstance(row, dict) else {}
    contract = adapter_contract_for_row(row)
    provider_type = str(row.get("provider_type") or "").strip().lower()
    download_capable_type = registry.is_download_capable_provider_type(provider_type)
    health_problem = bool(row.get("provider_health_problem"))
    can_search = bool(row.get("auto_search_allowed") and not health_problem)
    supports_auto_download = bool(contract.get("can_emit_download_task") and download_capable_type)
    emits_download_task = bool(can_search and row.get("auto_download_allowed") and supports_auto_download)
    requires_operator = bool(
        contract.get("manual_review_only")
        or row.get("requires_manual_review")
        or row.get("manual_review_allowed")
        or str(row.get("registry_state") or "").strip().lower() in {"assist", "manual_review"}
    )
    schedule_state = _schedule_state(row, contract, can_search, emits_download_task, requires_operator)
    plan = {
        "worker_plan_contract_version": CONTRACT_VERSION,
        "provider_id": row.get("provider_id"),
        "display_name": row.get("display_name"),
        "provider_type": provider_type,
        "source_kind": row.get("source_kind"),
        "integration_class": row.get("integration_class"),
        "source_mode": row.get("source_mode"),
        "registry_state": row.get("registry_state"),
        "priority": row.get("priority"),
        "adapter_family": contract.get("adapter_family"),
        "adapter_id": contract.get("adapter_id"),
        "search_operation": contract.get("search_operation"),
        "candidate_parser": contract.get("candidate_parser"),
        "verdict_helper": contract.get("verdict_helper"),
        "attempt_seed_helper": contract.get("attempt_seed_helper"),
        "source_attempt_helper": contract.get("source_attempt_helper"),
        "handoff_kind": contract.get("handoff_kind"),
        "can_search": can_search,
        "provider_health_problem": health_problem,
        "provider_health": row.get("provider_health") if isinstance(row.get("provider_health"), dict) else {},
        "health_provider_ids": list(row.get("health_provider_ids") or []),
        "supports_auto_download": supports_auto_download,
        "can_auto_download": bool(row.get("auto_download_allowed") and supports_auto_download),
        "emits_download_task": emits_download_task,
        "requires_operator": requires_operator,
        "evidence_only": bool(contract.get("evidence_only") or not supports_auto_download),
        "manual_review_only": bool(contract.get("manual_review_only")),
        "schedule_state": schedule_state,
        "schedule_reason": _schedule_reason(row, contract, schedule_state, emits_download_task, requires_operator),
        "block_reasons": list(row.get("block_reasons") or []),
    }
    return {key: value for key, value in plan.items() if value not in (None, "", [], {})}


def source_worker_plan_from_registry(rows, *, include_blocked=False):
    plans = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        plan = source_worker_plan_for_row(row)
        if not include_blocked and plan.get("schedule_state") == "blocked":
            continue
        plans.append(plan)
    return sorted(plans, key=lambda plan: (plan.get("priority", 100), str(plan.get("display_name") or "").lower(), plan.get("provider_id") or ""))


def source_worker_plan_from_settings_snapshot(snapshot, *, include_blocked=False):
    rows = registry.registry_from_settings_snapshot(snapshot, include_disabled=True)
    return source_worker_plan_from_registry(rows, include_blocked=include_blocked)


def worker_plan_summary(plans):
    plans = list(plans or [])
    by_state = {}
    by_adapter = {}
    for plan in plans:
        state = str((plan or {}).get("schedule_state") or "unknown")
        adapter = str((plan or {}).get("adapter_family") or "unknown")
        by_state[state] = by_state.get(state, 0) + 1
        by_adapter[adapter] = by_adapter.get(adapter, 0) + 1
    return {
        "total": len(plans),
        "by_state": dict(sorted(by_state.items())),
        "by_adapter": dict(sorted(by_adapter.items())),
        "auto_download": sum(1 for plan in plans if plan.get("emits_download_task")),
        "manual_or_assist": sum(1 for plan in plans if plan.get("requires_operator")),
        "evidence_only": sum(1 for plan in plans if plan.get("evidence_only")),
    }
