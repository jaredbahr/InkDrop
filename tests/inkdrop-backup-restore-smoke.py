#!/usr/bin/env python3
"""Smoke-test InkDrop public backup/restore behavior."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import contextlib
import zipfile
from pathlib import Path

from core import inkdrop_backup_restore


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    with tempfile.TemporaryDirectory(prefix="inkdrop-backup-restore-") as tmp:
        root = Path(tmp)
        config = root / "config"
        state = root / "state"
        backups = root / "backups"
        library = root / "library" / "comics"
        for path in (config, state, backups, library):
            path.mkdir(parents=True, exist_ok=True)
        db_path = state / "inkdrop-state.sqlite3"
        provider_secret = "komga-canary-password-DEADBEEF12345678"
        with contextlib.closing(sqlite3.connect(db_path)) as con:
            con.execute("create table app_settings(key text primary key, value_json text)")
            con.execute("insert into app_settings(key, value_json) values (?, ?)", ("path.comic_root", json.dumps(str(library))))
            # provider_configs has no vault indirection like
            # download_client_instances does -- credentials entered through
            # Settings for a provider like Komga land directly in
            # settings_json. This row reproduces AUDIT-BACKUP-P1-01's live
            # production shape: a plaintext password sitting in the raw
            # column the full backup archive embeds a byte-for-byte copy of.
            con.execute(
                "create table provider_configs(id text primary key, provider_type text, "
                "display_name text, enabled integer, base_url text, secret_ref text, "
                "settings_json text, source text, created_at real, updated_at real)"
            )
            con.execute(
                "insert into provider_configs(id, provider_type, display_name, enabled, "
                "base_url, secret_ref, settings_json, source, created_at, updated_at) "
                "values (?,?,?,?,?,?,?,?,?,?)",
                (
                    "komga",
                    "reader",
                    "Komga",
                    1,
                    "https://komga.example.invalid",
                    None,
                    json.dumps({"username": "acquire-bot", "password": provider_secret}),
                    "user",
                    0.0,
                    0.0,
                ),
            )
            # Phase 1 notification connectors: the exact same raw-credential
            # shape as the komga row above (webhook_url has no vault
            # indirection either), but in a table redact_provider_secrets
            # didn't know to scan until AUDIT-BACKUP-P1-02 -- confirm it's
            # covered now, not just provider_configs.
            connector_secret = "https://discord.com/api/webhooks/1/canary-webhook-DEADBEEF"
            con.execute(
                "create table notification_connectors(id text primary key, type text, "
                "name text, enabled integer, settings_json text, events_json text, "
                "series_filter_json text, created_at real, updated_at real)"
            )
            con.execute(
                "insert into notification_connectors(id, type, name, enabled, "
                "settings_json, events_json, series_filter_json, created_at, updated_at) "
                "values (?,?,?,?,?,?,?,?,?)",
                ("discord", "discord", "Discord", 1, json.dumps({"webhook_url": connector_secret}), "[]", "[]", 0.0, 0.0),
            )
            con.commit()
        env = {
            "INKDROP_CONFIG_DIR": str(config),
            "INKDROP_STATE_DIR": str(state),
            "INKDROP_BACKUP_DIR": str(backups),
            "INKDROP_COMIC_ROOT": str(library),
            "INKDROP_MANGA_ROOT": str(root / "old-host-missing" / "manga"),
            "INKDROP_COMICVINE_API_KEY": "super-secret-comicvine-key",
            "INKDROP_QBITTORRENT_PASSWORD": "super-secret-qbit-password",
        }
        result = inkdrop_backup_restore.create_backup_archive(
            config_dir=config,
            state_db_path=db_path,
            backup_dir=backups,
            environ=env,
            label="smoke",
        )
        require(result["ok"], f"backup should succeed: {result}")
        archive = Path(result["archive_path"])
        require(archive.exists(), "backup archive should exist")
        if os.name == "posix":
            require(archive.stat().st_mode & 0o777 == 0o600, "sensitive backup archive must be mode 0600")
            require(backups.stat().st_mode & 0o777 == 0o700, "sensitive backup directory must be mode 0700")
        require(not list(backups.glob(".inkdrop-backup-*.tmp")), "completed backup must not leave temporary archives")
        require(result["manifest"]["contains"]["state_db"] is True, "backup should contain state DB")
        require(result["manifest"]["contains"]["media_files"] is False, "backup must not contain media files")
        require(result["config_export"]["secret_ref_count"] == 2, "secret refs should be recorded without values")

        # AUDIT-BACKUP-P1-01: the embedded state-DB copy must not carry
        # provider_configs.settings_json credentials in plaintext. Checking
        # only the raw zip bytes (below) is not enough on its own -- the
        # member is DEFLATE-compressed, so a plaintext secret wouldn't show
        # up in raw bytes even if the redaction never ran. The decisive
        # check is the *decompressed* state DB member, read the same way
        # restore_backup_archive() itself reads it.
        provider_redaction = result["manifest"]["state_db_backup"].get("provider_secret_redaction")
        require(provider_redaction and provider_redaction.get("rows_redacted") == 2, f"provider secret redaction should report 2 redacted rows (komga + discord connector): {provider_redaction}")
        require(provider_redaction.get("fields_redacted") == 4, f"komga (username+password) and discord connector (webhook_url+declared secret_fields marker) should redact 4 fields total: {provider_redaction}")
        with zipfile.ZipFile(archive) as zf:
            decompressed_state_db = zf.read(inkdrop_backup_restore.STATE_DB_ARCHIVE_NAME)
        require(provider_secret.encode() not in decompressed_state_db, "provider credential leaked into decompressed embedded state DB")
        require(connector_secret.encode() not in decompressed_state_db, "notification connector credential leaked into decompressed embedded state DB")
        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as handle:
            handle.write(decompressed_state_db)
            extracted_state_db = Path(handle.name)
        try:
            with contextlib.closing(sqlite3.connect(f"file:{extracted_state_db}?mode=ro", uri=True)) as con:
                provider_row = con.execute("select settings_json from provider_configs where id='komga'").fetchone()
                connector_row = con.execute("select settings_json from notification_connectors where id='discord'").fetchone()
            require(provider_row is not None, "redaction must not delete the provider row, only its secret fields")
            provider_settings = json.loads(provider_row[0])
            require("password" not in provider_settings, f"password field should be redacted from embedded provider settings: {provider_settings}")
            require("username" not in provider_settings, f"username field should be redacted from embedded provider settings: {provider_settings}")
            require(connector_row is not None, "redaction must not delete the notification connector row, only its secret fields")
            connector_settings = json.loads(connector_row[0])
            require("webhook_url" not in connector_settings, f"webhook_url should be redacted from embedded connector settings: {connector_settings}")
        finally:
            extracted_state_db.unlink(missing_ok=True)

        with archive.open("rb") as handle:
            blob = handle.read()
        require(b"super-secret-comicvine-key" not in blob, "raw ComicVine secret leaked into backup")
        require(b"super-secret-qbit-password" not in blob, "raw qBit secret leaked into backup")
        require(provider_secret.encode() not in blob, "raw provider credential leaked into backup archive bytes")
        require(connector_secret.encode() not in blob, "raw notification connector credential leaked into backup archive bytes")

        restore_config = root / "restore-config"
        restore_state = root / "restore-state"
        preview = inkdrop_backup_restore.restore_backup_archive(
            archive,
            target_config_dir=restore_config,
            target_state_dir=restore_state,
            apply=False,
        )
        require(preview["ok"], f"restore preview should succeed: {preview}")
        require(preview["dry_run"] is True, "preview should be dry-run")
        require(preview["would_restore"]["state_db"] is True, "preview should include state DB")
        require(preview["database_validation"]["state_db"]["quick_check"] == "ok", "preview must validate state DB integrity")
        require(preview["path_warnings"], "preview should warn about source paths missing on this host")
        require(not restore_config.exists(), "dry-run restore must not create config dir")
        require(not restore_state.exists(), "dry-run restore must not create state dir")

        # AUDIT-BACKUP-P2-01: path_remaps/--path-remap was wired but never
        # actually remapped anything -- it only suppressed the warning above.
        # Rather than ship a flag that quietly does nothing, it was removed;
        # confirm it stays removed instead of quietly reappearing.
        try:
            inkdrop_backup_restore.restore_backup_archive(
                archive,
                target_config_dir=restore_config,
                target_state_dir=restore_state,
                apply=False,
                path_remaps={"INKDROP_COMIC_ROOT": "/wherever"},
            )
        except TypeError:
            pass
        else:
            raise AssertionError("path_remaps should no longer be an accepted parameter")

        applied = inkdrop_backup_restore.restore_backup_archive(
            archive,
            target_config_dir=restore_config,
            target_state_dir=restore_state,
            apply=True,
        )
        require(applied["ok"], f"restore apply should succeed: {applied}")
        require(applied["dry_run"] is False, "apply restore should not be dry-run")
        restored_db = restore_state / "inkdrop-state.sqlite3"
        require(restored_db.exists(), "restored state DB should exist")
        with contextlib.closing(sqlite3.connect(restored_db)) as con:
            row = con.execute("select value_json from app_settings where key=?", ("path.comic_root",)).fetchone()
            require(con.execute("pragma quick_check").fetchone()[0] == "ok", "restored DB must pass quick_check")
        require(row and json.loads(row[0]) == str(library), "restored DB should preserve state contents")
        config_export = json.loads((restore_config / "inkdrop-config-export.json").read_text(encoding="utf-8"))
        secret_refs = json.loads((restore_config / "inkdrop-secret-refs.json").read_text(encoding="utf-8"))
        require(config_export["values"]["INKDROP_COMICVINE_API_KEY"] == "<set>", "config export should redact API key")
        require(secret_refs["secrets"]["INKDROP_QBITTORRENT_PASSWORD"]["value"] == "<redacted>", "secret ref should redact password")
        require(applied.get("pre_restore_snapshots") == [], "a first-time restore onto an empty target has nothing to snapshot")

        # A second restore onto the SAME target now has something to lose --
        # it must snapshot what's there before overwriting it.
        restore_backups = root / "restore-backups"
        second_apply = inkdrop_backup_restore.restore_backup_archive(
            archive,
            target_config_dir=restore_config,
            target_state_dir=restore_state,
            apply=True,
            backup_dir=restore_backups,
        )
        require(second_apply.get("pre_restore_snapshots"), "restoring over an existing state DB must snapshot it first")
        snapshot_db = Path(second_apply["pre_restore_snapshots"][0])
        require(snapshot_db.exists(), f"snapshot file should exist: {snapshot_db}")
        if os.name == "posix":
            require(snapshot_db.stat().st_mode & 0o777 == 0o600, "pre-restore snapshot of sensitive state must be mode 0600")
        with contextlib.closing(sqlite3.connect(snapshot_db)) as con:
            snapshot_row = con.execute("select value_json from app_settings where key=?", ("path.comic_root",)).fetchone()
        require(snapshot_row and json.loads(snapshot_row[0]) == str(library), "snapshot should hold the pre-second-restore content, not the new one")
        require(len(second_apply["pre_restore_snapshots"]) == 3, f"expected a snapshot for the state DB and both config files: {second_apply['pre_restore_snapshots']}")

        preview_after_apply = inkdrop_backup_restore.restore_backup_archive(
            archive,
            target_config_dir=restore_config,
            target_state_dir=restore_state,
            apply=False,
            backup_dir=restore_backups,
        )
        require("pre_restore_snapshots" not in preview_after_apply, "a dry-run preview must not create or report any snapshot")

        # A syntactically valid ZIP containing arbitrary bytes used to pass
        # preview and overwrite the live state database.  Both preview and
        # apply must reject it without changing the existing target.
        corrupt_archive = root / "corrupt-state.zip"
        with zipfile.ZipFile(archive, "r") as source, zipfile.ZipFile(corrupt_archive, "w") as corrupt:
            for info in source.infolist():
                payload = source.read(info.filename)
                if info.filename == inkdrop_backup_restore.STATE_DB_ARCHIVE_NAME:
                    payload = b"not-a-sqlite-database"
                corrupt.writestr(info, payload)
        before_corrupt_apply = restored_db.read_bytes()
        for corrupt_apply in (False, True):
            try:
                inkdrop_backup_restore.restore_backup_archive(
                    corrupt_archive,
                    target_config_dir=restore_config,
                    target_state_dir=restore_state,
                    apply=corrupt_apply,
                    backup_dir=restore_backups,
                )
            except ValueError as exc:
                require("valid SQLite database" in str(exc), "corrupt DB rejection should explain the failure")
            else:
                raise AssertionError(f"corrupt state DB should be rejected (apply={corrupt_apply})")
            require(restored_db.read_bytes() == before_corrupt_apply, "rejected restore must preserve current state DB")
        require(not list(restore_state.glob(".inkdrop-restore-*")), "rejected restore must clean staged files")

        # An archive without application state is not a successful InkDrop
        # backup, even when redacted configuration can still be exported.
        missing_state_backups = root / "missing-state-backups"
        try:
            inkdrop_backup_restore.create_backup_archive(
                config_dir=config,
                state_db_path=root / "missing-state.sqlite3",
                backup_dir=missing_state_backups,
                environ=env,
                label="missing-state",
            )
        except ValueError as exc:
            require("state database backup failed" in str(exc), "missing DB failure should be explicit")
        else:
            raise AssertionError("backup without a state DB must not report success")
        require(not list(missing_state_backups.glob("*.zip")), "failed backup must not publish an archive")
        require(not list(missing_state_backups.glob(".inkdrop-backup-*.tmp")), "failed backup must clean temporary archive")

        traversal = root / "bad.zip"

        with zipfile.ZipFile(traversal, "w") as zf:
            zf.writestr("../escape.txt", "nope")
            zf.writestr("manifest.json", "{}")
        try:
            inkdrop_backup_restore.restore_backup_archive(traversal, target_config_dir=restore_config, target_state_dir=restore_state)
        except ValueError as exc:
            require("unsafe archive member" in str(exc), "unsafe member should be rejected with clear error")
        else:
            raise AssertionError("unsafe archive member should be rejected")

    print("INKDROP_BACKUP_RESTORE_OK")


if __name__ == "__main__":
    main()
