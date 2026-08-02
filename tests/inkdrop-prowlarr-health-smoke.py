#!/usr/bin/env python3
import sys
import types

try:
    import requests  # noqa: F401
except Exception:
    sys.modules["requests"] = types.SimpleNamespace()

import inkdrop_web as web
import inkdrop_acquire_adapter


def fail(message):
    print(f"PROWLARR_HEALTH_FAIL: {message}")
    raise SystemExit(1)


def assert_equal(actual, expected, message):
    if actual != expected:
        fail(f"{message}: expected {expected!r}, got {actual!r}")


def assert_true(value, message):
    if not value:
        fail(message)


def main():
    assert_equal(web.ACQUIRE_SCRIPT.name, "inkdrop_missing_acquire.py", "worker acquire script remains the missing-acquire entrypoint")
    assert_equal(
        inkdrop_acquire_adapter.ACQUIRE_MODULE_SCRIPT.name,
        "inkdrop_acquire.py",
        "provider helper module uses the canonical InkDrop adapter",
    )
    acquire = web.load_acquire_module()
    for helper_name in ("load_prowlarr_settings", "load_qbit_settings", "load_sab_settings"):
        assert_true(hasattr(acquire, helper_name), f"provider helper module exposes {helper_name}")

    freeleech_only = web.prowlarr_torrentleech_coverage(
        [
            {
                "id": 44,
                "name": "TorrentLeech",
                "enable": True,
                "fields": [
                    {"name": "freeleech", "label": "Freeleech only", "value": True},
                ],
            },
            {
                "id": 12,
                "name": "DOGnzb",
                "enable": True,
                "fields": [],
            },
        ]
    )
    assert_equal(freeleech_only["torrentleech_enabled_indexer_count"], 1, "enabled TL count")
    assert_equal(freeleech_only["torrentleech_freeleech_indexer_count"], 1, "freeleech TL count")
    assert_equal(freeleech_only["torrentleech_freeleech_only"], True, "freeleech-only TL flag")
    assert_true("non-freeleech weekly packs" in freeleech_only["torrentleech_coverage_detail"], "freeleech detail names hidden weekly packs")

    mixed = web.prowlarr_torrentleech_coverage(
        [
            {
                "id": 44,
                "name": "TorrentLeech Freeleech",
                "enable": True,
                "fields": [{"name": "freeleech", "value": True}],
            },
            {
                "id": 45,
                "name": "TorrentLeech Comics All",
                "enable": True,
                "fields": [{"name": "freeleech", "value": False}],
            },
        ]
    )
    assert_equal(mixed["torrentleech_enabled_indexer_count"], 2, "mixed TL count")
    assert_equal(mixed["torrentleech_freeleech_indexer_count"], 1, "mixed freeleech TL count")
    assert_equal(mixed["torrentleech_freeleech_only"], False, "mixed TL is not globally freeleech-only")

    snapshot = web.source_health_provider_snapshot(
        {
            "prowlarr_status": "healthy",
            "prowlarr_api_state": "healthy",
            "prowlarr_torrentleech_enabled_indexer_count": freeleech_only["torrentleech_enabled_indexer_count"],
            "prowlarr_torrentleech_freeleech_indexer_count": freeleech_only["torrentleech_freeleech_indexer_count"],
            "prowlarr_torrentleech_freeleech_only": freeleech_only["torrentleech_freeleech_only"],
            "prowlarr_torrentleech_freeleech_indexer_ids": freeleech_only["torrentleech_freeleech_indexer_ids"],
            "prowlarr_torrentleech_coverage_detail": freeleech_only["torrentleech_coverage_detail"],
        }
    )
    prowlarr = snapshot.get("prowlarr") or {}
    assert_equal(prowlarr.get("state"), "healthy", "Prowlarr health remains healthy")
    assert_equal(prowlarr.get("torrentleech_freeleech_only"), True, "snapshot preserves TL coverage flag")
    assert_true("non-freeleech weekly packs" in prowlarr.get("torrentleech_coverage_detail", ""), "snapshot preserves coverage detail")

    print("PROWLARR_HEALTH_OK: TorrentLeech freeleech-only coverage note is diagnostic, not a provider outage")


if __name__ == "__main__":
    main()
