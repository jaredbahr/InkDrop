#!/usr/bin/env python3
"""Translate slskd's own refusal prose into the shared refusal vocabulary.

The slskd probe refuses files with human-readable penalty strings
(``series title words are not an ordered phrase``), while every other
acquisition path records machine codes (``candidate_title_mismatch``). Until
both speak the same vocabulary the refusal distribution cannot be taken across
providers, which is the whole reason this module exists.

Two properties are load-bearing and both are structural rather than promised:

**Media filtering is not adjudication, and cannot be merged into it.**
Measured on 2026-08-18 against the live probe cache: 723,876 of 789,944 refused
files (91.6%) were refused for carrying a ``.mp3``/``.flac``/``.opus``/``.jpg``
extension. Those files never became candidates -- the extension gate runs
before candidate construction -- so they are not decisions *about a candidate*
and must never appear in a refusal distribution. If they were merged,
``unsupported extension .mp3`` would become the top refusal reason in the
product and destroy the measurement. So the classifier returns a *class*
alongside the code, and the caller keeps the two in separate fields. There is
no code path that can put a ``media_filter`` code into the adjudication list.

**Nothing is guessed.** Every entry below is an explicitly declared match --
exact string, declared prefix, or anchored pattern -- and each was read off the
function in ``inkdrop_slskd_source_probe`` that produces it, not inferred from
sampled data. There is deliberately **no substring matching**: a label that
happens to contain a mapped label's text is not that label. Anything the table
does not cover maps to ``unmapped``, because a mapping that silently mis-buckets
is worse than no mapping -- it looks authoritative while being wrong, and every
consumer downstream inherits the error without a way to notice it.

The label vocabulary is unbounded by construction (1,018 distinct labels
observed, from ~40 shapes) because producers interpolate values into the
prose: ``book/volume token 3 does not match 14``. That is why prefixes and
anchored patterns are declared forms here rather than an exact-match table,
which would leave the majority unmapped.

**The interpolated part is somebody else's file name, so it does not travel.**
What gets interpolated is text lifted out of the remote file or its parent
folder -- ``related subseries title tail: return to treehouse``, ``candidate
appears to be a different titled series/subseries: nickelodeon``, ``unsupported
extension .xtch``. Counted and persisted, that is a stranger's library naming
sitting in our database. ``redact_label()`` takes the interpolation out and
leaves the declared form, so the label still says which decision was made and
no longer says which file it was made about. It is the reason the patterns
above wrap their peer-derived span in a capturing group: that group is exactly
what a redaction removes.
"""

from __future__ import annotations

import hashlib
import re
import time

# How a table entry is matched. Declared per row -- never inferred, and never
# widened to "the label contains this text somewhere".
EXACT = "exact"
PREFIX = "prefix"
PATTERN = "pattern"

# What kind of refusal this is. These are different *kinds of decision*, not
# positions in the pipeline (that axis is `stage` in
# inkdrop_source_worker_runtime.refusal_evidence).
CLASS_MEDIA_FILTER = "media_filter"
CLASS_ADJUDICATION = "adjudication"

# The honest value for a label no entry claims. Not a bucket of last resort to
# be quietly grown -- when this shows up in the data it means a producer added
# a shape and this table has not been told about it yet.
CODE_UNMAPPED = "unmapped"

# What an interpolated span reads as once it has been taken out. One token, no
# spaces, so a redacted label still matches the declared form it came from and
# `classify()` keeps working on it unchanged.
REDACTED = "<redacted>"

# The durable handle an unrecognised label keeps in place of its text. A digest
# of the prose, so the same unknown shape counts as the same unknown shape on
# every later attempt and a table gap is still countable -- and so an engineer
# holding a candidate label can confirm the match by hashing it locally.
OPAQUE_LABEL_PREFIX = "unmapped_penalty:"

# (form, matcher, code, class)
#
# Grouped by the producing site so a future reader can check the table against
# the source rather than against their memory of it.
REFUSAL_LABEL_TABLE = (
    # --- pre-candidate media filtering (inkdrop_slskd_source_probe:6774, 6780,
    # 12135). These files are filtered before a candidate exists. ---
    (PREFIX, "unsupported extension ", "unsupported_file_extension", CLASS_MEDIA_FILTER),
    (EXACT, "non-comic path context", "non_comic_path_context", CLASS_MEDIA_FILTER),

    # --- series title identity (series_title_match and friends) ---
    (EXACT, "no title words available", "candidate_title_mismatch", CLASS_ADJUDICATION),
    (EXACT, "no series title words available", "candidate_title_mismatch", CLASS_ADJUDICATION),
    (EXACT, "single-word title missing from filename", "candidate_title_mismatch", CLASS_ADJUDICATION),
    (EXACT, "single-word title repeats like a different series title", "candidate_title_mismatch", CLASS_ADJUDICATION),
    (EXACT, "single-word title appears only as subtitle/path text", "candidate_title_mismatch", CLASS_ADJUDICATION),
    (EXACT, "series title words are not an ordered phrase", "candidate_title_mismatch", CLASS_ADJUDICATION),
    (EXACT, "series title words are scattered across filename/path text", "candidate_title_mismatch", CLASS_ADJUDICATION),
    (EXACT, "title mismatch", "candidate_title_mismatch", CLASS_ADJUDICATION),
    (PATTERN, r"matched only \d+/\d+ required title words", "candidate_title_mismatch", CLASS_ADJUDICATION),
    (PREFIX, "missing subtitle/title word: ", "candidate_title_mismatch", CLASS_ADJUDICATION),

    # --- a different, related work (title_prefix_subseries_conflict,
    # leaf_title_conflict, related_subseries_source_blocker,
    # staged_parent_series_conflict) ---
    (PREFIX, "candidate appears to be a different titled series/subseries: ", "related_series_identity", CLASS_ADJUDICATION),
    (PREFIX, "filename title appears to be a different series: ", "related_series_identity", CLASS_ADJUDICATION),
    (PREFIX, "filename title appears to be a related different series/subseries: ", "related_series_identity", CLASS_ADJUDICATION),
    (PREFIX, "related subseries title tail: ", "related_series_identity", CLASS_ADJUDICATION),
    (PREFIX, "related subseries title prefix: ", "related_series_identity", CLASS_ADJUDICATION),
    (PREFIX, "related subseries title tail after publisher imprint: ", "related_series_identity", CLASS_ADJUDICATION),
    # The prefixed form names the bracket group that could not be explained.
    # The bare form below it is kept because 48,768 rows already carry it and
    # classify() maps an unrecognised label to CODE_UNMAPPED without raising.
    (PREFIX, "related subseries or untrusted publication suffix: ", "related_series_identity", CLASS_ADJUDICATION),
    (EXACT, "related subseries or untrusted publication suffix", "related_series_identity", CLASS_ADJUDICATION),
    (EXACT, "publisher imprint is not attached to an exact numbered series title", "related_series_identity", CLASS_ADJUDICATION),
    (PREFIX, "parent folder names a different series: ", "related_series_identity", CLASS_ADJUDICATION),

    # --- the unit number (issue_number_match, book_volume_match) ---
    # `wrong_volume_number` and `wrong_issue_number` are kept distinct because
    # the shared vocabulary distinguishes them and collapsing volume evidence
    # into an issue code would misreport which axis disagreed.
    (PATTERN, r"book/volume token (.+) does not match .+", "wrong_volume_number", CLASS_ADJUDICATION),
    (PATTERN, r"explicit issue token (.+) does not match .+", "wrong_issue_number", CLASS_ADJUDICATION),
    (PATTERN, r"filename issue token (.+) does not match .+", "wrong_issue_number", CLASS_ADJUDICATION),
    (PATTERN, r"issue range (.+) does not contain .+", "wrong_issue_number", CLASS_ADJUDICATION),
    (PREFIX, "filename issue evidence overrides folder context: ", "wrong_issue_number", CLASS_ADJUDICATION),
    (EXACT, "missing issue/part token", "missing_required_unit_number", CLASS_ADJUDICATION),
    (EXACT, "no numeric issue token", "missing_required_unit_number", CLASS_ADJUDICATION),
    (EXACT, "no issue title metadata", "unknown_match_confidence", CLASS_ADJUDICATION),
    (PATTERN, r"matched only \d+/\d+ required issue-title words", "candidate_title_mismatch", CLASS_ADJUDICATION),

    # --- edition / collection semantics (collected_singleton_edition_conflict) ---
    (EXACT, "semantic conflict with collected comic target", "collected_edition_disallowed", CLASS_ADJUDICATION),
    (EXACT, "prefixless Absolute-edition match lacks collection/volume evidence", "collected_edition_disallowed", CLASS_ADJUDICATION),
    (PATTERN, r"candidate year (.+) conflicts with target year .+ and does not carry the target's .+ edition marker", "collected_edition_disallowed", CLASS_ADJUDICATION),

    # --- language (localized_title_penalty) ---
    (PREFIX, "likely translated issue title: ", "wrong_language", CLASS_ADJUDICATION),
    (PREFIX, "non-English language marker: ", "wrong_language", CLASS_ADJUDICATION),

    # --- staged-file guards (weak_staged_filename_guard). These already emit
    # code-shaped strings; they are still declared rather than passed through,
    # so the shared vocabulary stays the only thing consumers see. ---
    (EXACT, "duplicate_copy_suffix", "duplicate_candidate", CLASS_ADJUDICATION),
    (EXACT, "pack_candidate_requires_pack_handling", "pack_membership_not_proven", CLASS_ADJUDICATION),
    (EXACT, "single_part_file_does_not_satisfy_collection_target", "wrong_unit_type", CLASS_ADJUDICATION),
    (EXACT, "unit_model_mismatch", "wrong_unit_type", CLASS_ADJUDICATION),
    (EXACT, "weak_filename_unit_evidence", "ambiguous_unit_identity", CLASS_ADJUDICATION),

    # --- identity proof gaps (title_terminal_punctuation_conflict:6194,
    # collected singleton fallback:7099) ---
    (EXACT, "candidate title has significant terminal punctuation for a different series identity", "related_series_identity", CLASS_ADJUDICATION),
    (EXACT, "candidate file identity does not prove the collected singleton", "ambiguous_unit_identity", CLASS_ADJUDICATION),

    # --- codes the probe already emits verbatim ---
    # Some slskd paths hand a target-compatibility code straight through as a
    # penalty, so the raw label is already in the shared vocabulary. Declared
    # as identities rather than passed through untouched, so the table stays
    # the single place that says what a consumer can receive.
    (EXACT, "wrong_issue_number", "wrong_issue_number", CLASS_ADJUDICATION),
    (EXACT, "wrong_unit_type", "wrong_unit_type", CLASS_ADJUDICATION),
    (EXACT, "wrong_chapter_number", "wrong_chapter_number", CLASS_ADJUDICATION),
    (EXACT, "wrong_volume_number", "wrong_volume_number", CLASS_ADJUDICATION),
    (EXACT, "coverage_not_unit_number", "coverage_not_unit_number", CLASS_ADJUDICATION),
    (EXACT, "creator_identity_conflict", "creator_identity_conflict", CLASS_ADJUDICATION),
    (EXACT, "related_series_identity", "related_series_identity", CLASS_ADJUDICATION),
    (EXACT, "candidate_title_mismatch", "candidate_title_mismatch", CLASS_ADJUDICATION),
    (EXACT, "missing_required_unit_number", "missing_required_unit_number", CLASS_ADJUDICATION),
    (EXACT, "ambiguous_unit_identity", "ambiguous_unit_identity", CLASS_ADJUDICATION),
    (EXACT, "collected_edition_disallowed", "collected_edition_disallowed", CLASS_ADJUDICATION),
    (EXACT, "print_run_not_confirmed", "print_run_not_confirmed", CLASS_ADJUDICATION),

    # --- the probe's own fall-throughs (rejection_label, annotate_* ) ---
    (EXACT, "rejected by matcher", "source_did_not_mark_candidate_acceptable", CLASS_ADJUDICATION),
    (EXACT, "candidate no longer matches row", "source_identity_rejected", CLASS_ADJUDICATION),

    # --- what an unrecognised label becomes once redacted ---
    # Declared so the opaque form classifies exactly as the prose it replaced
    # did: unmapped, and therefore still visible as a table gap. Without this
    # row a redacted unknown would be a second unknown shape and redaction
    # would stop being idempotent.
    (PREFIX, OPAQUE_LABEL_PREFIX, CODE_UNMAPPED, CLASS_ADJUDICATION),
)

# Labels the product writes verbatim, with nothing interpolated into them, that
# the table above deliberately does not claim. Declared here for one purpose
# only: so redaction leaves them alone. They are NOT table entries, so what
# `classify()` makes of them is unchanged -- adding a string here changes what
# a label looks like, never what it counts as.
#
# The bar for this list is not "we recognise it", it is "we wrote it, all of
# it". A string with any interpolated span belongs in the table as a prefix or
# a pattern instead.
#
# Both entries were found the same way, by redacting every distinct label in
# the 2026-09-13 snapshot and reading what came out opaque: those are the
# strings we wrote that no table entry claims. `collection_target_single_part`
# is deliberately left here rather than promoted to a table row -- it would
# classify as `wrong_unit_type` and that is a change to the refusal
# distribution, which is a different question from this one.
DECLARED_LITERAL_LABELS = frozenset({
    # inkdrop_slskd_source_probe.RAW_PAGE_LOCKED_REASON
    "raw page folder skipped: pages are locked",
    # inkdrop_candidate_matching:2131, a review reason passed through as a
    # penalty. 4 instances in the snapshot.
    "collection_target_single_part",
})

# File suffixes that may stay readable in ``unsupported extension <suffix>``.
#
# The suffix is not ours: it is whatever followed the last dot in the remote
# file name, so the field is unbounded and a peer decides its contents. The
# live snapshot already holds 72 distinct values, including a peer's Synology
# sidecars (``.flac@synoeastream``) and one-offs like ``.xtch`` and ``.pck``.
#
# Deleting the suffix would be the easy answer and the wrong one: 91.6% of
# refused files are refused right here and *which format* they were is the
# whole content of that number. So the descriptor is bounded instead of
# dropped -- a declared format name survives, anything else reads as
# ``<redacted>`` and is still counted under `unsupported_file_extension`.
# Undeclared costs a diagnostic; declared-by-default would cost a stranger's
# file naming, and that is the trade this list takes.
DECLARED_FILE_SUFFIXES = frozenset({
    # Produced by us when there is no suffix at all, not read off the file.
    "no extension",
    # comics and books
    ".cbz", ".cbr", ".cb7", ".cbt", ".pdf", ".epub", ".mobi", ".azw3", ".djvu",
    # archives
    ".zip", ".rar", ".7z", ".tar", ".gz",
    # images
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff",
    # audio -- the bulk of the extension gate's work
    ".mp3", ".flac", ".opus", ".ogg", ".m4a", ".m4b", ".m4p", ".wav", ".aiff",
    ".aif", ".wma", ".ape", ".wv", ".dsf", ".aac", ".mid", ".midi",
    # video
    ".mkv", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".m4v", ".mpg", ".mpeg",
    # the sidecars that travel with a shared folder
    ".nfo", ".txt", ".log", ".cue", ".sfv", ".m3u", ".m3u8", ".lrc", ".srt",
    ".ass", ".sub", ".opf", ".xml", ".json", ".yaml", ".html", ".mht", ".url",
    ".torrent", ".iso", ".db", ".ini", ".css", ".doc", ".docx", ".rtf",
})


def _compiled():
    compiled = []
    for form, matcher, code, refusal_class in REFUSAL_LABEL_TABLE:
        if form == PATTERN:
            compiled.append((form, re.compile(matcher), code, refusal_class, matcher))
        else:
            compiled.append((form, matcher, code, refusal_class, matcher))
    return tuple(compiled)


_TABLE = _compiled()


def classify(label):
    """Map one slskd penalty string to ``(code, refusal_class)``.

    Unknown labels return ``(CODE_UNMAPPED, CLASS_ADJUDICATION)`` -- adjudication
    because an unrecognised label came out of the matcher, and calling it media
    filtering would hide it from exactly the distribution it belongs in.
    """
    text = str(label or "").strip()
    if not text:
        return CODE_UNMAPPED, CLASS_ADJUDICATION
    for form, matcher, code, refusal_class, _raw in _TABLE:
        if form == EXACT:
            if text == matcher:
                return code, refusal_class
        elif form == PREFIX:
            if text.startswith(matcher):
                return code, refusal_class
        elif form == PATTERN:
            if matcher.fullmatch(text):
                return code, refusal_class
    return CODE_UNMAPPED, CLASS_ADJUDICATION


def is_media_filter(label):
    return classify(label)[1] == CLASS_MEDIA_FILTER


# The one prefix whose interpolation is a bounded descriptor rather than free
# text, handled apart from the others below.
_EXTENSION_PREFIX = "unsupported extension "


def _blank_interpolated_groups(text, found):
    """Replace every capturing group's span with ``REDACTED``.

    A capturing group in ``REFUSAL_LABEL_TABLE`` means one thing and is used
    for nothing else: *this span was read off the remote file*. The
    non-captured ``.+`` spans in the same patterns are our own target's
    numbers, and they stay -- ``does not match 4`` is the half that says what
    we asked for, and losing it would leave a label that names a disagreement
    without naming either side of it.
    """
    spans = [
        found.span(index)
        for index in range(1, (found.re.groups or 0) + 1)
        if found.span(index) != (-1, -1)
    ]
    for start, end in sorted(spans, reverse=True):
        text = text[:start] + REDACTED + text[end:]
    return text


def redact_label(label):
    """Take the peer's own text out of one penalty string, keep the decision.

    Matches the table in the same order ``classify()`` does, so the two can
    never disagree about which entry a label belongs to:

      * an **exact** entry interpolates nothing, so it comes back verbatim;
      * a **prefix** entry's tail is filename or folder words, so it becomes
        ``<prefix><redacted>`` -- except ``unsupported extension ``, whose
        tail is kept when it names a declared format (see
        ``DECLARED_FILE_SUFFIXES``);
      * a **pattern** entry loses its capturing groups and nothing else;
      * a string in ``DECLARED_LITERAL_LABELS`` is ours in full and is kept;
      * anything else is prose nobody has declared, so it cannot be trusted to
        contain nothing -- it becomes ``unmapped_penalty:<digest>``.

    **Idempotent, which is what lets it sit at more than one boundary.** Every
    output above still matches the entry its input matched, so redacting twice
    changes nothing. That is why the probe can redact where it counts *and*
    the writer can redact again before the column, without the second pass
    mangling the first.

    The digest is taken over the case-folded prose, so the same unknown shape
    reported by two producers, or by the same producer twice, counts as one
    shape -- a table gap you can count is a table gap someone can close.
    """
    text = str(label or "").strip()
    if not text:
        return ""
    # Already redacted. Checked before the table so a second pass cannot
    # digest a digest, which would make the handle unstable.
    if text.startswith(OPAQUE_LABEL_PREFIX):
        return text
    if text in DECLARED_LITERAL_LABELS:
        return text
    for form, matcher, _code, _refusal_class, _raw in _TABLE:
        if form == EXACT:
            if text == matcher:
                return text
        elif form == PREFIX:
            if not text.startswith(matcher):
                continue
            if matcher == _EXTENSION_PREFIX:
                suffix = text[len(matcher):].strip().casefold()
                if suffix in DECLARED_FILE_SUFFIXES:
                    return matcher + suffix
            return matcher + REDACTED
        elif form == PATTERN:
            found = matcher.fullmatch(text)
            if found is not None:
                return _blank_interpolated_groups(text, found)
    return OPAQUE_LABEL_PREFIX + hashlib.sha256(
        text.casefold().encode("utf-8", "replace")
    ).hexdigest()[:16]


def redact_reason_counts(reason_counts):
    """Redact a ``[{"reason": str, "count": int}, ...]`` list in place of text.

    Counts are re-summed after redaction, because two labels that differed
    only by the peer text they carried are the same refusal and have to read
    as one row rather than two identical-looking ones.
    """
    totals = {}
    for row in reason_counts or []:
        if not isinstance(row, dict):
            continue
        label = redact_label(row.get("reason"))
        if not label:
            continue
        try:
            count = int(row.get("count") or 0)
        except (TypeError, ValueError):
            continue
        totals[label] = totals.get(label, 0) + count
    return [
        {"reason": reason, "count": count}
        for reason, count in sorted(totals.items(), key=lambda row: (-row[1], row[0]))
    ]


# How many refused candidates keep an identity. Upstream `summarize_rejections`
# already ranks and caps its samples at 5; this mirrors that so the cap is
# stated in both places rather than inherited silently.
SAMPLE_CAP = 5


def identity_fingerprint(filename):
    """A stable, non-identifying handle for one refused candidate.

    Peer file inventory is memory-only in this codebase and is never persisted
    -- not the username, not the directory, and not the filename. That contract
    predates this module (pinned in inkdrop-slskd-series-run-handoff-smoke) and
    is broader than "no full paths": *which files a peer holds* is the thing
    being protected, and a leaf name answers that as completely as a path does.

    So the durable identity is a digest of the leaf, not the leaf. What that
    still buys, which an aggregate count does not:

      * distinct candidates are countable -- five refusals of one release is a
        different fact from one refusal each of five;
      * each candidate keeps its own full reason set;
      * the same release refused on a later attempt fingerprints the same, so
        repeat refusals are visible as repeats.

    Derived from the leaf rather than the path on purpose: the same release
    shared by two peers under different directories yields one fingerprint.

    **Do not replace this with the readable name.** The evidence being
    unreadable to us is the property, not a shortcoming.
    """
    text = str(filename or "").strip()
    if not text:
        return ""
    for separator in ("\\", "/"):
        if separator in text:
            text = text.rsplit(separator, 1)[-1]
    leaf = text.strip().casefold()
    if not leaf:
        return ""
    return hashlib.sha256(leaf.encode("utf-8", "replace")).hexdigest()[:16]


def candidate_samples(samples, cap=SAMPLE_CAP):
    """Per-candidate refusal evidence, scrubbed to leaf identity and codes.

    Raw penalty prose is **not** carried. Several producers interpolate peer
    folder text into their message (``parent folder names a different series:
    <folder>``), so the prose is a second route to the same inventory the
    fingerprint exists to withhold. Nothing in a returned record is readable
    text: the codes are a closed vocabulary and the identity is a digest, so
    there is no field a peer's library can be reconstructed from.

    Every contributing reason is kept, not just the first: the single-column
    projection that keeps sending people to the wrong subsystem is exactly what
    durable evidence is supposed to end.
    """
    records = []
    for row in (samples or [])[: max(0, int(cap))]:
        if not isinstance(row, dict):
            continue
        identity = identity_fingerprint(row.get("filename"))
        labels = [str(value or "").strip() for value in (row.get("match_penalties") or [])]
        if not labels:
            label = str(row.get("reason") or "").strip()
            labels = [label] if label else []
        codes = []
        classes = []
        for label in labels:
            if not label:
                continue
            code, refusal_class = classify(label)
            if code not in codes:
                codes.append(code)
            if refusal_class not in classes:
                classes.append(refusal_class)
        if not identity and not codes:
            continue
        record = {"identity_fingerprint": identity, "reasons": codes}
        try:
            record["score"] = int(row.get("score") or 0)
        except (TypeError, ValueError):
            record["score"] = 0
        # A candidate refused only by the extension gate never became a
        # candidate at all; labelling it here keeps that visible per-row rather
        # than only in the aggregate.
        record["refusal_class"] = (
            CLASS_MEDIA_FILTER
            if classes == [CLASS_MEDIA_FILTER]
            else CLASS_ADJUDICATION
        )
        records.append(record)
    return records


def merge_refusal_evidence(records):
    """Combine one probe row's per-query evidence into one ledger record.

    A probe row runs several queries and each produces its own evidence, but
    ``source_attempts`` gets one row per probe. Merging is arithmetic over
    records that already exist -- it never constructs a record where no query
    refused anything, so an item that was searched and refused nothing still
    reads as exactly that.

    The timestamp is the newest contributing *decision*, never the queue row's
    ``updated_at``: the retry machinery touches that every pass, so a refusal
    decided six weeks ago would read as decided moments ago.
    """
    kept = [row for row in (records or []) if isinstance(row, dict) and row.get("reasons")]
    if not kept:
        return {}
    if len(kept) == 1:
        return dict(kept[0])

    totals = {}
    identities = []
    seen = set()
    refused_file_count = 0
    counts_truncated = False
    candidates_truncated = False
    unmapped = []
    decided_at = 0.0
    for row in kept:
        detail = row.get("detail") if isinstance(row.get("detail"), dict) else {}
        for code, count in (detail.get("reason_counts") or {}).items():
            try:
                totals[code] = totals.get(code, 0) + int(count or 0)
            except (TypeError, ValueError):
                continue
        # A code with no count still has to survive the merge, or a query whose
        # counts were dropped upstream would vanish from the reason list.
        for code in row.get("reasons") or []:
            totals.setdefault(code, 0)
        refused_file_count += int(detail.get("refused_file_count") or 0)
        counts_truncated = counts_truncated or bool(detail.get("reason_counts_truncated"))
        candidates_truncated = candidates_truncated or bool(detail.get("candidates_truncated"))
        for label in detail.get("unmapped_labels") or []:
            if label not in unmapped:
                unmapped.append(label)
        for candidate in detail.get("candidates") or []:
            key = str((candidate or {}).get("identity_fingerprint") or "")
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            identities.append(candidate)
        try:
            decided_at = max(decided_at, float(row.get("decided_at") or 0))
        except (TypeError, ValueError):
            continue

    if len(identities) > SAMPLE_CAP:
        candidates_truncated = True
        identities = identities[:SAMPLE_CAP]

    ranked = [code for code, _count in sorted(totals.items(), key=lambda row: (-row[1], row[0]))]
    first = kept[0]
    detail = dict(first.get("detail") or {})
    detail.update({
        "reason_counts": {code: totals[code] for code in ranked},
        "refused_file_count": refused_file_count,
        "candidates": identities,
        "candidate_sample_cap": SAMPLE_CAP,
        "candidates_truncated": candidates_truncated,
        "reason_counts_truncated": counts_truncated,
        "query_count": len(kept),
    })
    if unmapped:
        detail["unmapped_labels"] = unmapped[:8]
        detail["unmapped_label_count"] = len(unmapped)
    else:
        detail.pop("unmapped_labels", None)
        detail.pop("unmapped_label_count", None)
    merged = dict(first)
    merged["reasons"] = ranked
    merged["primary_reason"] = ranked[0]
    merged["detail"] = detail
    if decided_at > 0:
        merged["decided_at"] = decided_at
        merged["decided_at_iso"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(decided_at))
    return merged


def merge_media_filter_summaries(summaries):
    """Sum the pre-candidate filter counts across one probe row's queries.

    Deliberately a separate function from ``merge_refusal_evidence`` with a
    separate return value. There is no merge that produces both in one
    structure, because the only reason they exist apart is that they must never
    be added together.
    """
    kept = [row for row in (summaries or []) if isinstance(row, dict) and row.get("reason_counts")]
    if not kept:
        return {}
    totals = {}
    filtered = 0
    for row in kept:
        for code, count in (row.get("reason_counts") or {}).items():
            try:
                totals[code] = totals.get(code, 0) + int(count or 0)
            except (TypeError, ValueError):
                continue
        filtered += int(row.get("filtered_file_count") or 0)
    ranked = sorted(totals.items(), key=lambda row: (-row[1], row[0]))
    return {
        "provider": "slskd",
        "stage": "pre_candidate_media_filter",
        "filtered_file_count": filtered,
        "reason_counts": {code: count for code, count in ranked},
        "query_count": len(kept),
    }


def classify_counts(reason_counts):
    """Split ``[{"reason": str, "count": int}, ...]`` by class.

    Returns ``{"adjudication": {code: count}, "media_filter": {code: count},
    "unmapped_labels": [...], "adjudication_file_count": int,
    "media_filter_file_count": int}``.

    The two count maps are separate dicts rather than one map with a class
    field, so there is no shape in which a caller can sum them by accident.
    """
    adjudication = {}
    media_filter = {}
    unmapped = []
    for row in reason_counts or []:
        if not isinstance(row, dict):
            continue
        label = str(row.get("reason") or "").strip()
        if not label:
            continue
        try:
            count = int(row.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        if count <= 0:
            continue
        code, refusal_class = classify(label)
        bucket = media_filter if refusal_class == CLASS_MEDIA_FILTER else adjudication
        bucket[code] = bucket.get(code, 0) + count
        if code == CODE_UNMAPPED and label not in unmapped:
            unmapped.append(label)
    return {
        "adjudication": adjudication,
        "media_filter": media_filter,
        # Capped: an unmapped label is a signal that the table needs an entry,
        # and a handful is enough to act on. The count above stays exact.
        "unmapped_labels": unmapped[:8],
        "unmapped_label_count": len(unmapped),
        "adjudication_file_count": sum(adjudication.values()),
        "media_filter_file_count": sum(media_filter.values()),
    }
