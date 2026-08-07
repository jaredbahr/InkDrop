#!/usr/bin/env python3
"""Only a changed query plan may bypass cooldown / reset query rotation.

Found against live production data (Wild Strawberry,
finding 8 in the SLSKD miss audit): should_skip_cache() was called with
force=bool(refresh_reason), and derive_query_offset() had a blanket
`if refresh_reason: return 0`. Both treated EVERY refresh reason the same
as "query_plan_changed" -- so a periodic candidate_recheck (has cached
candidates, nothing passed matching, recheck every 20 minutes) bypassed the
45-minute cooldown_hours setting entirely and restarted query rotation at
the anchor query every single recheck, hot-looping the same first few
queries instead of respecting the configured cadence or making rotation
progress through the rest of the query list.
"""

import time

from core import inkdrop_slskd_source_probe as probe


def require(condition, message):
    if not condition:
        raise AssertionError(message)


now = time.time()

# --- cooldown bypass -------------------------------------------------
# A recently-checked entry with a non-"query_plan_changed" refresh reason
# must still respect cooldown_hours.
recent_entry = {"status": "available", "checked_at": now - 60, "candidates": []}
for reason in ("candidate_recheck", "retry_probe_error", "upgrade_autograb_verdicts", "schema_upgrade"):
    skip = probe.should_skip_cache(recent_entry, cooldown_hours=1.0, force=(reason == "query_plan_changed"))
    require(skip, f"refresh_reason={reason!r} must not bypass a live cooldown")

# query_plan_changed is the one reason that legitimately invalidates the
# cached cooldown -- callers pass force=True for it explicitly.
skip = probe.should_skip_cache(recent_entry, cooldown_hours=1.0, force=True)
require(not skip, "query_plan_changed must still bypass cooldown")

# --- rotation reset ----------------------------------------------------
queries = ["Wild Strawberry", "Wild Strawberry manga", "Wild Strawberry 1", "Wild Strawberry comics"]
cache_entry = {
    "status": "searched_no_candidates",
    "query_signature": probe.query_signature(queries),
    "next_query_offset": 2,
}
for reason in ("candidate_recheck", "retry_probe_error", "upgrade_rejection_summary", "schema_upgrade"):
    offset = probe.derive_query_offset(queries, cache_entry, refresh_reason=reason)
    require(
        offset == 2,
        f"refresh_reason={reason!r} reset rotation to {offset} instead of continuing from next_query_offset=2",
    )

# query_plan_changed still resets to the anchor -- the stored offset was
# computed against a query list that no longer applies.
offset = probe.derive_query_offset(queries, cache_entry, refresh_reason="query_plan_changed")
require(offset == 0, f"query_plan_changed must still reset rotation to 0, got {offset}")

print("inkdrop-slskd-refresh-reason-cooldown-smoke: ok")
