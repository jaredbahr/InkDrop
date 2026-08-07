#!/usr/bin/env python3
"""Regression for the notifications wiring added to the two 'Wanted cleared'
settlement paths and the shared 'append_manual_review' choke point.

Proves the wiring actually calls into inkdrop_notifications with the right
event data -- not just that the settlement/review logic still runs. Uses
targeted mocking at the settlement boundary (verified_import_for_queue /
mark_queue_verified_for_import) rather than a full on-disk import fixture,
since the notification call itself is what's under test here, not import
verification.
"""

import tempfile
import time
from pathlib import Path
from unittest import mock

from core import inkdrop_state
from core import inkdrop_completed_import
from core import inkdrop_pack_import
from core import inkdrop_notifications
from core import inkdrop_notification_store as store


def require(condition, message):
    if not condition:
        raise AssertionError(message)


NOW = 1784376000.0

with tempfile.TemporaryDirectory() as temp_dir:
    db_path = Path(temp_dir) / "state.sqlite3"
    with inkdrop_state.connect(db_path) as con:
        inkdrop_state.init_schema(con)
        con.execute(
            "insert into series(id,title,media_type,metadata_provider,metadata_id,source,monitored,monitor_new,auto_grab,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("series-1", "Series One", "comic", "comicvine", "1", "comicvine", 1, 1, 1, NOW, NOW, "{}"),
        )
        con.execute(
            "insert into issues(id,series_id,issue_number,normalized_number,title,release_date,metadata_provider,metadata_id,monitored,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("issue-1", "series-1", "1", "001", "Issue 1", "2026-01-01", "comicvine", "cv-1", 1, NOW, NOW, "{}"),
        )
        con.execute(
            "insert into wanted_items(id,series_id,issue_id,reason,status,priority,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?)",
            ("wanted-1", "series-1", "issue-1", "missing", "wanted", 50, NOW, NOW, "{}"),
        )
        con.execute(
            "insert into queue_items(id,wanted_id,series_id,issue_id,state,current_source,active,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?,?)",
            ("queue-1", "wanted-1", "series-1", "issue-1", "downloading", "slskd", 1, NOW, NOW, "{}"),
        )
        # settle_queue_items_with_verified_imports pre-filters in SQL on a
        # real verified import_results row before ever calling
        # verified_import_for_queue -- the mocked return value below only
        # covers the per-row lookup, not this existence check.
        con.execute(
            "insert into import_results(id,queue_id,series_id,issue_id,source_path,dest_path,status,verified,created_at,raw_json) values(?,?,?,?,?,?,?,?,?,?)",
            ("import-1", "queue-1", "series-1", "issue-1", "/staging/issue1.cbz", "/library/Series One/Issue 1.cbz", "verified", 1, NOW, "{}"),
        )
        con.commit()

    # 1. settle_queue_items_with_verified_imports calls notify_wanted_cleared
    #    with the right series/issue/path data when a queue item settles.
    fake_verified_import = {
        "id": "import-1",
        "created_at": NOW,
        "dest_path": "/library/Series One/Issue 1.cbz",
        "source_path": "/staging/issue1.cbz",
    }
    with inkdrop_state.connect(db_path) as con:
        with mock.patch.object(inkdrop_state, "verified_import_for_queue", return_value=fake_verified_import), \
             mock.patch.object(inkdrop_state, "mark_queue_verified_for_import", return_value=True), \
             mock.patch.object(inkdrop_notifications, "notify_wanted_cleared") as notify_mock:
            settled = inkdrop_state.settle_queue_items_with_verified_imports(con, NOW)
        require(settled == 1, f"expected 1 settled row, got {settled}")
        require(notify_mock.call_count == 1, f"expected notify_wanted_cleared to be called once, got {notify_mock.call_count}")
        call_kwargs = notify_mock.call_args.kwargs
        require(call_kwargs.get("series") == "Series One", f"wrong series in notify call: {call_kwargs}")
        require(call_kwargs.get("dest_path") == fake_verified_import["dest_path"], f"wrong dest_path in notify call: {call_kwargs}")
        # Audit finding 2 (2026-08-06): series_id/issue_id were never threaded
        # through, silently defeating any channel's per-series filter for this
        # event type. Both are selected into `row` by the settlement query --
        # this fails if a future edit stops forwarding them.
        require(call_kwargs.get("series_id") == "series-1", f"series_id not threaded to notify_wanted_cleared: {call_kwargs}")
        require(call_kwargs.get("issue_id") == "issue-1", f"issue_id not threaded to notify_wanted_cleared: {call_kwargs}")

    # 2. A settlement failure in inkdrop_notifications must not block the
    #    transaction -- the try/except around the notify call is load-bearing.
    with inkdrop_state.connect(db_path) as con:
        with mock.patch.object(inkdrop_state, "verified_import_for_queue", return_value=fake_verified_import), \
             mock.patch.object(inkdrop_state, "mark_queue_verified_for_import", return_value=True), \
             mock.patch.object(inkdrop_notifications, "notify_wanted_cleared", side_effect=RuntimeError("boom")):
            settled = inkdrop_state.settle_queue_items_with_verified_imports(con, NOW)
        require(settled == 1, f"a notification failure must not block settlement, got settled={settled}")

    # 3. append_manual_review (inkdrop_completed_import) calls notify_manual_review
    #    when it actually persists a record. Points the write-time dedup store at
    #    this run's temp dir -- it lives under the real STATE_DIR by default, so a
    #    prior run on the same machine within the 6-hour dedup window would
    #    otherwise suppress this notify and fail the assertion below.
    review_file = Path(temp_dir) / "manual-review.jsonl"
    dedup_file = Path(temp_dir) / "manual-review-dedup.json"
    with mock.patch.object(inkdrop_notifications, "notify_manual_review") as notify_mock, \
         mock.patch.object(inkdrop_completed_import, "REVIEW_FILE", review_file), \
         mock.patch.object(inkdrop_completed_import, "MANUAL_REVIEW_DEDUP_FILE", dedup_file):
        inkdrop_completed_import.append_manual_review(
            "candidate_title_mismatch",
            {"series": "Court of Owls", "source": "slskd", "detail": "wrong edition"},
            db_path=str(db_path),
        )
    require(notify_mock.call_count == 1, "append_manual_review (completed_import) should notify")
    call_kwargs = notify_mock.call_args.kwargs
    require(call_kwargs.get("reason") == "candidate_title_mismatch", f"wrong reason: {call_kwargs}")
    require(call_kwargs.get("series") == "Court of Owls", f"wrong series: {call_kwargs}")

    # 4. append_manual_review (inkdrop_pack_import) calls notify_manual_review
    #    for a normal review reason... Same real-STATE_DIR-by-default trap as
    #    inkdrop_completed_import above -- pack_import writes MANUAL_REVIEW_FILE
    #    unconditionally (no dedup gate), so an unpatched run just bloats the
    #    real log on repeat invocations instead of failing outright.
    pack_review_file = Path(temp_dir) / "manual-review.jsonl"
    with mock.patch.object(inkdrop_notifications, "notify_manual_review") as notify_mock, \
         mock.patch.object(inkdrop_pack_import, "MANUAL_REVIEW_FILE", pack_review_file):
        inkdrop_pack_import.append_manual_review(
            "pack_ambiguous_issue",
            {"series": "Gods Lie", "source": "prowlarr"},
            db_path=str(db_path),
        )
    require(notify_mock.call_count == 1, "append_manual_review (pack_import) should notify for a persisted record")

    # ...but NOT for the pack_import_bad_archive short-circuit that skips
    # persisting entirely (the notify call sits after the write, matching it).
    with mock.patch.object(inkdrop_notifications, "notify_manual_review") as notify_mock, \
         mock.patch.object(inkdrop_pack_import, "MANUAL_REVIEW_FILE", pack_review_file):
        inkdrop_pack_import.append_manual_review(
            "pack_import_bad_archive",
            {"series": "Gods Lie"},
            db_path=str(db_path),
        )
    require(notify_mock.call_count == 0, "append_manual_review (pack_import) must not notify when the record isn't persisted")

    # 4b. Audit finding 1 (2026-08-06, confirmed live: 47 real manual-review
    #     events / 3 days, zero deliveries): every one of the 19 real call sites
    #     across both files omits db_path -- notify() silently no-ops on
    #     db_path=None with no exception, so this was never caught. Steps 3/4
    #     above pass db_path=str(db_path) explicitly, which is exactly how the
    #     bug stayed invisible: the test never exercised what a real call site
    #     (which never passes it) actually does. This calls append_manual_review
    #     exactly as all 19 production call sites do -- with db_path omitted --
    #     and fails if the fallback to INKDROP_STATE_DB regresses.
    with mock.patch.object(inkdrop_notifications, "notify_manual_review") as notify_mock, \
         mock.patch.object(inkdrop_completed_import, "REVIEW_FILE", review_file), \
         mock.patch.object(inkdrop_completed_import, "MANUAL_REVIEW_DEDUP_FILE", Path(temp_dir) / "manual-review-dedup-2.json"):
        inkdrop_completed_import.append_manual_review(
            "weak_filename_import_guard",
            {
                "source": "/inbox/x.cbz",
                "matched_series": "Amulet",
                "native_series_id": "series-1",
                "note": "test",
            },
        )
    require(notify_mock.call_count == 1, "append_manual_review (completed_import) with no db_path arg should still notify")
    call_args, call_kwargs = notify_mock.call_args
    require(
        call_args and call_args[0] == inkdrop_completed_import.INKDROP_STATE_DB,
        f"db_path must fall back to INKDROP_STATE_DB when the caller omits it (this is the exact production bug), got: {call_args}",
    )
    require(call_kwargs.get("series_id") == "series-1", f"series_id not extracted from payload['native_series_id']: {call_kwargs}")

    with mock.patch.object(inkdrop_notifications, "notify_manual_review") as notify_mock, \
         mock.patch.object(inkdrop_pack_import, "MANUAL_REVIEW_FILE", Path(temp_dir) / "manual-review-2.jsonl"):
        inkdrop_pack_import.append_manual_review(
            "pack_ambiguous_issue",
            {"series": "Gods Lie", "source": "prowlarr", "series_id": "series-1", "issue_id": "issue-1"},
        )
    require(notify_mock.call_count == 1, "append_manual_review (pack_import) with no db_path arg should still notify")
    call_args, call_kwargs = notify_mock.call_args
    require(
        call_args and call_args[0] == inkdrop_pack_import.INKDROP_STATE_DB,
        f"db_path must fall back to INKDROP_STATE_DB when the caller omits it (this is the exact production bug), got: {call_args}",
    )
    require(call_kwargs.get("series_id") == "series-1", f"series_id not extracted from payload: {call_kwargs}")
    require(call_kwargs.get("issue_id") == "issue-1", f"issue_id not extracted from payload: {call_kwargs}")

    # 5. test_all() -- the "Test" button's backend. Proves it actually sends (not just
    #    reports a guessed health state), reports per-connector not just an aggregate, and
    #    redacts a secret (the webhook URL itself) that leaks into an exception's text.
    #    Phase 1 (2026-08-06): test_all() now iterates real connector instances rather
    #    than the two fixed provider classes, so with zero connectors created there is
    #    nothing to test -- an empty list, not a placeholder row per type.
    class FakeResponse:
        def __init__(self, status_code, text="", body=None):
            self.status_code = status_code
            self.text = text
            self._body = body

        def json(self):
            if self._body is None:
                raise ValueError("no body")
            return self._body

    no_connectors = inkdrop_notifications.test_all(str(db_path))
    require(no_connectors == [], f"nothing configured yet -- expected no rows to test, got {no_connectors}")

    store.create_connector(
        db_path, connector_id="discord", type="discord", name="Discord",
        settings={"webhook_url": "https://discord.com/api/webhooks/123/super-secret-token"},
    )

    # Live discrepancy (2026-08-06): delivery rows marked "sent" with no
    # corresponding message ever appearing in the real Discord channel. Root
    # cause: send() treated any non-error HTTP status as success without
    # requesting delivery confirmation (Discord's webhook endpoint is
    # fire-and-forget unless wait=true is passed) or checking the response
    # body for an actual message id. This asserts the send request now asks
    # for that confirmation, and that a 2xx with no message id -- exactly the
    # silent-failure shape seen live -- is treated as a failure, not "sent".
    with mock.patch.object(inkdrop_notifications.requests, "post", return_value=FakeResponse(204)) as post:
        no_confirmation = inkdrop_notifications.test_all(str(db_path))
    require(post.call_args.kwargs.get("params") == {"wait": "true"}, f"Discord send must request wait=true: {post.call_args}")
    discord_unconfirmed = next(row for row in no_confirmation if row["id"] == "discord")
    require(
        discord_unconfirmed["sent"] is False,
        f"a 2xx response with no message id must NOT be treated as sent -- this is the exact live gap (delivery log said 'sent', channel never showed the message): {discord_unconfirmed}",
    )

    with mock.patch.object(inkdrop_notifications.requests, "post", return_value=FakeResponse(200, body={"id": "111222333444555666"})):
        sent_ok = inkdrop_notifications.test_all(str(db_path))
    discord_result = next(row for row in sent_ok if row["id"] == "discord")
    require(discord_result["configured"] is True and discord_result["sent"] is True, f"a real message id in the response should send: {discord_result}")
    require(len(sent_ok) == 1, f"pushover was never created as a connector, so it must not appear at all: {sent_ok}")

    # DiscordWebhookProvider.send() already catches requests.RequestException itself
    # and returns False, so a network-level failure never reaches test_all()'s own
    # try/except. That except is defensive-in-depth for anything send() doesn't catch
    # (a bug in a future provider, for example) -- exercise it directly so the
    # redaction on that path is proven, not just assumed dead code.
    def raise_with_secret_url(self, title, message, event_type):
        raise RuntimeError(
            "unexpected failure calling https://discord.com/api/webhooks/123/super-secret-token"
        )

    with mock.patch.object(inkdrop_notifications.DiscordWebhookProvider, "send", raise_with_secret_url):
        failed = inkdrop_notifications.test_all(str(db_path))
    discord_failed = next(row for row in failed if row["id"] == "discord")
    require(discord_failed["sent"] is False, f"a raised send error must report sent=False: {discord_failed}")
    require("super-secret-token" not in discord_failed["detail"], f"webhook URL must not leak into the test result: {discord_failed}")
    require("discord.com" not in discord_failed["detail"], f"the URL itself must be redacted, not just the token: {discord_failed}")

print("inkdrop-notifications-wiring-smoke: all checks passed")
