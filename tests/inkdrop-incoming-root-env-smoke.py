#!/usr/bin/env python3
"""Prove the comic/ebook incoming roots honour their env vars and settings.

Same failure shape as INKDROP_SLSKD_INCOMPLETE_ROOT: the module constants
resolved INKDROP_COMIC_INCOMING_ROOT at import, then
apply_path_provider_settings() -- which runs on every import pass, and on every
inkdrop_library_paths.current_paths() call -- reassigned COMIC_DEST to
comic_root/_Incoming and threw the value away. The path.comic_incoming_root and
path.ebook_incoming_root settings exposed in the UI were never read at all.

Runs in child processes because these are import-time constants.

Resolution order locked down here:
  * stored path.* setting  -- what the settings card writes
  * env var                -- INKDROP_{COMIC,EBOOK}_INCOMING_ROOT
  * comic_root/_Incoming   -- unchanged fallback, still tracks a moved root
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

COMIC_ROOT = "/library/comics"
COMIC_ENV = "/mnt/fast/incoming-comics"
EBOOK_ENV = "/mnt/fast/incoming-ebooks"

CHILD = r"""
import json, os, sys
from unittest import mock

sys.path.insert(0, os.environ["INKDROP_SMOKE_REPO"])
from core import inkdrop_completed_import as ci

out = {"import_comic": str(ci.COMIC_DEST), "import_ebook": str(ci.EBOOK_DEST)}

def apply(library_settings):
    cfg = {"settings": library_settings} if library_settings is not None else {}
    with mock.patch.object(ci, "provider_config", side_effect=lambda p: cfg if p == "library_paths" else {}):
        ci.apply_path_provider_settings()
    return str(ci.COMIC_DEST), str(ci.EBOOK_DEST)

out["applied_comic"], out["applied_ebook"] = apply({})
out["moved_root_comic"], _ = apply({"comic_root": os.environ["INKDROP_SMOKE_MOVED_ROOT"]})
stored = {
    "comic_incoming_root": os.environ["INKDROP_SMOKE_STORED_COMIC"],
    "ebook_incoming_root": os.environ["INKDROP_SMOKE_STORED_EBOOK"],
}
out["stored_comic"], out["stored_ebook"] = apply(stored)

print(json.dumps(out))
"""


def resolve(comic_env, ebook_env, *, moved_root="/mnt/other/comics"):
    with tempfile.TemporaryDirectory() as temp_dir:
        env = dict(os.environ)
        for key, rel in (
            ("INKDROP_CONFIG_DIR", "config"),
            ("INKDROP_STATE_DIR", "state"),
            ("INKDROP_LOCK_DIR", "state/locks"),
            ("INKDROP_LOG_DIR", "state/logs"),
            ("INKDROP_CACHE_DIR", "state/cache"),
            ("INKDROP_BACKUP_DIR", "state/backups"),
            ("INKDROP_STAGING_DIR", "staging"),
            ("INKDROP_MANUAL_INBOX_DIR", "manual-inbox"),
            ("INKDROP_QUARANTINE_DIR", "state/quarantine"),
        ):
            path = Path(temp_dir) / rel
            path.mkdir(parents=True, exist_ok=True)
            env[key] = str(path)

        env["INKDROP_SMOKE_REPO"] = str(REPO)
        env["INKDROP_SMOKE_MOVED_ROOT"] = moved_root
        env["INKDROP_SMOKE_STORED_COMIC"] = "/ui/set/comics"
        env["INKDROP_SMOKE_STORED_EBOOK"] = "/ui/set/ebooks"
        env["INKDROP_COMIC_ROOT"] = COMIC_ROOT
        env.pop("INKDROP_COMIC_INCOMING_ROOT", None)
        env.pop("INKDROP_EBOOK_INCOMING_ROOT", None)
        if comic_env is not None:
            env["INKDROP_COMIC_INCOMING_ROOT"] = comic_env
        if ebook_env is not None:
            env["INKDROP_EBOOK_INCOMING_ROOT"] = ebook_env

        done = subprocess.run(
            [sys.executable, "-B", "-c", CHILD],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(REPO),
            timeout=300,
        )
        if done.returncode != 0:
            raise AssertionError(f"child failed ({done.returncode}):\n{done.stdout}\n{done.stderr}")
        return json.loads(done.stdout.strip().splitlines()[-1])


def same_path(left, right):
    return str(left).replace("\\", "/").rstrip("/") == str(right).replace("\\", "/").rstrip("/")


def main():
    derived = f"{COMIC_ROOT}/_Incoming"

    # --- env vars SET ---------------------------------------------------
    # applied_* is the regression: it used to come back as the derived path
    # because apply_path_provider_settings() overwrote the import-time value.
    on = resolve(COMIC_ENV, EBOOK_ENV)
    for field in ("import_comic", "applied_comic"):
        assert same_path(on[field], COMIC_ENV), (
            f"INKDROP_COMIC_INCOMING_ROOT ignored at {field}: got {on[field]!r}, wanted {COMIC_ENV!r}"
        )
    for field in ("import_ebook", "applied_ebook"):
        assert same_path(on[field], EBOOK_ENV), (
            f"INKDROP_EBOOK_INCOMING_ROOT ignored at {field}: got {on[field]!r}, wanted {EBOOK_ENV!r}"
        )

    # The incoming root must be independent of the library root, which is the
    # point: it can live on another mount entirely.
    assert not same_path(on["applied_comic"], derived), on
    # An env-set incoming root must not follow comic_root around.
    assert same_path(on["moved_root_comic"], COMIC_ENV), (
        f"env var should outrank a moved comic_root: {on['moved_root_comic']!r}"
    )

    # A stored setting still outranks the env var.
    assert same_path(on["stored_comic"], "/ui/set/comics"), on
    assert same_path(on["stored_ebook"], "/ui/set/ebooks"), on

    # --- env vars UNSET: existing installs must not move ----------------
    off = resolve(None, None)
    for field in ("import_comic", "applied_comic"):
        assert same_path(off[field], derived), (
            f"unset env var should derive from the comic root at {field}: {off[field]!r}"
        )
    assert same_path(off["applied_ebook"], "/library/ebooks/_Incoming"), off
    # With no env var the incoming folder still tracks whatever comic_root is.
    assert same_path(off["moved_root_comic"], "/mnt/other/comics/_Incoming"), off
    # The settings card still wins with no env var set.
    assert same_path(off["stored_comic"], "/ui/set/comics"), off

    # --- blank is not a configuration -----------------------------------
    blank = resolve("", "")
    assert same_path(blank["applied_comic"], derived), blank
    assert same_path(blank["applied_ebook"], "/library/ebooks/_Incoming"), blank

    print("incoming-root env smoke: ok")


if __name__ == "__main__":
    main()
