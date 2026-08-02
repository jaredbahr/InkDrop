#!/usr/bin/env python3
"""A retry-eligible queue row must not be starved behind an untried backlog.

Found against live production data: 629 queue rows sat
"queued" with an elapsed retry_after (SLSKD reservation expired, reconciled,
eligible for retry) for days -- some over a week -- despite the reconciliation
pass correctly resetting their state within minutes. queue_probe_priority()
sorted ascending on the raw retry_after epoch timestamp, so a never-attempted
row (retry_after unset -> 0) always outranked a previously-attempted row
whose cooldown had elapsed (retry_after ~= a large real epoch value). With
1435 untried rows and a 200-row per-pass selection cap, the 629 retry-eligible
rows never made the cut.
"""

import time

import inkdrop_slskd_source_probe as probe


def require(condition, message):
    if not condition:
        raise AssertionError(message)


now = time.time()

# A retry-eligible row: SLSKD reservation expired and was reconciled minutes
# ago, so retry_after is a real epoch timestamp already in the past.
retried_row = {
    "state": "queued",
    "current_source": None,
    "series": "Spawn",
    "issue": "103",
    "retry_after": now - 500,
    "last_event": "SLSKD candidate reservation expired; automatic retry scheduled",
}

# A large backlog of rows that have never been attempted at all.
untried_rows = [
    {
        "state": "queued",
        "current_source": None,
        "series": f"Untried Series {i}",
        "issue": "1",
        "retry_after": None,
    }
    for i in range(1500)
]

rows = untried_rows + [retried_row]
ranked = sorted(rows, key=probe.queue_probe_priority)
top_200 = ranked[:200]
require(
    retried_row in top_200,
    "a retry-eligible row with an elapsed retry_after was starved behind "
    "1500 never-attempted rows -- the priority sort must treat both as "
    "equally eligible-now, not rank raw epoch timestamps",
)

# A row whose cooldown has NOT elapsed yet must still sort after rows that
# are eligible right now (fresh or expired-and-retry-eligible alike).
not_yet_eligible_row = {
    "state": "queued",
    "current_source": None,
    "series": "Spawn",
    "issue": "106",
    "retry_after": now + 3600,
}
ranked_with_future = sorted([retried_row, not_yet_eligible_row] + untried_rows[:5], key=probe.queue_probe_priority)
require(
    ranked_with_future.index(not_yet_eligible_row) > ranked_with_future.index(retried_row),
    "a row not yet due for retry must not outrank a row that is eligible now",
)

print("inkdrop-slskd-queue-priority-starvation-smoke: ok")
