#!/usr/bin/env python3
"""Prove INKDROP_SLSKD_INCOMPLETE_ROOT actually moves the incomplete path.

Reported against the public repo: an operator sets the variable in
docker-compose to keep in-progress transfers on an SSD while completed
downloads land on an HDD, and gets no error, no warning, and no effect. The
path constants were wired to the env var on 2026-08-13, but the probe still
re-derived the path from download_root every run inside
load_slskd_provider_settings(), so apply_slskd_provider_settings() overwrote
the correctly-resolved global and the variable stayed inert at runtime.

Every check below runs in a child process: these are import-time constants, so
a single interpreter cannot observe two different environments.

The three resolution layers this locks down:
  * explicit stored setting wins  -- per-instance config still overrides
  * INKDROP_SLSKD_INCOMPLETE_ROOT -- honoured when nothing explicit is stored
  * download_root/incomplete      -- unchanged fallback when the var is unset
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

HDD = "/mnt/hdd/slskd"
SSD = "/mnt/ssd/slskd-incomplete"

# Resolves the constants and every load_slskd_provider_settings() branch, then
# reports them as JSON. Runs in a child so the env is fixed before import.
CHILD = r"""
import json, os, sys
from unittest import mock

sys.path.insert(0, os.environ["INKDROP_SMOKE_REPO"])

from core import inkdrop_web_config as web_config
from core import inkdrop_slskd_source_probe as probe

routing = probe.inkdrop_download_client_routing
out = {
    "web_config_constant": str(web_config.SLSKD_INCOMPLETE_ROOT),
    "probe_constant": str(probe.SLSKD_INCOMPLETE_ROOT),
}

def settings(instance, config):
    with mock.patch.object(routing, "slskd_source_instance", return_value=instance), \
         mock.patch.object(probe, "provider_config", return_value=config):
        return probe.load_slskd_provider_settings()

enabled = {"enabled": True, "settings": {}}
out["registry_bare"] = settings(None, enabled)["incomplete_root"]

with_root = {"enabled": True, "settings": {"download_root": os.environ["INKDROP_SMOKE_STORED_ROOT"]}}
out["registry_download_root"] = settings(None, with_root)["incomplete_root"]

explicit = {"enabled": True, "settings": {"incomplete_root": os.environ["INKDROP_SMOKE_EXPLICIT"]}}
out["registry_explicit"] = settings(None, explicit)["incomplete_root"]

instance = {
    "download_client_instance_id": 1,
    "instance": {
        "base_url": "http://slskd:5030",
        "settings": {},
        "download_paths": {"comics": os.environ["INKDROP_SMOKE_STORED_ROOT"]},
    },
}
out["instance_bare"] = settings(instance, None)["incomplete_root"]

instance_explicit = json.loads(json.dumps(instance))
instance_explicit["instance"]["settings"] = {"incomplete_root": os.environ["INKDROP_SMOKE_EXPLICIT"]}
out["instance_explicit"] = settings(instance_explicit, None)["incomplete_root"]

# apply_slskd_provider_settings() is what every probe run calls; it reassigns
# the module global and is where the env var used to get thrown away.
with mock.patch.object(routing, "slskd_source_instance", return_value=None), \
     mock.patch.object(probe, "provider_config", return_value=enabled):
    probe.apply_slskd_provider_settings()
out["probe_constant_after_apply"] = str(probe.SLSKD_INCOMPLETE_ROOT)

print(json.dumps(out))
"""


def resolve(incomplete_env, *, stored_root=HDD, explicit="/mnt/nvme/explicit"):
    """Import the modules in a child process under a controlled environment."""

    with tempfile.TemporaryDirectory() as temp_dir:
        env = dict(os.environ)
        # Never let a smoke run touch real application state.
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
        env["INKDROP_SMOKE_STORED_ROOT"] = stored_root
        env["INKDROP_SMOKE_EXPLICIT"] = explicit
        env["INKDROP_SLSKD_DOWNLOAD_ROOT"] = HDD
        env["INKDROP_SLSKD_API_BASE_URL"] = "http://slskd:5030"
        env.pop("INKDROP_SLSKD_INCOMPLETE_ROOT", None)
        if incomplete_env is not None:
            env["INKDROP_SLSKD_INCOMPLETE_ROOT"] = incomplete_env

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
    """Compare paths without tripping over separator style on Windows."""

    return str(left).replace("\\", "/").rstrip("/") == str(right).replace("\\", "/").rstrip("/")


def main():
    derived = f"{HDD}/incomplete"

    # --- env var SET: it must reach every layer -------------------------
    # This is the whole bug. Before the fix registry_bare, instance_bare and
    # probe_constant_after_apply all came back as HDD/incomplete.
    on = resolve(SSD)
    for field in (
        "web_config_constant",
        "probe_constant",
        "registry_bare",
        "registry_download_root",
        "instance_bare",
        "probe_constant_after_apply",
    ):
        assert same_path(on[field], SSD), (
            f"INKDROP_SLSKD_INCOMPLETE_ROOT ignored at {field}: got {on[field]!r}, wanted {SSD!r}"
        )

    # The variable has to be independent of the download root, not a suffix of
    # it -- the separate-mount case is the entire reason it exists.
    assert not same_path(on["probe_constant"], derived), on
    assert not str(on["probe_constant"]).replace("\\", "/").startswith(HDD + "/"), (
        f"incomplete root is still nested under the download root: {on['probe_constant']!r}"
    )

    # An explicitly stored setting still outranks the env var, so a
    # per-instance download-client path keeps overriding the global.
    for field in ("registry_explicit", "instance_explicit"):
        assert same_path(on[field], "/mnt/nvme/explicit"), (
            f"stored setting should outrank the env var at {field}: {on[field]!r}"
        )

    # --- env var UNSET: the old derivation must survive untouched -------
    off = resolve(None)
    for field in ("web_config_constant", "probe_constant", "registry_bare", "probe_constant_after_apply"):
        assert same_path(off[field], derived), (
            f"unset env var should derive from the download root at {field}: {off[field]!r}"
        )

    # A stored download_root still pairs its own incomplete folder when no env
    # var is set -- fixing the env path must not repoint existing installs.
    custom = resolve(None, stored_root="/mnt/other/slskd")
    assert same_path(custom["registry_download_root"], "/mnt/other/slskd/incomplete"), custom
    assert same_path(custom["instance_bare"], "/mnt/other/slskd/incomplete"), custom

    # --- empty string is not a configuration ----------------------------
    # docker-compose renders an unset ${VAR:-} as "", which must mean "unset".
    blank = resolve("")
    assert same_path(blank["probe_constant"], derived), (
        f"blank env var should fall back to the derived path: {blank['probe_constant']!r}"
    )
    assert same_path(blank["probe_constant_after_apply"], derived), blank

    print("slskd incomplete-root env smoke: ok")


if __name__ == "__main__":
    main()
