#!/usr/bin/env python3
"""Bound SLSKD search-history growth through the public SLSKD API."""

from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))


import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml


DEFAULT_BASE_URL = "http://127.0.0.1:5030/api/v0"
STATE_DB_NAME = "inkdrop-state.sqlite3"


def int_env(name: str, default: int) -> int:
    value = str(os.environ.get(name, "")).strip()
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed >= 0 else default


def boolish(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def state_db_path() -> Path:
    explicit = str(os.environ.get("INKDROP_STATE_DB") or "").strip()
    if explicit:
        return Path(explicit)
    return Path(os.environ.get("INKDROP_STATE_DIR") or "/state") / STATE_DB_NAME


def load_provider_settings(provider_id: str) -> dict:
    db_path = state_db_path()
    if not db_path.exists():
        return {}
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5) as con:
            row = con.execute(
                "select settings_json from provider_configs where id = ? limit 1",
                (provider_id,),
            ).fetchone()
    except sqlite3.Error:
        return {}
    if not row:
        return {}
    try:
        payload = json.loads(row[0] or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def cleanup_enabled(provider_settings: dict) -> bool:
    if "delete_search_history" in provider_settings:
        return boolish(provider_settings.get("delete_search_history"), False)
    if "search_history_cleanup_enabled" in provider_settings:
        return boolish(provider_settings.get("search_history_cleanup_enabled"), False)
    return boolish(os.environ.get("INKDROP_SLSKD_SEARCH_HISTORY_CLEANUP_ENABLED"), False)


def setting_int(provider_settings: dict, key: str, env_name: str, default: int) -> int:
    if key in provider_settings:
        try:
            return max(0, int(provider_settings.get(key)))
        except (TypeError, ValueError):
            return default
    return int_env(env_name, default)


def load_slskd_config(path: str) -> dict:
    if not path:
        return {}
    try:
        with Path(path).open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except OSError:
        return {}
    except yaml.YAMLError:
        return {}


def configured_api_key(config: dict) -> str:
    explicit = str(os.environ.get("INKDROP_SLSKD_API_KEY") or "").strip()
    if explicit:
        return explicit
    auth = ((config.get("web") or {}).get("authentication") or {})
    api_keys = auth.get("api_keys") or {}
    if isinstance(api_keys, dict):
        for entry in api_keys.values():
            if isinstance(entry, dict):
                key = str(entry.get("key") or "").strip()
                if key:
                    return key
    return ""


def configured_base_url() -> str:
    return str(os.environ.get("INKDROP_SLSKD_API_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")


def api_request(base_url: str, api_key: str, path: str, *, method: str = "GET", timeout: int = 20):
    request = urllib.request.Request(
        base_url + path,
        headers={"X-API-Key": api_key},
        method=method,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
        if not body:
            return None
        return json.loads(body.decode("utf-8"))


def completed_searches(searches: list[dict], *, min_age_seconds: int) -> list[dict]:
    cutoff = time.time() - max(0, min_age_seconds)
    out: list[dict] = []
    for search in searches:
        if not isinstance(search, dict):
            continue
        ended_at = str(search.get("endedAt") or "").strip()
        if not (search.get("isComplete") or ended_at):
            continue
        if min_age_seconds and ended_at:
            try:
                parsed = ended_at.replace("Z", "+00:00")
                ended_epoch = __import__("datetime").datetime.fromisoformat(parsed).timestamp()
            except Exception:
                ended_epoch = 0
            if ended_epoch > cutoff:
                continue
        out.append(search)
    out.sort(key=lambda item: str(item.get("endedAt") or ""))
    return out


def cleanup_searches(
    *,
    keep: int | None,
    max_delete: int | None,
    min_age_minutes: int | None,
    dry_run: bool,
    force: bool = False,
) -> dict:
    provider_settings = load_provider_settings("slskd")
    if not force and not cleanup_enabled(provider_settings):
        return {
            "ok": True,
            "skipped": True,
            "reason": "slskd_search_history_cleanup_disabled",
            "state_db_path": str(state_db_path()),
        }
    keep = setting_int(
        provider_settings,
        "search_history_keep",
        "INKDROP_SLSKD_SEARCH_HISTORY_KEEP",
        100,
    ) if keep is None else keep
    max_delete = setting_int(
        provider_settings,
        "search_history_max_delete",
        "INKDROP_SLSKD_SEARCH_HISTORY_MAX_DELETE",
        5000,
    ) if max_delete is None else max_delete
    min_age_minutes = setting_int(
        provider_settings,
        "search_history_min_age_minutes",
        "INKDROP_SLSKD_SEARCH_HISTORY_MIN_AGE_MINUTES",
        30,
    ) if min_age_minutes is None else min_age_minutes
    config_path = str(os.environ.get("INKDROP_SLSKD_CONFIG") or "/config/slskd/slskd.yml")
    config = load_slskd_config(config_path)
    api_key = configured_api_key(config)
    base_url = configured_base_url()
    if not api_key:
        return {
            "ok": True,
            "skipped": True,
            "reason": "slskd_api_key_unconfigured",
            "base_url": base_url,
            "config_path": config_path,
        }
    searches = api_request(base_url, api_key, "/searches", timeout=60) or []
    if not isinstance(searches, list):
        searches = []
    candidates = completed_searches(searches, min_age_seconds=max(0, min_age_minutes) * 60)
    keep = max(0, keep)
    delete_candidates = candidates[:-keep] if keep and len(candidates) > keep else ([] if keep else candidates)
    if max_delete:
        delete_candidates = delete_candidates[: max(0, max_delete)]
    deleted = 0
    errors: list[dict] = []
    for search in delete_candidates:
        search_id = str(search.get("id") or "").strip()
        if not search_id:
            continue
        if dry_run:
            deleted += 1
            continue
        path = "/searches/" + urllib.parse.quote(search_id, safe="")
        try:
            api_request(base_url, api_key, path, method="DELETE", timeout=15)
            deleted += 1
        except (OSError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
            errors.append(
                {
                    "id": search_id,
                    "search_text": str(search.get("searchText") or "")[:120],
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            if len(errors) >= 20:
                break
    remaining = None
    if not dry_run:
        try:
            remaining_payload = api_request(base_url, api_key, "/searches", timeout=60) or []
            remaining = len(remaining_payload) if isinstance(remaining_payload, list) else None
        except Exception:
            remaining = None
    return {
        "ok": not errors,
        "skipped": False,
        "enabled_by": "forced" if force else "provider_config_or_env",
        "dry_run": dry_run,
        "before_total": len(searches),
        "before_completed_eligible": len(candidates),
        "keep_newest_completed": keep,
        "max_delete": max_delete,
        "min_age_minutes": min_age_minutes,
        "delete_target": len(delete_candidates),
        "deleted": deleted,
        "remaining_total": remaining,
        "error_count": len(errors),
        "errors": errors[:5],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", type=int, default=None)
    parser.add_argument("--max-delete", type=int, default=None)
    parser.add_argument("--min-age-minutes", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="Run even when the InkDrop SLSKD cleanup setting is disabled.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = cleanup_searches(
        keep=args.keep,
        max_delete=args.max_delete,
        min_age_minutes=args.min_age_minutes,
        dry_run=bool(args.dry_run),
        force=bool(args.force),
    )
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(
            "SLSKD search cleanup: "
            f"deleted={result.get('deleted', 0)} remaining={result.get('remaining_total')} "
            f"skipped={result.get('skipped', False)} errors={result.get('error_count', 0)}"
        )
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
