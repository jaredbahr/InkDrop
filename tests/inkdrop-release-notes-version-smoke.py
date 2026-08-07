#!/usr/bin/env python3
import json
import re
from pathlib import Path

from core import inkdrop_version


ROOT = Path(__file__).resolve().parents[1]
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

# The in-app About catalog accumulates a rolling history, newest release
# first, capped at DETAILED_RELEASE_LIMIT (10) -- GitHub is the record for
# anything older than that. It must gain an entry each release, not replace
# its one entry: that replace-only shape shipped every release from 0.1.02
# through 0.1.07 (root-caused and fixed 2026-08-05, see
# docs/inkdrop/public-release-process.md Step 1.4), because an earlier
# version of this exact assertion pinned "exactly one entry" as if it were
# permanent, when it was only ever meant to lock in a one-time purge of
# alpha-era entries carrying private/closed-alpha language (2026-07-31,
# db197018).
detailed_releases_match = re.search(r"var DETAILED_RELEASES = Object\.freeze\(\[(.*?)\]\);", catalog, re.DOTALL)
assert detailed_releases_match, "DETAILED_RELEASES array was not found"
detailed_releases_body = detailed_releases_match.group(1)
detailed_release_limit_match = re.search(r"var DETAILED_RELEASE_LIMIT = (\d+);", catalog)
assert detailed_release_limit_match, "DETAILED_RELEASE_LIMIT was not found"
detailed_release_limit = int(detailed_release_limit_match.group(1))
entry_versions = re.findall(r'publicRelease\(\{\s*version:\s*"(v[^"]+)"', detailed_releases_body)
assert entry_versions, "DETAILED_RELEASES has no entries"
assert len(entry_versions) <= detailed_release_limit, (
    f"DETAILED_RELEASES has {len(entry_versions)} entries, over its own DETAILED_RELEASE_LIMIT ({detailed_release_limit})"
)
assert len(entry_versions) == len(set(entry_versions)), f"DETAILED_RELEASES has duplicate versions: {entry_versions}"
assert entry_versions[0] == catalog_version, (
    f"the first (newest) DETAILED_RELEASES entry must be the current release; found {entry_versions[0]!r}, expected {catalog_version!r}"
)

assert "var RELEASE_ROLLUPS" not in catalog, "RELEASE_ROLLUPS was dead code (never referenced) and should be removed, not left empty"
assert "Older release notes remain available on GitHub" in catalog

forbidden_terms = (
    "private alpha",
    "private-alpha",
    "private qa",
    "invite-only beta",
    "owner",
    "coordinator",
    "not publicly launched",
)
for forbidden in forbidden_terms:
    assert forbidden not in current_notes.lower(), f"current release notes contain launch-status language: {forbidden}"
# The About catalog now carries every entry that gets shipped, not just the
# current one -- so the release entries need the same launch-status-language
# guard the current release's own notes file gets. Scoped to
# detailed_releases_body specifically, not the whole file: PRERELEASE_STAGES
# legitimately contains "not publicly launched" as the Channel-row label for
# genuinely alpha-tagged builds, which is product copy, not a release note.
for forbidden in forbidden_terms:
    assert forbidden not in detailed_releases_body.lower(), f"a DETAILED_RELEASES entry contains launch-status language: {forbidden}"

print("release notes/version alignment smoke passed")
