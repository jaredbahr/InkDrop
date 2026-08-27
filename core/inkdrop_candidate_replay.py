"""Bounded, read-only replay of retained candidate refusals.

This answers exactly one question: for a candidate a provider once handed us
and InkDrop refused, what would the authority deployed *today* decide about
that same stored identity? It grabs nothing, queues nothing, schedules
nothing and writes nothing -- promoting a row that now passes is a separate,
explicitly invoked step, never a consequence of measuring.

Three rules hold the answer honest.

*The replayed candidate is the retained payload verbatim.* Not a subset
copied field by field, and never rebuilt from ``source_attempts.title`` or
``.query``, which record the search we sent rather than the release a peer
offered. A proxy that passes where the real row failed is worse than no
measurement at all, so if a refusal kept too little to judge, this module
reports that row as insufficient evidence instead of approximating it.

*The verdict comes from the production call that made it.* There is no replay
matcher. ``inkdrop_candidate_matching.candidate_compatibility`` and
``inkdrop_slskd_source_probe.candidate_identity_compatibility`` are the two
authorities, and a row is replayed through whichever one judged it. Writing a
second implementation to compare against the first is the defect this
codebase already carries fifteen instances of.

*A verdict is only compared against its own authority.* A row refused for
seeders, category or a client policy was never judged by target
compatibility, so compatibility having nothing to say about it is not that
row "now passing". Only rows that recorded a compatibility verdict of their
own are compared against one; the rest are reported as
``authority_not_recorded`` rather than folded into the headline. That label
is deliberately about the evidence, not the subsystem: most of those rows
carry refusal reasons compatibility does raise, and simply never kept the
verdict that would prove it decided them.

Feeding a retained payload back through ``candidate_compatibility`` is exact
rather than circular: that function reads only identity-bearing fields and
the target, re-deriving ``source_unit_evidence`` and every ``parsed_*`` key
from the release text on each call. It never consults ``block_reasons``,
``auto_grab_verdict`` or the stored ``target_compatibility``, so the old
decision cannot vote for itself. ``apply_compatibility`` is deliberately NOT
used here for the opposite reason: it unions the candidate's already-recorded
``block_reasons`` into its result, which would reproduce every historical
refusal by construction.

Compatibility is not the whole grab decision. Provider policy, download
client routing, size and the strict import verifier all still apply, and none
of them are replayed. "No longer refused by this authority" means exactly
that -- it does not mean a file would be acquired.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from collections import Counter, defaultdict
from pathlib import Path

from core import inkdrop_candidate_matching
from core import inkdrop_source_worker_coordinator
from core import inkdrop_state


CONTRACT_VERSION = 1

# The bucket whose rows this module exists to explain. Resolved by the
# production classifier rather than by a local re-implementation of it.
REJECTED_BUCKET = "candidates_rejected"

DEFAULT_STATUSES = ("wanted", "in_progress")

OUTCOME_INSUFFICIENT = "insufficient_evidence"
OUTCOME_STILL_REFUSED = "still_refused"
OUTCOME_NOW_REVIEW = "now_review"
OUTCOME_NO_LONGER_REFUSED = "no_longer_refused"

SCOPE_COMPATIBILITY = "compatibility"
SCOPE_OTHER_AUTHORITY = "other_authority"
SCOPE_AUTHORITY_NOT_RECORDED = "authority_not_recorded"

AUTHORITY_COMPATIBILITY = "candidate_compatibility"
AUTHORITY_SLSKD_IDENTITY = "slskd_candidate_identity_compatibility"

# How good the retained identity is, not how far the candidate got. A
# verdicted payload is the object a verdict was computed for; a sampled one is
# a projection of a provider row the search never built a candidate from.
# (The refusal record written going forward carries the separate
# considered/selected/executed axis -- see inkdrop_source_worker_runtime.)
EVIDENCE_VERDICTED = "verdicted"
EVIDENCE_SAMPLED = "sampled"

# Why a retained refusal could not be replayed. Each value names a distinct
# hole in what was kept, so the report can say which subsystem to fix rather
# than reporting one undifferentiated "not enough evidence" total.
INSUFFICIENT_NO_CANDIDATE = "no_retained_candidate_payload"
INSUFFICIENT_NO_IDENTITY_TEXT = "retained_candidate_has_no_identity_text"
INSUFFICIENT_NO_TARGET = "wanted_target_unavailable"
INSUFFICIENT_ONLY_CONSIDERED = "only_considered_samples_retained"


def _dict(value):
    return value if isinstance(value, dict) else {}


def _text(value):
    return str(value if value is not None else "").strip()


def _codes(value):
    out = []
    for code in value or []:
        code = _text(code)
        if code and code not in out:
            out.append(code)
    return out


def attempt_raw(raw_json):
    """The stored attempt payload, as a dict."""
    if isinstance(raw_json, dict):
        return raw_json
    return _dict(inkdrop_state.json_loads(raw_json or "{}", {}))


def retained_candidate(raw):
    """The candidate payload a verdict was actually computed for, or ``None``.

    Deliberately does not fall back to the attempt's own ``title``: that
    column holds the query for most providers, so treating it as a release
    name would replay something no peer ever offered.
    """
    candidate = _dict(attempt_raw(raw).get("raw")).get("candidate")
    return candidate if isinstance(candidate, dict) and candidate else None


def considered_samples(raw):
    """Release rows the search looked at but never built a verdict for.

    A search that ends with no usable candidate keeps a capped sample of the
    provider rows it saw. Each entry holds the provider's own release title,
    so it can be judged -- but it is a projection, not the candidate object
    the verdict helper would have built, and it is missing fields the
    authority reads. Replaying one answers a weaker question than replaying a
    verdicted payload, so it is kept in its own lane and never counted in the
    headline.
    """
    evidence = _dict(_dict(attempt_raw(raw).get("raw")).get("no_candidate_evidence"))
    return [
        sample
        for sample in (evidence.get("payload_samples") or [])
        if isinstance(sample, dict) and _text(sample.get("title"))
    ]


def considered_truncated(raw):
    """True when the search saw more results than it kept a sample of.

    Surfaced rather than swallowed: a capped sample that reads as complete
    coverage turns "we judged everything it saw" into a claim nobody checked.
    """
    evidence = _dict(_dict(attempt_raw(raw).get("raw")).get("no_candidate_evidence"))
    try:
        return int(evidence.get("payload_result_count") or 0) > int(evidence.get("payload_sample_count") or 0)
    except (TypeError, ValueError):
        return False


def retained_candidates(raw):
    """Every candidate identity the refusal kept, tagged by how far it got."""
    out = []
    candidate = retained_candidate(raw)
    if candidate is not None:
        out.append({"evidence": EVIDENCE_VERDICTED, "candidate": candidate, "identity_only": False})
    for sample in considered_samples(raw):
        out.append({"evidence": EVIDENCE_SAMPLED, "candidate": sample, "identity_only": True})
    return out


def stored_compatibility(raw):
    """The compatibility verdict this refusal recorded, if it recorded one."""
    return _dict(_dict(attempt_raw(raw).get("raw")).get("target_compatibility"))


def stored_refusal_record(raw):
    """The typed refusal record written by the acquisition path, if present.

    Rows decided before that record existed simply do not have one; every
    reader here falls back to the older evidence rather than treating its
    absence as an error.
    """
    return _dict(_dict(attempt_raw(raw).get("raw")).get("refusal"))


def stored_refusal_codes(raw, failure_reason=""):
    """Every refusal reason the row retained, not just the projected one.

    ``source_attempts.failure_reason`` is a single column and the acquisition
    path writes the first code into it, so a row refused for three reasons
    reads as one. Projecting that single reason is what repeatedly sent people
    to the wrong subsystem, so all of them are collected here.
    """
    raw = attempt_raw(raw)
    compatibility = stored_compatibility(raw)
    # The typed record leads when it exists: it was written to carry every
    # contributing reason, which is exactly what the older shapes drop.
    codes = _codes(stored_refusal_record(raw).get("reasons"))
    for code in _codes(compatibility.get("rejection_codes")):
        if code not in codes:
            codes.append(code)
    for extra in (
        raw.get("rejection_codes"),
        raw.get("block_reasons"),
        compatibility.get("review_codes"),
        raw.get("review_codes"),
        raw.get("review_reasons"),
    ):
        for code in _codes(extra):
            if code not in codes:
                codes.append(code)
    reason = _text(failure_reason) or _text(raw.get("failure_reason")) or _text(raw.get("reason"))
    if reason and reason not in codes:
        codes.append(reason)
    return codes


def decided_at(raw, started_at=None, completed_at=None):
    """When the refusal itself was decided.

    Never ``queue_items.updated_at`` / ``queue_updated_at``: the retry
    machinery touches those every pass, so they date the last piece of
    bookkeeping rather than the decision, and rows refused weeks ago read as
    decided moments ago. The attempt payload stamps its own ``ts`` when the
    verdict is built, which is the decision's own clock.
    """
    raw = attempt_raw(raw)
    for value in (
        stored_refusal_record(raw).get("decided_at"),
        raw.get("ts"),
        completed_at,
        started_at,
        raw.get("completed_at"),
        raw.get("started_at"),
    ):
        try:
            stamp = float(value)
        except (TypeError, ValueError):
            continue
        if stamp > 0 and math.isfinite(stamp):
            return stamp
    return None


def replay_authority(source):
    """Which production call judged this row.

    slskd composes its own verdict on top of the shared matcher -- it projects
    an authoritative leaf, then merges peer-level vetoes the leaf cannot see.
    Replaying an slskd refusal through the shared matcher alone would drop
    those vetoes and report a refusal as cleared when it was not.
    """
    return AUTHORITY_SLSKD_IDENTITY if _text(source).lower().startswith("slskd") else AUTHORITY_COMPATIBILITY


def _slskd_filename(candidate):
    for key in ("filename", "remote_filename", "path", "source_path", "original_result_title", "title"):
        value = _text(_dict(candidate).get(key))
        if value:
            return value
    return ""


def replay_compatibility(candidate, wanted_item, *, source="", settings=None):
    """Call the deployed authority for this row. No local matcher."""
    authority = replay_authority(source)
    if authority == AUTHORITY_SLSKD_IDENTITY:
        from core import inkdrop_slskd_source_probe

        # Returns (compatibility, identity_text); only the verdict is ours to
        # report. Unpacked rather than coerced, so a future signature change
        # raises here instead of quietly degrading to an empty verdict that
        # would read as "nothing refuses this any more".
        verdict, _identity_text = inkdrop_slskd_source_probe.candidate_identity_compatibility(
            candidate, _slskd_filename(candidate), wanted_item, settings=settings
        )
    else:
        verdict = inkdrop_candidate_matching.candidate_compatibility(candidate, wanted_item, settings=settings)
    if not isinstance(verdict, dict) or "rejection_codes" not in verdict:
        raise TypeError(f"{authority} did not return a compatibility verdict: {type(verdict).__name__}")
    return authority, verdict


def has_replayable_identity(candidate, wanted_item):
    """True when the retained payload still carries release text to judge.

    Asks the matcher's own normalizer rather than checking a local list of
    identity keys: if production's text extractor finds nothing in the
    payload, there is nothing for production's verdict to be about, and
    reporting that is the finding.
    """
    normalized = inkdrop_candidate_matching.normalize_candidate(candidate, wanted_item)
    return bool(_dict(normalized.get("source_unit_evidence")).get("sources"))


def _scope_for(raw, authority_codes):
    """Whether this refusal is the compatibility authority's to re-judge.

    A refusal that names its own authority is believed. Only rows that name
    none fall back to inferring it from a recorded compatibility verdict, and
    rows with neither stay `authority_not_recorded` -- which is a statement
    about the evidence, not a claim that some other subsystem decided.
    """
    named = _text(stored_refusal_record(raw).get("authority"))
    if named and named != "not_recorded":
        return SCOPE_COMPATIBILITY if named == AUTHORITY_COMPATIBILITY else SCOPE_OTHER_AUTHORITY
    return SCOPE_COMPATIBILITY if authority_codes else SCOPE_AUTHORITY_NOT_RECORDED


def replay_refusal(refusal, wanted_item):
    """Replay one retained refusal. Read-only, side-effect free.

    ``refusal`` is a row from :func:`retained_refusals`; ``wanted_item`` is the
    production target built by ``wanted_item_from_queue``.
    """
    refusal = _dict(refusal)
    raw = attempt_raw(refusal.get("raw_json"))
    source = _text(refusal.get("source"))
    stored_codes = stored_refusal_codes(raw, refusal.get("failure_reason"))
    authority_codes = _codes(stored_compatibility(raw).get("rejection_codes"))
    out = {
        "candidate_replay_contract_version": CONTRACT_VERSION,
        "wanted_id": _text(refusal.get("wanted_id")),
        "queue_id": _text(refusal.get("queue_id")),
        "attempt_id": _text(refusal.get("id")),
        "source": source,
        "authority": replay_authority(source),
        "decided_at": decided_at(raw, refusal.get("started_at"), refusal.get("completed_at")),
        "stored_refusal_codes": stored_codes,
        "stored_authority_codes": authority_codes,
        # A row whose refusal this authority never recorded was decided
        # somewhere else -- provider policy, client routing, the blocklist.
        # Keeping the two apart is what stops "compatibility has nothing to
        # say about a seeder floor" from being counted as a pass.
        "scope": _scope_for(raw, authority_codes),
        "replay_rejection_codes": [],
        "replay_review_codes": [],
        "cleared_codes": [],
        "new_codes": [],
    }
    out["decided_at_iso"] = inkdrop_state.utc_stamp(out["decided_at"]) if out["decided_at"] else ""
    samples = considered_samples(raw)
    out["considered_count"] = len(samples)
    out["considered_truncated"] = considered_truncated(raw)
    out["considered_outcomes"] = {}
    candidate = retained_candidate(raw)
    if not isinstance(wanted_item, dict) or not wanted_item:
        out["outcome"] = OUTCOME_INSUFFICIENT
        out["insufficient_reason"] = INSUFFICIENT_NO_TARGET
        return out

    # The considered lane runs whether or not a verdicted payload exists: it
    # is what the search saw, and it is often the only identity a refusal kept.
    considered = Counter()
    for sample in samples:
        if not has_replayable_identity(sample, wanted_item):
            continue
        _authority, verdict = replay_compatibility(sample, wanted_item, source=source)
        if _codes(verdict.get("rejection_codes")):
            considered[OUTCOME_STILL_REFUSED] += 1
        elif _codes(verdict.get("review_codes")):
            considered[OUTCOME_NOW_REVIEW] += 1
        else:
            considered[OUTCOME_NO_LONGER_REFUSED] += 1
    out["considered_outcomes"] = dict(considered)

    if candidate is None:
        out["outcome"] = OUTCOME_INSUFFICIENT
        out["insufficient_reason"] = INSUFFICIENT_ONLY_CONSIDERED if samples else INSUFFICIENT_NO_CANDIDATE
        return out
    if not has_replayable_identity(candidate, wanted_item):
        out["outcome"] = OUTCOME_INSUFFICIENT
        out["insufficient_reason"] = INSUFFICIENT_NO_IDENTITY_TEXT
        return out
    authority, verdict = replay_compatibility(candidate, wanted_item, source=source)
    out["authority"] = authority
    rejection = _codes(verdict.get("rejection_codes"))
    review = _codes(verdict.get("review_codes"))
    out["replay_rejection_codes"] = rejection
    out["replay_review_codes"] = review
    out["cleared_codes"] = [code for code in stored_codes if code not in rejection and code not in review]
    out["new_codes"] = [code for code in rejection if code not in stored_codes]
    if rejection:
        out["outcome"] = OUTCOME_STILL_REFUSED
    elif review:
        out["outcome"] = OUTCOME_NOW_REVIEW
    else:
        out["outcome"] = OUTCOME_NO_LONGER_REFUSED
    return out


def rejected_bucket_items(db_path, statuses=DEFAULT_STATUSES, now=None):
    """The Wanted rows the production classifier puts in the rejected bucket."""
    items = inkdrop_state.reliability_view_rows(db_path, statuses=statuses, now=now)
    return [item for item in items if item.get("bucket") == REJECTED_BUCKET]


def retained_refusals(db_path, wanted_ids, *, chunk_size=400):
    """Every retained provider refusal for these Wanted ids. Read-only.

    Rows are classified by ``reliability_attempt_evidence_class``, the same
    call the Reliability page uses to decide an attempt was a refusal, so this
    cannot disagree with the bucket it is explaining. Connections are
    short-lived and closed per chunk: a held read snapshot blocks WAL
    checkpoint truncation, which grew an 11.3 GiB write-ahead log here.
    """
    keys = [_text(value) for value in wanted_ids or [] if _text(value)]
    chunk_size = max(1, int(chunk_size))
    out = []
    for start in range(0, len(keys), chunk_size):
        chunk = keys[start:start + chunk_size]
        placeholders = ",".join("?" for _ in chunk)
        with inkdrop_state.connect_read(Path(db_path)) as con:
            # `title` and `query` are deliberately not selected. For most
            # providers they hold the search we sent rather than the release a
            # peer offered, so a replay that reached for them would judge a
            # string InkDrop invented. Leaving them unfetched makes that
            # mistake unreachable rather than merely discouraged.
            rows = con.execute(
                f"""
                select id, queue_id, wanted_id, source, status, failure_reason,
                       candidate_identity, download_url_hash, started_at, completed_at, raw_json
                  from source_attempts
                 where wanted_id in ({placeholders})
                """,
                chunk,
            ).fetchall()
        for row in rows:
            row = dict(row)
            if not inkdrop_state.reliability_attempt_is_provider_search(row.get("source")):
                continue
            if inkdrop_state.reliability_attempt_evidence_class(
                row.get("status"), row.get("failure_reason")
            ) != "rejected":
                continue
            out.append(row)
    return out


def _sample_rank(value):
    return hashlib.sha256(_text(value).encode("utf-8")).hexdigest()


def refusal_evidence_rank(row):
    """How replayable one refusal row is: verdicted, considered, or neither.

    An item carries many refusal rows, and the newest is frequently an
    item-level review handoff that kept the reason and dropped the release.
    Measured live 2026-08-18: taking the newest row outright reported 148 of
    400 sampled items as keeping no candidate, while three quarters of the
    population retain one on some row. Picking the newest row that actually
    kept a candidate answers the question being asked -- would this refused
    candidate pass now -- instead of answering it about the last piece of
    bookkeeping.
    """
    raw = row.get("raw_json") if isinstance(row, dict) else None
    if retained_candidate(raw) is not None:
        return 2
    if considered_samples(raw):
        return 1
    return 0


def newest_refusal_per_item(refusals):
    """One refusal per Wanted id: the newest that still holds a candidate.

    Ranked by retained evidence first, then by the decision's own timestamp.
    Falls back to the newest row overall when nothing was kept anywhere, so
    an item with no replayable evidence stays visible as exactly that.
    """
    best = {}
    for row in refusals or []:
        wanted_id = _text(row.get("wanted_id"))
        if not wanted_id:
            continue
        key = (
            refusal_evidence_rank(row),
            decided_at(row.get("raw_json"), row.get("started_at"), row.get("completed_at")) or 0.0,
            _text(row.get("id")),
        )
        current = best.get(wanted_id)
        if current is None or key > current[0]:
            best[wanted_id] = (key, row)
    return {key: value[1] for key, value in best.items()}


def stratified_sample(refusals_by_item, limit):
    """A deterministic sample spread across refusal reasons.

    Stratified because the reasons are wildly unequal -- one code covers more
    rows than the next five together -- and an unstratified draw would answer
    for that code alone. Deterministic (ranked by a hash of the Wanted id, no
    RNG) so a rerun measures the same rows and a changed number means the code
    changed, not the draw.
    """
    limit = max(0, int(limit or 0))
    if not limit or limit >= len(refusals_by_item):
        return dict(sorted(refusals_by_item.items()))
    strata = defaultdict(list)
    for wanted_id, row in refusals_by_item.items():
        codes = stored_refusal_codes(row.get("raw_json"), row.get("failure_reason"))
        strata[codes[0] if codes else ""].append(wanted_id)
    for key in strata:
        strata[key].sort(key=_sample_rank)
    picked = []
    order = sorted(strata, key=lambda key: (-len(strata[key]), key))
    cursor = 0
    while len(picked) < limit:
        progressed = False
        for key in order:
            if cursor < len(strata[key]):
                picked.append(strata[key][cursor])
                progressed = True
                if len(picked) >= limit:
                    break
        if not progressed:
            break
        cursor += 1
    return {wanted_id: refusals_by_item[wanted_id] for wanted_id in sorted(picked)}


def _targets_for(db_path, refusals_by_item, items_by_wanted_id):
    queue_ids = []
    for wanted_id in refusals_by_item:
        queue_id = _text(_dict(items_by_wanted_id.get(wanted_id)).get("queue_id"))
        if queue_id:
            queue_ids.append(queue_id)
    targets = {}
    for start in range(0, len(queue_ids), 200):
        chunk = queue_ids[start:start + 200]
        for queue_id, queue in inkdrop_source_worker_coordinator.queue_items_by_id(db_path, chunk).items():
            targets[queue_id] = inkdrop_source_worker_coordinator.wanted_item_from_queue(queue, db_path=db_path)
    return targets


def replay_sample(db_path, *, limit=200, statuses=DEFAULT_STATUSES, now=None):
    """Replay a bounded, stratified sample of the rejected bucket. Read-only.

    Returns the report; writes nothing anywhere.
    """
    started = time.time()
    items = rejected_bucket_items(db_path, statuses=statuses, now=now)
    items_by_wanted_id = {_text(item.get("wanted_id")): item for item in items}
    refusals = retained_refusals(db_path, list(items_by_wanted_id))
    by_item = newest_refusal_per_item(refusals)
    without_refusal = [key for key in items_by_wanted_id if key not in by_item]
    sample = stratified_sample(by_item, limit)
    targets = _targets_for(db_path, sample, items_by_wanted_id)

    results = []
    for wanted_id, row in sample.items():
        item = _dict(items_by_wanted_id.get(wanted_id))
        result = replay_refusal(row, targets.get(_text(item.get("queue_id"))))
        result["series"] = _text(item.get("series"))
        result["issue_number"] = _text(item.get("issue_number"))
        results.append(result)

    outcomes = Counter(result["outcome"] for result in results)
    insufficient = Counter(
        result.get("insufficient_reason", "")
        for result in results
        if result["outcome"] == OUTCOME_INSUFFICIENT
    )
    by_source = Counter(_text(result.get("source")) for result in results if result["outcome"] == OUTCOME_INSUFFICIENT)
    in_scope = [result for result in results if result["scope"] == SCOPE_COMPATIBILITY]
    judged = [result for result in in_scope if result["outcome"] != OUTCOME_INSUFFICIENT]
    changed = [result for result in judged if result["outcome"] != OUTCOME_STILL_REFUSED]
    cleared = Counter()
    for result in changed:
        for code in result["cleared_codes"]:
            cleared[code] += 1
    considered_rows = [result for result in results if result.get("considered_count")]
    considered_outcomes = Counter()
    for result in considered_rows:
        for key, value in _dict(result.get("considered_outcomes")).items():
            considered_outcomes[key] += value
    considered = {
        "rows_with_samples": len(considered_rows),
        "rows_where_the_sample_was_capped": sum(1 for result in considered_rows if result.get("considered_truncated")),
        "samples_retained": sum(int(result.get("considered_count") or 0) for result in considered_rows),
        "outcomes": dict(considered_outcomes),
        "note": (
            "Release titles a search looked at but never built a verdict for."
            " Judged on identity alone, missing fields the authority normally"
            " reads, and deliberately not counted in the headline."
        ),
    }
    return {
        "candidate_replay_contract_version": CONTRACT_VERSION,
        "read_only": True,
        "measured_at": started,
        "measured_at_iso": inkdrop_state.utc_stamp(started),
        "elapsed_seconds": round(time.time() - started, 3),
        "statuses": list(statuses),
        "rejected_bucket_items": len(items),
        "items_with_retained_refusal": len(by_item),
        "items_without_retained_refusal": len(without_refusal),
        "sample_size": len(results),
        "outcomes": dict(outcomes),
        "insufficient_reasons": dict(insufficient),
        "insufficient_by_source": dict(by_source.most_common()),
        "in_scope_for_compatibility": len(in_scope),
        "authority_not_recorded": sum(1 for result in results if result["scope"] == SCOPE_AUTHORITY_NOT_RECORDED),
        "other_authority": sum(1 for result in results if result["scope"] == SCOPE_OTHER_AUTHORITY),
        "judged": len(judged),
        "changed": len(changed),
        "cleared_codes": dict(cleared.most_common()),
        "considered_lane": considered,
        "results": results,
    }


def format_report(report):
    report = _dict(report)
    lines = [
        "Candidate replay -- read-only. Nothing grabbed, queued or written.",
        f"  measured                 {report.get('measured_at_iso')} in {report.get('elapsed_seconds')}s",
        f"  rejected-bucket items    {report.get('rejected_bucket_items')}",
        f"  with retained refusal    {report.get('items_with_retained_refusal')}",
        f"  without any refusal row  {report.get('items_without_retained_refusal')}",
        f"  sampled                  {report.get('sample_size')}",
        "  judged by this authority "
        f"{report.get('judged')} of {report.get('in_scope_for_compatibility')} in scope"
        f" ({report.get('other_authority')} another authority, {report.get('authority_not_recorded')} authority not recorded)",
        f"  verdict changed          {report.get('changed')}",
        "  outcomes:",
    ]
    for key, value in sorted(_dict(report.get("outcomes")).items(), key=lambda pair: -pair[1]):
        lines.append(f"    {key:<44} {value}")
    if report.get("insufficient_reasons"):
        lines.append("  insufficient evidence, by hole:")
        for key, value in sorted(_dict(report.get("insufficient_reasons")).items(), key=lambda pair: -pair[1]):
            lines.append(f"    {key:<44} {value}")
    if report.get("insufficient_by_source"):
        lines.append("  insufficient evidence, by source:")
        for key, value in _dict(report.get("insufficient_by_source")).items():
            lines.append(f"    {key:<44} {value}")
    if report.get("cleared_codes"):
        lines.append("  refusal codes the current authority no longer raises:")
        for key, value in _dict(report.get("cleared_codes")).items():
            lines.append(f"    {key:<44} {value}")
    lane = _dict(report.get("considered_lane"))
    if lane.get("rows_with_samples"):
        lines.append(
            f"  considered-only lane: {lane.get('samples_retained')} release titles across"
            f" {lane.get('rows_with_samples')} rows"
            f" ({lane.get('rows_where_the_sample_was_capped')} rows kept fewer samples than results seen)"
        )
        for key, value in sorted(_dict(lane.get("outcomes")).items(), key=lambda pair: -pair[1]):
            lines.append(f"    {key:<44} {value}")
        lines.append("    (identity only, never verdicted, not in the headline)")
    lines.append(
        "  Compatibility is not the whole grab decision: provider policy, client"
        " routing and the strict import verifier are not replayed."
    )
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Replay retained candidate refusals through the deployed authority. Read-only.",
    )
    parser.add_argument("--db", required=True, help="path to the InkDrop state database")
    parser.add_argument(
        "--limit",
        type=int,
        required=True,
        help="how many Wanted items to replay, so a sample is always a deliberate size",
    )
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    parser.add_argument("--results", action="store_true", help="include per-row results in the JSON output")
    args = parser.parse_args(argv)
    report = replay_sample(args.db, limit=args.limit)
    if args.json:
        if not args.results:
            report = {key: value for key, value in report.items() if key != "results"}
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
