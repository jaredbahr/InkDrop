#!/usr/bin/env python3
import json
import re
import subprocess
import tempfile
from pathlib import Path

from core import inkdrop_version


ROOT = Path(__file__).resolve().parents[1]
catalog_path = ROOT / "web/static/js/inkdrop-version-about.js"
catalog = catalog_path.read_text(encoding="utf-8")
workflow = (ROOT / ".github/workflows/inkdrop-public-release.yml").read_text(encoding="utf-8")
release_contract = json.loads((ROOT / "docs/inkdrop/releases/current.json").read_text(encoding="utf-8"))
current_notes = (ROOT / release_contract["notes_path"]).read_text(encoding="utf-8")

limits_match = re.search(r"var RELEASE_LIMITS = Object\.freeze\(\{(.*?)\}\);", catalog, re.DOTALL)
assert limits_match, "RELEASE_LIMITS was not found"
limits_body = limits_match.group(1)


def release_limit(name: str) -> int:
    """Read a limit out of the loader's own RELEASE_LIMITS, never a copy of it.

    Every boundary fixture below is built from this, so the smoke and the
    loader cannot drift apart: change a number in the JavaScript and the
    fixtures move with it.
    """
    match = re.search(rf"\b{name}:\s*(\d+)\b", limits_body)
    assert match, f"RELEASE_LIMITS.{name} was not found"
    return int(match.group(1))


summary_limit = release_limit("summary")


def assert_summary_length(summary: str) -> None:
    javascript_length = len(summary.encode("utf-16-le")) // 2
    assert javascript_length <= summary_limit, (
        f"release summary is {javascript_length} UTF-16 code units; "
        f"loader limit is {summary_limit}"
    )


def run_catalog_loader(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "node",
            "-e",
            (
                f"require({json.dumps(str(path))});"
                "process.stdout.write(JSON.stringify(globalThis.InkDropVersionAbout.publicReleases));"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def catalog_with_string_field(field: str, value: str) -> str:
    """Rewrite the first release entry's `field` string, leaving the rest alone."""
    updated, replacements = re.subn(
        rf'(\b{field}:\s*)"(?:[^"\\]|\\.)*"',
        lambda match: match.group(1) + json.dumps(value),
        catalog,
        count=1,
    )
    assert replacements == 1, f"About release {field} fixture was not found"
    return updated


def catalog_with_highlight(highlight: str) -> str:
    """Rewrite the first release entry's first highlight string."""
    updated, replacements = re.subn(
        r'(highlights:\s*\[\s*\n\s*)"(?:[^"\\]|\\.)*"',
        lambda match: match.group(1) + json.dumps(highlight),
        catalog,
        count=1,
    )
    assert replacements == 1, "About release highlight fixture was not found"
    return updated


def catalog_with_highlight_count(count: int) -> str:
    """Replace the first release entry's highlight list with `count` entries."""
    body = "".join(f'        "highlight {index}",\n' for index in range(count))
    updated, replacements = re.subn(
        r"(highlights:\s*\[\n)(.*?)(\n\s*\],)",
        lambda match: match.group(1) + body.rstrip("\n") + match.group(3),
        catalog,
        count=1,
        flags=re.DOTALL,
    )
    assert replacements == 1, "About release highlight-count fixture was not found"
    return updated


def boundary_fixtures(field: str) -> tuple[int, str, str]:
    """Return (limit, catalog at the limit, catalog one past the limit)."""
    limit = release_limit(field)
    if field == "highlights":
        return limit, catalog_with_highlight_count(limit), catalog_with_highlight_count(limit + 1)
    if field == "highlight":
        return limit, catalog_with_highlight("x" * limit), catalog_with_highlight("x" * (limit + 1))
    return limit, catalog_with_string_field(field, "x" * limit), catalog_with_string_field(field, "x" * (limit + 1))


loader = run_catalog_loader(catalog_path)
assert loader.returncode == 0, (
    "About release catalog failed its production loader: "
    f"{(loader.stderr or loader.stdout).strip()}"
)
loaded_releases = json.loads(loader.stdout)
for release in loaded_releases:
    assert_summary_length(str(release["summary"]))

# Exercise the production loader itself at both sides of each exact boundary.
#
# Loading the shipped catalog above already fails CI on any over-limit field,
# but only for the fields the shipped catalog happens to be near. It cannot
# tell a limit that is enforced from one that has quietly stopped being
# enforced: delete the title, highlight-count or highlight-length check from
# publicRelease() and the shipped catalog still loads clean. These fixtures
# are the check that could have failed for each of them -- every limit the
# loader claims to enforce gets a refusal proved against it, not just the one
# that happened to bite while cutting a release.
FIELD_BOUNDARIES = (
    ("summary", "Error: Release summary is too long"),
    ("title", "Error: Release title is too long"),
    ("highlights", "Error: Release highlights are out of bounds"),
    ("highlight", "Error: Release highlight is out of bounds"),
)
with tempfile.TemporaryDirectory(prefix="inkdrop-release-limits-") as temp_dir:
    for field, expected_error in FIELD_BOUNDARIES:
        limit, at_limit_catalog, over_limit_catalog = boundary_fixtures(field)

        boundary_path = Path(temp_dir) / f"{field}-boundary-catalog.js"
        boundary_path.write_text(at_limit_catalog, encoding="utf-8")
        boundary_loader = run_catalog_loader(boundary_path)
        assert boundary_loader.returncode == 0, (
            f"About release loader refused a {field} at its exact limit ({limit}): "
            f"{(boundary_loader.stderr or boundary_loader.stdout).strip()}"
        )

        over_limit_path = Path(temp_dir) / f"{field}-over-limit-catalog.js"
        over_limit_path.write_text(over_limit_catalog, encoding="utf-8")
        over_limit_loader = run_catalog_loader(over_limit_path)
        assert over_limit_loader.returncode != 0, (
            f"About release loader accepted a {field} over its limit ({limit + 1} > {limit})"
        )
        assert expected_error in over_limit_loader.stderr, over_limit_loader.stderr

# Pin the Python assertion boundary as a separate reporting guard.
assert_summary_length("x" * summary_limit)
try:
    assert_summary_length("x" * (summary_limit + 1))
except AssertionError:
    pass
else:
    raise AssertionError("release summary over the loader limit was accepted")

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
