#!/usr/bin/env python3
"""Focused rollback, permission, cleanup, and backup smoke for client secrets."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import time
from pathlib import Path

import inkdrop_backup_restore
import inkdrop_download_client_config as config
import inkdrop_secret_store
import inkdrop_state


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    with tempfile.TemporaryDirectory(prefix="inkdrop-download-client-secrets-") as tmp:
        root = Path(tmp)
        db = root / "state.sqlite3"
        secret_root = root / "config" / "secrets" / "download-clients"
        with inkdrop_state.connect(db) as con:
            inkdrop_state.init_schema(con)

        instance = inkdrop_state.create_download_client_instance(
            db,
            {
                "id": "rollback-qbit",
                "name": "Rollback qBit",
                "client_type": "qbittorrent",
                "enabled": True,
                "base_url": "http://qbit:8080",
                "username": "inkdrop",
                "secrets": {"password": "original-secret-value"},
            },
            secret_root=secret_root,
        )
        refs_before = config.active_secret_references(db)
        require(len(refs_before) == 1, "one opaque secret ref is stored")
        require(inkdrop_secret_store.read_secret(refs_before[0], root=secret_root) == "original-secret-value", "secret resolves outside SQLite")
        require(not refs_before[0].endswith("password"), "secret reference is opaque")

        root_status = inkdrop_secret_store.ensure_secret_root(root=secret_root)
        secret_path = inkdrop_secret_store.reference_path(refs_before[0], root=secret_root)
        if os.name == "nt":
            require(root_status["permissions_enforced"] is False, "Windows does not claim POSIX ACL proof")
            require("Windows ACLs" in root_status["platform_caveat"], "Windows permission caveat is explicit")
        else:
            require(stat.S_IMODE(secret_root.stat().st_mode) == 0o700, "secret directory is mode 0700")
            require(stat.S_IMODE(secret_path.stat().st_mode) == 0o600, "secret file is mode 0600")

        def fail_commit(_con):
            raise RuntimeError("injected commit failure")

        try:
            inkdrop_state.update_download_client_instance(
                db,
                instance["id"],
                {"secrets": {"password": "replacement-that-must-rollback"}},
                expected_revision=instance["revision"],
                secret_root=secret_root,
                before_commit=fail_commit,
            )
        except RuntimeError as exc:
            require("injected" in str(exc), "injected failure reached caller")
        else:
            raise AssertionError("expected injected commit failure")
        unchanged = inkdrop_state.download_client_instance(db, instance["id"])
        refs_after_failure = config.active_secret_references(db)
        require(unchanged["revision"] == 1, "failed transaction preserves revision")
        require(refs_after_failure == refs_before, "failed transaction preserves old ref")
        require(inkdrop_secret_store.list_references(root=secret_root) == refs_before, "failed transaction removes unreferenced new secret")
        require(inkdrop_secret_store.read_secret(refs_before[0], root=secret_root) == "original-secret-value", "failed transaction preserves old secret")

        replaced = inkdrop_state.update_download_client_instance(
            db,
            instance["id"],
            {"secrets": {"password": "replacement-secret-value"}},
            expected_revision=1,
            secret_root=secret_root,
        )
        refs_after_success = config.active_secret_references(db)
        require(replaced["revision"] == 2 and refs_after_success != refs_before, "successful replacement advances ref and revision")
        require(not secret_path.exists(), "old ref is garbage-collected only after DB commit")
        require(inkdrop_secret_store.read_secret(refs_after_success[0], root=secret_root) == "replacement-secret-value", "new ref resolves")

        kept = inkdrop_state.update_download_client_instance(
            db,
            instance["id"],
            {"secrets": {"password": ""}},
            expected_revision=2,
            secret_root=secret_root,
        )
        require(config.active_secret_references(db) == refs_after_success, "blank secret means leave existing secret unchanged")

        # A state-DB restore brings the secret_refs_json pointer back, but the
        # physical .secret file is intentionally excluded from backups and
        # never comes back with it -- status must reflect that, not just the
        # pointer's presence.
        restored_secret_path = inkdrop_secret_store.reference_path(refs_after_success[0], root=secret_root)
        require(restored_secret_path.exists(), "sanity: secret file exists before simulating a restore")
        os.remove(restored_secret_path)
        post_restore = inkdrop_state.download_client_instance(db, instance["id"], secret_root=secret_root)
        require(
            post_restore["secret_fields"]["password"] == {"configured": False, "reason": "secret_unresolvable"},
            "a restored instance whose secret file never came back must not report configured=true",
        )
        post_restore_list = inkdrop_state.download_client_instances(db, secret_root=secret_root)
        listed = next(item for item in post_restore_list["instances"] if item["id"] == instance["id"])
        require(
            listed["secret_fields"]["password"] == {"configured": False, "reason": "secret_unresolvable"},
            "the instance list view must also reflect the unresolvable secret",
        )
        # Restore the exact same file so the rest of this test's clear/GC
        # assertions below still exercise the normal, resolvable-secret path.
        restored_secret_path.write_bytes(b"replacement-secret-value")
        require(
            inkdrop_secret_store.read_secret(refs_after_success[0], root=secret_root) == "replacement-secret-value",
            "sanity: the reference resolves again once its file is back",
        )

        cleared = inkdrop_state.update_download_client_instance(
            db,
            instance["id"],
            {"enabled": False, "clear_secret_fields": ["password"]},
            expected_revision=kept["revision"],
            secret_root=secret_root,
        )
        require(cleared["secret_fields"] == {}, "explicit clear removes secret presence")
        require(config.active_secret_references(db) == [], "explicit clear removes DB reference")
        require(inkdrop_secret_store.list_references(root=secret_root) == [], "clear garbage-collects old ref after commit")

        active_write = inkdrop_secret_store.write_secret("active", root=secret_root)
        orphan_one = inkdrop_secret_store.write_secret("orphan-one", root=secret_root)
        orphan_two = inkdrop_secret_store.write_secret("orphan-two", root=secret_root)
        old = time.time() - 7200
        for reference in (active_write["reference"], orphan_one["reference"], orphan_two["reference"]):
            os.utime(inkdrop_secret_store.reference_path(reference, root=secret_root), (old, old))
        cleanup = inkdrop_secret_store.cleanup_orphans(
            [active_write["reference"]],
            root=secret_root,
            max_delete=1,
            min_age_seconds=3600,
            now=time.time(),
        )
        require(cleanup["deleted_count"] == 1, "orphan cleanup honors bounded delete limit")
        require(inkdrop_secret_store.reference_path(active_write["reference"], root=secret_root).exists(), "orphan cleanup preserves active refs")

        backup_document = inkdrop_backup_restore.export_portable_settings(db)
        backup_text = json.dumps(backup_document, sort_keys=True)
        require("original-secret-value" not in backup_text, "portable backup excludes original secret")
        require("replacement-secret-value" not in backup_text, "portable backup excludes replacement secret")
        require(active_write["reference"] not in backup_text, "portable backup excludes secret refs/files")
        require(".secret" not in backup_text, "portable backup contains no secret-file path")

        print(json.dumps({
            "ok": True,
            "rollback_preserved_reference": refs_before[0],
            "windows_permission_caveat": os.name == "nt",
            "orphan_cleanup_deleted": cleanup["deleted_count"],
            "backup_secret_free": True,
        }, sort_keys=True))


if __name__ == "__main__":
    main()
