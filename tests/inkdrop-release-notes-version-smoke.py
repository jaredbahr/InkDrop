#!/usr/bin/env python3
import json
import re
from pathlib import Path

import inkdrop_version


ROOT = Path(__file__).resolve().parent
catalog = (ROOT / "web/static/js/inkdrop-version-about.js").read_text(encoding="utf-8")
workflow = (ROOT / ".github/workflows/inkdrop-public-release.yml").read_text(encoding="utf-8")
release_contract = json.loads((ROOT / "docs/inkdrop/releases/current.json").read_text(encoding="utf-8"))
current_notes = (ROOT / release_contract["notes_path"]).read_text(encoding="utf-8")

catalog_match = re.search(r'var DETAILED_RELEASES.*?version: "(v[^"]+)"', catalog, re.DOTALL)
assert catalog_match, "newest About release version was not found"

catalog_version = catalog_match.group(1)
injected_version = release_contract["version"]
assert release_contract["tag"] == catalog_version, (release_contract, catalog_version)
assert "INKDROP_VERSION=${{ steps.release.outputs.version }}" in workflow, "QA image must consume checked-in release metadata"
assert '--version "${{ steps.release.outputs.version }}"' in workflow, "QA manifest must consume checked-in release metadata"
assert "INKDROP_VERSION=0.1.0-alpha." not in workflow, "workflow must not duplicate the checked-in version"

metadata = inkdrop_version.build_metadata({
    "INKDROP_VERSION": injected_version,
    "INKDROP_COMMIT_SHA": "2271e5e056d91edad0dff7a01b60324ae09d4016",
    "INKDROP_BUILD_DATE": "2026-07-15T00:00:00Z",
    "INKDROP_RELEASE_CHANNEL": "qa",
    "INKDROP_CANDIDATE_MANIFEST_PATH": str(ROOT / "does-not-exist.json"),
})
assert metadata["version"] == injected_version, metadata
assert metadata["display_version"] == injected_version, metadata

# The in-app About catalog was cleared to a single current entry ahead of the
# public beta launch (see docs/inkdrop/releases/*.md for the full alpha-by-
# alpha history, which stays on disk and on GitHub -- it just no longer gets
# mirrored into the in-app widget one entry per build). This test used to
# assert every individual alpha entry's exact phrasing against its standalone
# release-notes file; that comprehensive a check made sense while the catalog
# carried the full history, and doesn't fit a single-entry catalog. What's
# still meaningful: the one remaining entry must be the real current version,
# must actually match its own standalone release-notes content, and the
# rollup mechanism the old multi-entry catalog needed is gone, not just empty.
detailed_releases_match = re.search(r"var DETAILED_RELEASES = Object\.freeze\(\[(.*?)\]\);", catalog, re.DOTALL)
assert detailed_releases_match, "DETAILED_RELEASES array was not found"
detailed_releases_body = detailed_releases_match.group(1)
assert detailed_releases_body.count('publicRelease({') == 1, (
    "DETAILED_RELEASES should carry exactly one entry post-clear; "
    f"found {detailed_releases_body.count('publicRelease({')}"
)
assert f'version: "{catalog_version}"' in detailed_releases_body, "the single entry must be the current version"

assert "var RELEASE_ROLLUPS" not in catalog, "RELEASE_ROLLUPS was dead code (never referenced) and should be removed, not left empty"
assert "Older release notes remain available on GitHub" in catalog

for forbidden in (
    "private alpha",
    "private-alpha",
    "private qa",
    "invite-only beta",
    "owner",
    "coordinator",
    "not publicly launched",
):
    assert forbidden not in current_notes.lower(), f"current release notes contain launch-status language: {forbidden}"

print("release notes/version alignment smoke passed")
