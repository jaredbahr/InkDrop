#!/usr/bin/env python3
"""readiness_repair_issue_payload() must use issues.release_date, not the
cached raw_json blob's often-null "date" field, when repairing a stale
series-launch-year search query -- the same defect class PR #197 fixed in
issue_search_query(), reached through a different, untouched code path."""

import json

from core import inkdrop_state as state


def require(condition, message):
    if not condition:
        raise AssertionError(message)


# Exact real production shape: Old Man Logan #19 (comicvine:87624:issue:585089).
# release_date column is correct; raw_json's cached "date" is null and its
# cached searchQuery is already stale from a prior bad write.
issue_row = {
    "id": "comicvine:87624:issue:585089",
    "issue_number": "19",
    "title": "Gone Real Bad: Part I of II",
    "release_date": "2017-03-08",
    "metadata_provider": "comicvine",
    "metadata_id": "585089",
    "raw_json": json.dumps({"date": None, "searchQuery": "Old Man Logan 19 2016"}),
}
payload = state.readiness_repair_issue_payload(issue_row)
require(payload.get("date") == "2017-03-08", f"payload date must come from release_date column, got {payload}")

plan = state.issue_query_plan("Old Man Logan", payload, {"year": "2016"})
require(plan.get("repaired_reason") == "comic_issue_release_year", plan)
require("2017" in plan["query"] and "2016" not in plan["query"], plan)

# Negative control: without the release_date fallback, issue_payload_year()
# has nothing to compare against and the stale query is silently never
# corrected -- reproduces the pre-fix defect via git-stash-equivalent input.
buggy_payload = {
    "id": issue_row["metadata_id"],
    "issueNumber": issue_row["issue_number"],
    "date": None,
    "searchQuery": "Old Man Logan 19 2016",
}
buggy_plan = state.issue_query_plan("Old Man Logan", buggy_payload, {"year": "2016"})
require(buggy_plan.get("repaired_reason") != "comic_issue_release_year", buggy_plan)

# Two more real examples named in the audit, spot-checked the same way.
for series, issue_num, release_date, series_year, expected_year in (
    ("Gotham Central", "31", "2005-03-01", "2003", "2005"),
    ("Spawn", "24", "1994-06-01", "1992", "1994"),
):
    stale_query = f"{series} {issue_num} {series_year}"
    row = {
        "id": f"comicvine:x:issue:{issue_num}",
        "issue_number": issue_num,
        "title": "t",
        "release_date": release_date,
        "metadata_provider": "comicvine",
        "metadata_id": "x",
        "raw_json": json.dumps({"date": None, "searchQuery": stale_query}),
    }
    fixed_payload = state.readiness_repair_issue_payload(row)
    fixed_plan = state.issue_query_plan(series, fixed_payload, {"year": series_year})
    require(
        expected_year in fixed_plan["query"] and series_year not in fixed_plan["query"],
        f"{series} #{issue_num}: {fixed_plan}",
    )

print("inkdrop-readiness-repair-release-year-smoke: ok")
