#!/usr/bin/env python3
"""Prove probe_item() never lets a real automatic SLSKD query wait less than the
real-world floor a live search actually needs.

Context: a live production search for "League of Extraordinary Gentlemen" returned
zero peer responses through 40 seconds, then 134 peers / 1998 files all at once at
45 seconds -- while the automatic pipeline's configured wait_seconds ceiling was 30s
(clamped even lower internally by an older 25s floor). That gap silently reported
"no candidates found" for titles whose real matches simply hadn't arrived yet. This
is not query-specific: a comparably obscure search can legitimately need 40+ seconds
for the Soulseek network to fan out and respond, even though a popular title (Akira,
confirmed live: 250 peers within 5 seconds) answers almost immediately.

The fix raises probe_item()'s internal floor from 25s to 50s. This test proves that
floor is enforced regardless of what a low configured wait_seconds value requests --
and separately proves it costs nothing for a fast query, since the existing
quiet-period early exit in slskd_search() (unit-tested elsewhere) already stops
polling once results stop growing, independent of how high the ceiling is.
"""

from unittest import mock

import inkdrop_slskd_source_probe as probe


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def wanted_item(review_id="loeg-2"):
    return {
        "review_id": review_id,
        "series": "The League of Extraordinary Gentlemen",
        "query": "The League of Extraordinary Gentlemen",
        "issue": "2",
        "publisher": "America's Best Comics",
        "watch_publisher": "America's Best Comics",
        "year": "1999",
        "autopilot_queue": True,
        "autopilot_state": "queued",
    }


captured_wait_seconds = []


def fake_slskd_search(query, wait_seconds=8, deadline=None, **_options):
    # Takes whatever else probe_item passes (a reuse window, today). probe_item
    # runs each query under a broad except that records the failure as a query
    # error and moves on, so a double that rejects a new argument does not raise
    # here -- it just stops recording, and the only thing that notices is the
    # call-count check below. Keep that check.
    captured_wait_seconds.append(wait_seconds)
    return []


with (
    mock.patch.object(probe, "slskd_search", side_effect=fake_slskd_search),
    mock.patch.object(probe, "detected_staged_files", return_value=[]),
):
    # A caller that requests a very low wait (matching what the previously-capped
    # settings ceiling of 30s, or the even older 8s default, would have passed)
    # must still result in a real search that waits at least the live-verified floor.
    probe.probe_item(wanted_item(), max_queries=1, wait_seconds=2)

require(len(captured_wait_seconds) >= 1, "slskd_search should have been called at least once")
for observed in captured_wait_seconds:
    require(observed >= 50, f"probe_item must floor every automatic SLSKD query to at least 50s, got {observed}")

captured_wait_seconds.clear()
with (
    mock.patch.object(probe, "slskd_search", side_effect=fake_slskd_search),
    mock.patch.object(probe, "detected_staged_files", return_value=[]),
):
    # A caller that explicitly configures a longer wait than the floor must not be
    # clamped back down to the floor -- the floor is a minimum, not a ceiling.
    probe.probe_item(wanted_item(), max_queries=1, wait_seconds=90)

for observed in captured_wait_seconds:
    require(observed == 90, f"an explicit longer wait_seconds must be honored, not clamped to the floor: got {observed}")

# The settings-layer ceiling that used to clamp any configured wait_seconds down to
# 30 (silently undoing a user's or this fix's intent) must now allow at least 60.
clamped_low = probe.int_setting({"wait_seconds": 5}, "wait_seconds", 8, 2, 60)
clamped_default = probe.int_setting({}, "wait_seconds", 8, 2, 60)
clamped_high = probe.int_setting({"wait_seconds": 999}, "wait_seconds", 8, 2, 60)
require(clamped_low == 5, clamped_low)
require(clamped_default == 8, clamped_default)
require(clamped_high == 60, f"the settings ceiling must allow at least 60s now, not clamp back to 30: got {clamped_high}")

print("inkdrop-slskd-wait-seconds-floor-smoke: all checks passed")
