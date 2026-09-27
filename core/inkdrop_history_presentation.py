"""One presentation contract for a History row.

THE PROBLEM THIS REPLACES.
    Three independent classifiers described the same row. The vanilla shell
    had inkdropHistoryEventLabel's regex ladder; the React island carried
    what its own comment called "a reduced version" of it; and each had its
    own result text. Independent ladders over the same strings do not stay
    equal, and two of them had already drifted apart in ways that made the
    page say things that were not true:

      * A SCHEDULED RETRY READ AS A PERFORMED ONE. display_phase
        "retry_later" means the next attempt is booked for the future. The
        React result pill rendered it "Retried", past tense, on the same row
        whose own detail line correctly read "Automatic retry is scheduled."
        Booking a retry and having retried are different events, and only one
        of them means the system has tried again.

      * A FAILURE READ AS A SUCCESS ON A SUBSTRING. Both ladders tested
        /verified|verification/ before any general failure rule, so
        "verification_failed" matched "verified" and rendered as Verified,
        with the success glyph. The shell's own result-text function had
        already learned this lesson and written it down -- "the keyword match
        below can fire on a row reporting exactly the opposite of success" --
        but the label ladder beside it never got the same treatment.

THE SHAPE OF THE FIX.
    Three questions, asked separately, because they have different answers:

      EVENT KIND   what happened            -> event_label / event_icon
      PHASE        where the item is now    -> display_phase (already computed)
      RESULT       how it turned out        -> result_label / result_tone

    Conflating them is what produced both defects above: "Verified" is an
    event kind, "verified" is a phase, and success is a result, and matching
    any of the three against one lowercased blob lets a failure inherit a
    success label.

    The labels are computed once, server-side, and served on the row. Both
    UIs render what they are handed instead of re-deriving it, so they cannot
    disagree -- which is the part a shared table alone would not have given,
    since nothing stops a second copy of a table from being written.

WHAT THIS IS NOT.
    Not a lifecycle rewrite. The vocabulary is the one recent_history()
    already computes (history_activity_fields, DISPLAY_PHASE_LABELS,
    history_outcome_bucket); this only decides how to say it. Unknown events
    still fall through to a truthful title-cased echo of the raw kind rather
    than being forced into the nearest known class.
"""

from __future__ import annotations

import re

# Result tones, matching the pill classes the History table already styles.
TONE_GOOD = "good"
TONE_BAD = "bad"
TONE_WARN = "warn"
TONE_ACTIVE = "auto"
TONE_NEUTRAL = ""

# An outcome of "problem" is the server's own verdict that this row went
# wrong. It vetoes every success label below, because a row can carry a
# success-shaped event_type and still be a failure -- which is exactly how
# "missing_folder_import_proof_retracted" and "verification_failed" used to
# render as wins.
PROBLEM_OUTCOMES = frozenset({"problem", "fail", "failed", "error"})

# Ordered event-kind rules. FAILURE PATTERNS COME FIRST, ALWAYS. A rule that
# can match a failing row must be listed above the success rule whose
# substring it shares, or the success rule wins on the shared substring --
# /verified|verification/ above any failure rule is the bug that shipped.
EVENT_RULES = (
    # --- explicitly failed, in every flavour we name ---------------------
    (re.compile(r"verif\w*[_ -]*fail|fail\w*[_ -]*verif|verification_failed"), "Verification Failed", "x"),
    (re.compile(r"import\w*[_ -]*fail|fail\w*[_ -]*import"), "Import Failed", "x"),
    (re.compile(r"search\w*[_ -]*fail|fail\w*[_ -]*search"), "Search Failed", "x"),
    (re.compile(r"download\w*[_ -]*fail|fail\w*[_ -]*download"), "Download Failed", "x"),
    (re.compile(r"grab\w*[_ -]*fail|fail\w*[_ -]*grab"), "Grab Failed", "x"),
    (re.compile(r"retract|revoked|proof[_ -]*missing"), "Proof Retracted", "x"),
    # --- decisions and interventions -------------------------------------
    (re.compile(r"manual.*decision|decision.*manual"), "Manual Decision", "eye"),
    (re.compile(r"candidate.*reject|reject.*candidate|^reject"), "Candidate Rejected", "shield"),
    (re.compile(r"blocked|blocklist"), "Source Blocked", "shield"),
    # --- retries: scheduled is not the same event as attempted -----------
    (re.compile(r"retry.*exhaust|exhaust.*retry|retry.*abandon"), "Retries Exhausted", "x"),
    (re.compile(r"retry.*(start|attempt|begun|performed)|(re)?attempt.*retry"), "Retry Attempted", "refresh"),
    (re.compile(r"retry"), "Retry Scheduled", "refresh"),
    # --- metadata and library bookkeeping --------------------------------
    (re.compile(r"metadata"), "Metadata Updated", "tag"),
    (re.compile(r"series.*add"), "Series Added", "calendar"),
    (re.compile(r"series.*remove"), "Series Removed", "calendar"),
    (re.compile(r"queue.*reconcile|reconcile.*queue"), "Requeued", "refresh"),
    # --- searching --------------------------------------------------------
    (re.compile(r"search.*request|request.*search"), "Search Requested", "search"),
    (re.compile(r"search.*start|start.*search"), "Search Started", "search"),
    (re.compile(r"search.*complete|complete.*search"), "Search Completed", "search"),
    (re.compile(r"search"), "Search", "search"),
    # --- the success side, reachable only once every failure rule missed --
    (re.compile(r"verified|verification"), "Verified", "check"),
    (re.compile(r"import"), "Imported", "check"),
    (re.compile(r"download.*complete|completed.*download"), "Download Completed", "cloud-download"),
    (re.compile(r"download.*start|downloading|download"), "Download Started", "download"),
    (re.compile(r"grab|candidate.*accept"), "Grabbed", "download"),
    (re.compile(r"scan"), "Library Scan", "radar"),
    (re.compile(r"manual"), "Manual Decision", "eye"),
)

# Phase -> result. Ordered, and consulted before the outcome, because a phase
# is a statement about where the row IS and outranks a general verdict about
# how it went.
PHASE_RESULTS = (
    ("manual_review", "Needs review", TONE_BAD),
    # NOT "Retried". The retry is booked, not performed; the row's own detail
    # line says "Automatic retry is scheduled" and the pill has to agree.
    ("retry_later", "Retry scheduled", TONE_WARN),
)

ACTIVE_PHASES = re.compile(r"downloading|importing|searching|running|active|in_progress")


def _raw_kind(row):
    return str(
        row.get("event_type") or row.get("history_kind") or row.get("status") or "event"
    ).strip().lower()


def _title_case(raw):
    return re.sub(r"\b\w", lambda match: match.group(0).upper(), str(raw or "").replace("_", " "))


def is_problem_row(row):
    """True when the server has already judged this row a failure."""
    return str(row.get("outcome") or "").strip().lower() in PROBLEM_OUTCOMES


def event_presentation(row):
    """What happened, as a label and a glyph.

    An unrecognised kind is echoed back title-cased rather than being forced
    into the nearest rule -- a truthful "Foo Bar Happened" beats a confident
    wrong classification, and it is how a new event type announces that it
    needs a rule here.
    """
    row = row or {}
    raw = _raw_kind(row)
    for pattern, label, icon in EVENT_RULES:
        if pattern.search(raw):
            # Last line of defence. A row the server calls a problem may still
            # match a success-shaped rule through a kind we have no failure
            # pattern for; it must not keep the success glyph.
            if is_problem_row(row) and icon in {"check", "cloud-download"}:
                return {"label": f"{label} (failed)", "icon": "x"}
            return {"label": label, "icon": icon}
    return {"label": _title_case(raw), "icon": "clock"}


def result_presentation(row):
    """How it turned out, as a pill label and a tone.

    Same display_phase/outcome vocabulary history_outcome_bucket() uses for
    the masthead cards, so a row's pill and the card counting it cannot
    disagree about what happened.
    """
    row = row or {}
    phase = str(row.get("display_phase") or "").strip().lower()
    outcome = str(row.get("outcome") or "").strip().lower()
    status = str(row.get("status") or "").strip().lower()

    for phase_key, label, tone in PHASE_RESULTS:
        if phase == phase_key:
            return {"label": label, "tone": tone}
    # A blocklist hit is a problem outcome server-side, but "Blocked" is the
    # specific truth and beats the generic one.
    if "blocked" in phase or "blocked" in status:
        return {"label": "Blocked", "tone": TONE_WARN}
    if outcome in PROBLEM_OUTCOMES:
        return {"label": "Failed", "tone": TONE_BAD}
    if phase == "verified":
        return {"label": "Verified", "tone": TONE_GOOD}
    if phase == "import_ready":
        return {"label": "Completed", "tone": TONE_GOOD}
    if ACTIVE_PHASES.search(phase):
        return {"label": "In progress", "tone": TONE_ACTIVE}
    fallback = row.get("display_phase_label") or row.get("status") or row.get("outcome") or "Logged"
    return {"label": _title_case(fallback), "tone": TONE_NEUTRAL}


def presentation_fields(row):
    """The four fields a History row carries so no UI has to re-derive them."""
    event = event_presentation(row)
    result = result_presentation(row)
    return {
        "event_label": event["label"],
        "event_icon": event["icon"],
        "result_label": result["label"],
        "result_tone": result["tone"],
    }
