#!/usr/bin/env python3
"""The Download Clients page lists clients the user added, not forced defaults.

Two self-hosting users hit the same confusion from opposite ends. One saw a
qBittorrent card with no URL field and concluded he had to add a whole new
client. The other said it plainly: "i see slskd panel enabled, but it seems I
still need to add a slskd client."

Both were looking at the same thing -- a provider card InkDrop seeded onto every
install whether or not that client existed. SLSKD's was even marked enabled
because a probe script ships inside InkDrop's own image, so "enabled" described
InkDrop's filesystem rather than the user's setup. A card in that state is
indistinguishable from a configured client, which is why neither user could tell
whether they had one.

Download clients now behave like what they are: connections to something the
user runs, added explicitly. Direct-download sources (MangaDex, RSS, ComicsCodes)
are the opposite -- built into InkDrop, nothing to instantiate -- so they stay
pre-listed with a toggle, and this test holds that line too.

The risk in removing a forced default is orphaning real configuration, since
those legacy cards are live config that load_qbit_settings() and friends still
read. So the seeding stops only for installs that never had the row, and every
configured card is carried across into a real instance -- credentials moving out
of settings_json plaintext into the secret store on the way.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import time
from pathlib import Path

from core import inkdrop_download_client_config as config
from core import inkdrop_secret_store
from core import inkdrop_state


FAILURES = []


def check(label, condition, detail=""):
    if condition:
        print(f"PASS  {label}")
    else:
        print(f"FAIL  {label}" + (f" -- {detail}" if detail else ""))
        FAILURES.append(label)


def legacy_card(db, provider_id, display_name, *, enabled, base_url, settings):
    now = time.time()
    con = sqlite3.connect(db)
    try:
        con.execute(
            "insert into provider_configs(id,provider_type,display_name,enabled,base_url,secret_ref,"
            "settings_group,ownership,automation_role,description,next_action,capabilities_json,"
            "applied_by_json,settings_json,source,created_at,updated_at) "
            "values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                provider_id, "download_client", display_name, 1 if enabled else 0, base_url, "",
                "download_clients", "native", "", "", "", "[]", "[]",
                json.dumps(settings), "user", now, now,
            ),
        )
        con.commit()
    finally:
        con.close()


def instance_of(db, client_type, *, secret_root=None):
    rows = config.list_instances(db, secret_root=secret_root)["instances"]
    return next((row for row in rows if row["client_type"] == client_type), None)


def migration_status(db, client_type):
    con = sqlite3.connect(db)
    try:
        row = con.execute(
            "select status from download_client_instance_migrations where migration_key=?",
            (f"{config.LEGACY_MIGRATION_KEY_PREFIX}{client_type}",),
        ).fetchone()
    finally:
        con.close()
    return row[0] if row else None


def test_configured_cards_survive():
    with tempfile.TemporaryDirectory(prefix="inkdrop-add-first-") as tmp:
        root = Path(tmp)
        db = root / "state.sqlite3"
        secrets = root / "secrets"
        inkdrop_state.ensure_schema(db)

        # Exactly what an established install has: a working qBittorrent whose
        # password sits in settings_json plaintext, a working SABnzbd, and an
        # SLSKD card carrying tuned probe knobs.
        legacy_card(db, "qbittorrent", "qBittorrent", enabled=True, base_url="http://qbit.internal:8080", settings={
            "username": "inkdrop", "password": "s3cret-qbit",
            "comics_category": "comics", "ebooks_category": "ebooks",
            "comics_save_path": "/downloads/comics", "ebooks_save_path": "/downloads/ebooks",
            "torrent_cleanup_policy": "external_retention",
            "secret_fields": ["password"],
        })
        legacy_card(db, "sabnzbd", "SABnzbd", enabled=True, base_url="http://sab.internal:8085", settings={
            "api_key": "sab-api-key-value", "comics_category": "comics",
            "remove_completed_downloads": True, "sab_history_limit": 500,
            "secret_fields": ["api_key"],
        })
        legacy_card(db, "slskd", "SLSKD", enabled=True, base_url="http://slskd.internal:5030", settings={
            "api_key": "slskd-api-key-value",
            "download_root": "/downloads/staging/slskd",
            "incomplete_root": "/downloads/staging/slskd/incomplete",
            "max_total": 20, "max_per_series": 12, "wait_seconds": 8,
            "delete_search_history": True, "search_history_keep": 100,
            "secret_fields": ["api_key"],
        })

        result = config.materialize_legacy_instances(db, secret_root=secrets)
        migrated = {row["client_type"] for row in result["migrated"]}
        check("every configured legacy card becomes an instance",
              migrated == {"qbittorrent", "sabnzbd", "slskd"}, result)

        qbit = instance_of(db, "qbittorrent", secret_root=secrets)
        check("qBittorrent endpoint and username carry across",
              qbit and qbit["base_url"] == "http://qbit.internal:8080" and qbit["username"] == "inkdrop", qbit)
        check("qBittorrent stays enabled -- a working client keeps working",
              qbit and qbit["enabled"] is True, qbit)
        check("qBittorrent per-media categories and paths carry across",
              qbit and qbit["categories"] == {"comics": "comics", "ebooks": "ebooks"}
              and qbit["download_paths"] == {"comics": "/downloads/comics", "ebooks": "/downloads/ebooks"}, qbit)
        check("qBittorrent cleanup policy carries across",
              qbit and qbit["settings"].get("torrent_cleanup_policy") == "external_retention", qbit)

        # The password was sitting in settings_json in the clear. It must arrive
        # as a real secret-store reference, readable back as the same value and
        # never echoed by the API.
        check("qBittorrent password is now a stored secret",
              qbit and qbit["secret_fields"].get("password", {}).get("configured") is True, qbit)
        check("the migrated instance never echoes the secret value",
              "s3cret-qbit" not in json.dumps(qbit), "secret leaked into the public payload")
        resolved = config.adapter_settings(db, qbit["id"], secret_root=secrets)
        check("the migrated password still resolves to the original value",
              resolved.get("password") == "s3cret-qbit", "adapter could not read the migrated secret")

        sab = instance_of(db, "sabnzbd", secret_root=secrets)
        check("SABnzbd API key and cleanup knobs carry across",
              sab and sab["secret_fields"].get("api_key", {}).get("configured") is True
              and sab["settings"].get("sab_history_limit") == 500, sab)

        slskd = instance_of(db, "slskd", secret_root=secrets)
        check("SLSKD download root becomes the instance download path",
              slskd and slskd["download_path"] == "/downloads/staging/slskd", slskd)
        check("SLSKD probe tuning carries across untouched",
              slskd and slskd["settings"].get("max_total") == 20
              and slskd["settings"].get("max_per_series") == 12
              and slskd["settings"].get("incomplete_root") == "/downloads/staging/slskd/incomplete", slskd)
        check("SLSKD search-history cleanup choice carries across",
              slskd and slskd["settings"].get("delete_search_history") is True
              and slskd["settings"].get("search_history_keep") == 100, slskd)

        # Nothing is deleted. The adapters' legacy fallback path has to keep
        # working for anything not routed through an instance yet.
        con = sqlite3.connect(db)
        try:
            kept = con.execute(
                "select count(*) from provider_configs where id in ('qbittorrent','sabnzbd','slskd')"
            ).fetchone()[0]
        finally:
            con.close()
        check("the legacy rows are preserved, not deleted", kept == 3, f"{kept} of 3 rows remain")

        # Running again must not duplicate anything.
        again = config.materialize_legacy_instances(db, secret_root=secrets)
        check("re-running the migration creates nothing new", not again["migrated"], again)
        check("instance count stays at three",
              len(config.list_instances(db, secret_root=secrets)["instances"]) == 3, "duplicates created")
        check("each client type is recorded as completed",
              all(migration_status(db, name) == "completed" for name in ("qbittorrent", "sabnzbd", "slskd")),
              {name: migration_status(db, name) for name in ("qbittorrent", "sabnzbd", "slskd")})

        # A user who deletes a migrated instance on purpose must not have it
        # silently resurrected on the next settings load.
        current = config.get_instance(db, sab["id"])
        config.update_instance(db, sab["id"], {"enabled": False}, expected_revision=current["revision"], secret_root=secrets)
        current = config.get_instance(db, sab["id"])
        config.soft_delete_instance(db, sab["id"], expected_revision=current["revision"], secret_root=secrets)
        config.materialize_legacy_instances(db, secret_root=secrets)
        check("a deliberately deleted instance is not recreated",
              instance_of(db, "sabnzbd", secret_root=secrets) is None, "SABnzbd came back")


def test_unconfigured_card_is_not_materialized():
    with tempfile.TemporaryDirectory(prefix="inkdrop-add-first-empty-") as tmp:
        root = Path(tmp)
        db = root / "state.sqlite3"
        secrets = root / "secrets"
        inkdrop_state.ensure_schema(db)

        # Precisely the state Jibz was looking at: the card says enabled, and
        # there is nothing behind it. Materializing this would recreate the very
        # ambiguity the change removes, so it must stay a no-op.
        legacy_card(db, "slskd", "SLSKD", enabled=True, base_url="", settings={
            "api_key": "", "download_root": "/downloads/staging/slskd", "secret_fields": ["api_key"],
        })
        result = config.materialize_legacy_instances(db, secret_root=secrets)
        check("an enabled-but-empty card creates no instance", not result["migrated"], result)
        check("the empty card is reported as not configured",
              any(row["reason"] == "not_configured" for row in result["skipped"]), result)
        check("it stays pending so a later real config still migrates",
              migration_status(db, "slskd") == "pending", migration_status(db, "slskd"))

        # And the page is told this card holds nothing, so it can retire it
        # rather than leave "Enabled" on screen with no client behind it.
        from core import inkdrop_download_client_api as client_api

        cards = {row["client_type"]: row for row in client_api.list_payload(db).get("legacy_client_cards") or []}
        check("the API reports the empty card as unconfigured",
              cards.get("slskd", {}).get("configured") is False, cards)

        # Now the user actually fills it in. The retry must pick it up.
        con = sqlite3.connect(db)
        try:
            con.execute(
                "update provider_configs set base_url=?, settings_json=? where id='slskd'",
                ("http://slskd.internal:5030", json.dumps({"api_key": "late-key", "download_root": "/downloads/staging/slskd"})),
            )
            con.commit()
        finally:
            con.close()
        result = config.materialize_legacy_instances(db, secret_root=secrets)
        check("a card configured after the first pass still migrates",
              [row["client_type"] for row in result["migrated"]] == ["slskd"], result)


def test_incomplete_card_is_kept_rather_than_lost():
    with tempfile.TemporaryDirectory(prefix="inkdrop-add-first-partial-") as tmp:
        root = Path(tmp)
        db = root / "state.sqlite3"
        secrets = root / "secrets"
        inkdrop_state.ensure_schema(db)

        # A password with no username is something the legacy card allowed and
        # the instance model refuses to enable (qBittorrent's login endpoint
        # needs both). The config must still be kept, just not armed.
        legacy_card(db, "qbittorrent", "qBittorrent", enabled=True, base_url="http://qbit.internal:8080", settings={
            "username": "", "password": "orphaned-password", "secret_fields": ["password"],
        })
        result = config.materialize_legacy_instances(db, secret_root=secrets)
        row = instance_of(db, "qbittorrent", secret_root=secrets)
        check("an incomplete card still produces an instance", row is not None, result)
        check("it is saved disabled rather than dropped",
              row and row["enabled"] is False, row)
        check("its endpoint and secret are preserved for the user to finish",
              row and row["base_url"] == "http://qbit.internal:8080"
              and row["secret_fields"].get("password", {}).get("configured") is True, row)


def test_fresh_install_seeds_no_client_cards():
    from core import inkdrop_web

    with tempfile.TemporaryDirectory(prefix="inkdrop-add-first-seed-") as tmp:
        db = Path(tmp) / "state.sqlite3"
        inkdrop_state.ensure_schema(db)
        original = inkdrop_web.INKDROP_STATE_DB
        try:
            inkdrop_web.INKDROP_STATE_DB = db
            ids = {str(row.get("id")) for row in inkdrop_web.runtime_provider_settings()["providers"]}
        finally:
            inkdrop_web.INKDROP_STATE_DB = original
        check("a fresh install seeds no qBittorrent card", "qbittorrent" not in ids, sorted(ids))
        check("a fresh install seeds no SABnzbd card", "sabnzbd" not in ids, sorted(ids))
        check("a fresh install seeds no SLSKD card", "slskd" not in ids, sorted(ids))

        # The other half of the model: built-in direct-download sources have
        # nothing for a user to instantiate, so they stay pre-listed with a
        # toggle. Removing those would be the opposite mistake.
        for provider_id in ("mangadex", "rss", "comicscodes"):
            check(f"the built-in {provider_id} source is still pre-listed", provider_id in ids, sorted(ids))

        # An install that already has the row keeps its card until migration.
        legacy_card(db, "qbittorrent", "qBittorrent", enabled=True, base_url="http://qbit.internal:8080", settings={
            "username": "inkdrop", "password": "kept", "secret_fields": ["password"],
        })
        try:
            inkdrop_web.INKDROP_STATE_DB = db
            ids = {str(row.get("id")) for row in inkdrop_web.runtime_provider_settings()["providers"]}
        finally:
            inkdrop_web.INKDROP_STATE_DB = original
        check("an existing qBittorrent card is still refreshed", "qbittorrent" in ids, sorted(ids))


def test_page_keeps_the_two_models_apart():
    from core import inkdrop_web

    manager = (Path(__file__).resolve().parents[1] / "web/static/js/inkdrop-download-clients-ui.js").read_text(encoding="utf-8")
    check("the manager is no longer framed as 'additional' instances",
          "Additional Download Client Instances" not in manager and '"Download Clients"' in manager,
          "the heading still implies a built-in set these are additional to")
    check("the empty state says to add the client you run",
          "No download clients yet" in manager, "empty state does not prompt the add-first flow")
    check("the empty state explains the client is not bundled",
          "it isn't bundled" in manager, "nothing distinguishes a client from a built-in source")
    check("SLSKD's not-active note doesn't point at a card that isn't there",
          "InkDrop starts using this for SLSKD search and downloads once it's enabled" in manager,
          "a fresh install would be told to look for a legacy card below")
    # An active instance is what retires the SLSKD card, so the active note must
    # not promise one. Found on screen: it said "The single SLSKD card below is
    # disabled while this is active" over a card that was already display:none.
    check("SLSKD's active note doesn't promise a card the same render retired",
          "The single SLSKD card below is disabled while this is active" not in manager,
          "the active note still sends the user looking for a hidden card")
    check("the SLSKD note reads the same signal that retires the card",
          "slskdCardStillShowing" in manager and "legacy_client_cards" in manager,
          "the note and the page can disagree about whether a card is there")

    # Every SLSKD knob the legacy card exposed must be reachable on the
    # instance, or removing that card would put settings out of reach.
    for knob in ("max_total", "max_per_series", "delete_search_history",
                 "search_history_keep", "search_history_max_delete", "search_history_min_age_minutes"):
        check(f"the instance editor exposes {knob}", f'"{knob}"' in manager, "knob is unreachable once the card is gone")

    check("the Download Clients page labels its built-in sources separately",
          "Built-in sources" in inkdrop_web.HTML, "clients and built-in sources still read as one list")
    check("the built-in source group says there is nothing to connect",
          "there is nothing to install or connect" in inkdrop_web.HTML,
          "nothing tells the user these need no setup of their own")

    # Found live, on the real page: hideMigratedLegacyCards() sets `hidden`, and
    # applyInkdropSettingsSearch() clears `hidden` on every .settings-card
    # whenever the search box is empty. The superseded card came straight back,
    # so a migrated install showed the same client twice. The CSS rule keys off
    # the data attribute instead, which no filter pass touches.
    bundle = (Path(__file__).resolve().parents[1] / "web/static/css/inkdrop.css").read_text(encoding="utf-8")
    check("a superseded legacy card is retired by CSS, not by an attribute a filter can clear",
          '.settings-card[data-download-client-legacy-fallback="hidden-by-instance"]' in bundle,
          "the empty-query search pass will un-hide the superseded card again")
    check("the manager still marks superseded cards for that rule",
          '"hidden-by-instance"' in manager, "nothing sets the attribute the CSS keys off")
    check("an empty legacy card is retired too, not left saying Enabled",
          "legacy_client_cards" in manager and "row.configured" in manager,
          "a card with no endpoint and no credential still reads as a configured client")


def main():
    test_configured_cards_survive()
    test_unconfigured_card_is_not_materialized()
    test_incomplete_card_is_kept_rather_than_lost()
    test_fresh_install_seeds_no_client_cards()
    test_page_keeps_the_two_models_apart()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
        return 1
    print("download-client add-first model smoke: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
