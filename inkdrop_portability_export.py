#!/usr/bin/env python3
"""One reviewable JSON file: settings, providers, and the series list.

This is the portability export, not disaster recovery. ``inkdrop_backup_restore``
builds the full-state zip with a SQLite copy inside; this module writes a single
JSON document a person can read line by line, keep in version control, or use to
rebuild a fresh install by hand. It contains:

- the same redacted settings document the Settings backup card already exports,
- every configured acquisition provider with credential values stripped
  (secret references stay, because they are names, not secrets),
- every configured download client with secrets and usernames reduced to
  configured/not-configured flags,
- every live series with its monitored flags and, for manga, the
  volume/chapter acquisition policy. Series parked by automation (retired
  shadows, replaced metadata identities) are counted but not listed.

The state database is opened read-only. Secret values, auth users and sessions,
queue/task state, history, and media files are never included.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

import inkdrop_backup_restore
import inkdrop_download_client_config
import inkdrop_runtime_config
import inkdrop_state
import inkdrop_version


PORTABILITY_SCHEMA = "inkdrop.portability_export.v1"
PORTABILITY_PRODUCT = "InkDrop"


def _canonical_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


def document_checksum(document):
    payload = {key: value for key, value in dict(document or {}).items() if key != "checksum"}
    return "sha256:" + hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _as_bool(value):
    return bool(value) if isinstance(value, bool) else str(value or "0").strip() not in {"", "0", "false", "no"}


def sanitized_provider_settings(settings):
    """Split provider settings into exportable values and dropped credential fields."""
    kept = {}
    removed = []
    # Fields the provider itself declared secret are dropped first: names like
    # discord_webhook_url carry credentials without matching any credential
    # name pattern.
    declared = settings.get("secret_fields") if isinstance(settings, dict) else None
    declared = {str(name) for name in declared} if isinstance(declared, list) else set()
    for key in sorted(settings or {}, key=str):
        name = str(key)
        if name == "secret_ref":
            kept[name] = str(settings[key] or "")
            continue
        if name in declared:
            removed.append(name)
            continue
        if inkdrop_backup_restore.is_secret_key(name):
            removed.append(name)
            continue
        value = settings[key]
        if isinstance(value, (dict, list)):
            # Nested provider settings are rare and may bury credentials one
            # level down; drop them rather than guessing which leaves are safe.
            removed.append(name)
            continue
        kept[name] = value
    return kept, removed


def provider_entries(con):
    rows = con.execute(
        """
        select id, provider_type, display_name, enabled, base_url, secret_ref,
               settings_group, automation_role, settings_json
        from provider_configs
        order by id
        """
    ).fetchall()
    entries = []
    for row in rows:
        settings = inkdrop_state.json_loads(row["settings_json"] or "{}", {})
        settings = settings if isinstance(settings, dict) else {}
        kept, removed = sanitized_provider_settings(settings)
        entries.append(
            {
                "id": row["id"],
                "provider_type": row["provider_type"],
                "display_name": row["display_name"],
                "enabled": _as_bool(row["enabled"]),
                "base_url": str(row["base_url"] or ""),
                "secret_ref": str(row["secret_ref"] or ""),
                "settings_group": str(row["settings_group"] or ""),
                "automation_role": str(row["automation_role"] or ""),
                "settings": kept,
                "credential_fields_removed": removed,
            }
        )
    return entries


def series_entries(con):
    rows = con.execute(
        """
        select id, title, sort_title, media_type, year, publisher,
               metadata_provider, metadata_id, source, monitored, monitor_new,
               auto_grab, library_root, library_path, created_at, raw_json
        from series
        order by lower(coalesce(nullif(sort_title, ''), title)), id
        """
    ).fetchall()
    entries = []
    excluded = 0
    for row in rows:
        raw = inkdrop_state.json_loads(row["raw_json"] or "{}", {})
        raw = raw if isinstance(raw, dict) else {}
        # Rows parked by automation (retired shadows, replaced metadata
        # identities) are bookkeeping, not library members someone would
        # re-add from this document.
        if str(row["source"] or "") == "replaced_metadata" or str(raw.get("automation_parked_reason") or "").strip():
            excluded += 1
            continue
        media_type = str(row["media_type"] or "comic").strip().lower()
        entry = {
            "id": row["id"],
            "title": row["title"],
            "media_type": media_type,
            "year": row["year"],
            "publisher": str(row["publisher"] or ""),
            "metadata_provider": str(row["metadata_provider"] or ""),
            "metadata_id": str(row["metadata_id"] or ""),
            "source": str(row["source"] or ""),
            "monitored": _as_bool(row["monitored"]),
            "monitor_new": _as_bool(row["monitor_new"]),
            "auto_grab": _as_bool(row["auto_grab"]),
            # Folder name only. Full library paths can carry host usernames,
            # and this document is meant to be shareable and version-safe.
            "library_root": str(row["library_root"] or ""),
            "library_folder": Path(str(row["library_path"] or "")).name,
            "added_at": inkdrop_backup_restore.utc_stamp(row["created_at"]) if row["created_at"] else "",
        }
        if media_type in inkdrop_state.MANGA_MEDIA_TYPES:
            policy = inkdrop_state.media_management_manga_unit_policy({"raw_json": row["raw_json"]})
            if policy.get("has_manga_unit_policy"):
                entry["manga_unit_policy"] = policy.get("manga_unit_policy")
                entry["manga_unit_model"] = policy.get("manga_unit_model")
                entry["manga_unit_policy_source"] = policy.get("manga_unit_policy_source")
        entries.append(entry)
    return entries, excluded


def download_client_entries(db_path, *, secret_root=None):
    """Configured download clients with secrets and usernames reduced to flags."""
    try:
        listing = inkdrop_download_client_config.list_instances(db_path, secret_root=secret_root)
    except Exception:
        return []
    entries = []
    for item in listing.get("instances") or []:
        secrets_configured = sorted(
            field
            for field, status in (item.get("secret_fields") or {}).items()
            if isinstance(status, dict) and status.get("configured")
        )
        if str(item.get("username") or "").strip():
            secrets_configured = sorted(secrets_configured + ["username"])
        entries.append(
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "client_type": item.get("client_type"),
                "enabled": _as_bool(item.get("enabled")),
                "priority": int(item.get("priority") or 100),
                "base_url": str(item.get("base_url") or ""),
                "category": str(item.get("category") or ""),
                "categories": dict(item.get("categories") or {}),
                "provider_mappings": list(item.get("provider_mappings") or []),
                "secrets_configured": secrets_configured,
            }
        )
    return entries


def export_portability_document(db_path, *, now=None, version=None, secret_root=None):
    db_path = Path(db_path)
    now = time.time() if now is None else float(now)
    settings_document = inkdrop_backup_restore.export_portable_settings(db_path, now=now, version=version)
    with inkdrop_state.connect_read(db_path) as con:
        providers = provider_entries(con)
        series, series_excluded = series_entries(con)
    download_clients = download_client_entries(db_path, secret_root=secret_root)
    metadata = inkdrop_version.build_metadata() if version is None else {"version": str(version)}
    monitored_count = sum(1 for item in series if item["monitored"])
    media_type_counts = {}
    for item in series:
        media_type_counts[item["media_type"]] = media_type_counts.get(item["media_type"], 0) + 1
    document = {
        "schema": PORTABILITY_SCHEMA,
        "schema_version": 1,
        "product": PORTABILITY_PRODUCT,
        "product_version": str(metadata.get("version") or "dev")[:64],
        "exported_at": inkdrop_backup_restore.utc_stamp(now),
        "settings": settings_document,
        "providers": providers,
        "download_clients": download_clients,
        "series": series,
        "summary": {
            "series_total": len(series),
            "series_monitored": monitored_count,
            "series_by_media_type": media_type_counts,
            "series_excluded_parked_or_replaced": series_excluded,
            "providers_total": len(providers),
            "download_clients_total": len(download_clients),
        },
        "contains": {
            "app_settings": True,
            "provider_config": True,
            "download_client_config": True,
            "service_urls": True,
            "provider_credentials": False,
            "download_client_credentials": False,
            "full_library_paths": False,
            "auth_users_sessions": False,
            "queue_history_state": False,
            "media": False,
        },
    }
    document["checksum"] = document_checksum(document)
    return document


def portability_export_filename(document):
    stamp = "".join(character for character in str((document or {}).get("exported_at") or "") if character.isdigit())[:14]
    return f"inkdrop-portability-{stamp or 'export'}.json"


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Write the InkDrop settings/providers/series portability document as JSON."
    )
    parser.add_argument(
        "--db",
        default=str(inkdrop_runtime_config.state_db_path()),
        help="state database path (default: the configured state directory)",
    )
    parser.add_argument("--out", default="", help="output file (default: stdout)")
    parser.add_argument("--compact", action="store_true", help="write single-line JSON instead of indented")
    args = parser.parse_args(argv)
    db_path = Path(args.db)
    if not db_path.exists():
        parser.error(f"state database not found: {db_path}")
    document = export_portability_document(db_path)
    rendered = json.dumps(document, ensure_ascii=False, sort_keys=True, indent=None if args.compact else 2)
    if args.out:
        out_path = Path(args.out)
        if out_path.is_dir():
            out_path = out_path / portability_export_filename(document)
        out_path.write_text(rendered + "\n", encoding="utf-8")
        print(f"wrote {out_path} ({document['summary']['series_total']} series, "
              f"{document['summary']['providers_total']} providers)")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
