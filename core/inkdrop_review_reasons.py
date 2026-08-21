#!/usr/bin/env python3
"""One vocabulary for turning a review reason into something a person can read.

Manual Review is rendered three times -- the React island, the vanilla desktop
shell, and `/m` -- and the reason vocabulary existed in exactly one of them. The
mobile shell ran every reason through a generic title-caser, so
`slskd_transfer_missing_staged_file_repeat` reached the operator as
"Slskd Transfer Missing Staged File Repeat": a machine identifier dressed as an
English sentence, which reads as InkDrop's considered explanation rather than as
the leaked token it is. A bare snake_case string at least looks like a bug.

This lives server-side because that is the only place all three surfaces already
agree on. Every one of them reads the same `/api/inkdrop-state/manual_review`
rows, so attaching the label there gives one copy that cannot drift, rather than
a second table in JavaScript that agrees with the TypeScript one until someone
adds a reason to only one of them.

TWO FIELDS WITH DIFFERENT RULES, WHICH IS DELIBERATE AND PREDATES THIS MODULE.
    The desktop island distinguishes a *badge* from a *detail line*:

      - the badge always shows something, deriving a label from an unknown code
        rather than going blank, because a reason with no badge reads as "no
        reason given";
      - the detail line refuses identifiers outright, because it sits under the
        badge and printing the gate's own key there showed the same fact twice,
        once in machine form. That was reported live on
        `qbit_torrent_completed_outside_expected_save_path`.

    Both rules are preserved here rather than collapsed into one. Collapsing
    them would either blank the badge or re-leak the key.

DERIVED LABELS ARE MARKED, NOT HIDDEN.
    When a reason is not in REASON_META the label is derived from the token, and
    `reason_label_source` says `derived`. That flag is what lets a test assert
    "no operator-facing label was derived from an identifier" for the reasons we
    know about, without pretending the enum is closed. `review_reason` is not a
    closed enum and this module does not claim it is.
"""

from __future__ import annotations

import re


# A token that is only lowercase words joined by underscores is an identifier,
# not prose. Prose that happens to contain one ("it's stopped auto_retrying and
# needs a decision from you.") does not match, because the anchors require the
# whole string to be the token.
REASON_CODE_RE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)+$")

# ...but the whole-string anchor is itself a hole, and it is the one that put
# `trusted_issue_mismatch:174!=001` in front of an operator TWICE on one card:
# once title-cased into a badge and once raw underneath. The `:174!=001` suffix
# breaks the anchor above, so the string was not recognised as an identifier
# and was passed through as though it were prose.
#
# The defect is the anchor, not a missing lookup entry. Any reason that carries
# a payload after its code has always slipped past -- this is the shape, not
# the instance. Matching it here is what lets the vocabulary give the badge a
# real label and keep the raw token out of the detail line.
REASON_CODE_WITH_PAYLOAD_RE = re.compile(r"^(?P<gate>[a-z0-9]+(?:_[a-z0-9]+)+):(?P<left>[^!]*)!=(?P<right>.*)$")

# Left is what was FOUND, right is what was EXPECTED. From the producer,
# core/inkdrop_completed_import.py: f"trusted_issue_mismatch:{actual}!={expected}".
# That is the opposite of how "198 != 001" reads, so a label built from it must
# state which is which rather than repeating the two numbers.
GATE_SENTENCES = {
    "trusted_issue_mismatch": "Wanted issue {expected}, file looks like issue {found}",
}

# Deliberate phrasing for the reasons we have actually seen. Ported verbatim
# from the desktop island so the two surfaces cannot disagree about wording;
# the island now reads these values off the row instead of keeping its own copy.
REASON_META = {
    "weak_filename_unit_evidence": ("Weak filename evidence", "warn"),
    "ambiguous_results": ("Two possible matches", "warn"),
    "pack_requires_review": ("Pack needs review", "warn"),
    "wrong_unit_type": ("Wrong unit type", "warn"),
    "conflicting_manga_identity": ("Conflicting manga identity", "warn"),
    "filename_confidence_too_low": ("Filename confidence too low", "warn"),
    "destination_conflict": ("Destination conflict", "bad"),
    "policy_block": ("Blocked by policy", "bad"),
    "language_blocked": ("Blocked by language rule", "bad"),
    "staged_file_low_confidence": ("Staged file mismatch", "warn"),
    "qbit_torrent_completed_outside_expected_save_path": ("Torrent finished in the wrong folder", "bad"),
    # Named because it is the row that prompted this module. Without an entry
    # it still gets a derived label, but a derived label for a nine-word token
    # is exactly the sentence-shaped machine output worth avoiding.
    "slskd_transfer_missing_staged_file_repeat": ("Transfer finished but the file is missing", "bad"),
}

_BAD_TONE_RE = re.compile(r"fail|error|blocked|missing|conflict")


def looks_like_reason_code(value):
    """Is this the gate's own key rather than something written for a person?

    Recognises both the bare code and a code carrying a payload. The second
    arm is what stops `trusted_issue_mismatch:174!=001` reaching the detail
    line as prose.
    """
    text = str(value or "").strip().lower()
    if not text:
        return False
    return bool(REASON_CODE_RE.match(text) or REASON_CODE_WITH_PAYLOAD_RE.match(text))


def parse_reason_payload(value):
    """`gate:found!=expected` -> its parts, or None.

    Named for what the producer actually emits rather than for how the
    punctuation reads; see GATE_SENTENCES.
    """
    match = REASON_CODE_WITH_PAYLOAD_RE.match(str(value or "").strip().lower())
    if not match:
        return None
    found = match.group("left").strip()
    expected = match.group("right").strip()
    if not found and not expected:
        return None
    return {"gate": match.group("gate"), "found": found, "expected": expected}


def _normalize(value):
    return str(value or "").strip().lower().replace("-", "_")


def derive_label(token):
    """Sentence-case a token we have no phrasing for.

    Sentence case, not title case. "Slskd transfer missing staged file repeat"
    still reads as a leaked identifier, which is the honest outcome; title case
    ("Slskd Transfer Missing Staged File Repeat") dresses it as a considered
    English sentence and is what made this worth fixing.
    """
    text = _normalize(token).replace("_", " ").strip()
    if not text:
        return ""
    return text[:1].upper() + text[1:]


def reason_presentation(row):
    """`{label, tone, detail, label_source}` for one Manual Review row.

    ``label``   -- always non-empty when any reason exists. Safe to show.
    ``tone``    -- "warn" or "bad", for the badge.
    ``detail``  -- longer prose, or "" when every candidate was an identifier.
                   Never contains a reason code.
    ``label_source`` -- "mapped" | "prose" | "derived".
    """
    row = row if isinstance(row, dict) else {}
    raw = _normalize(row.get("review_reason") or row.get("reason"))

    detail = ""
    for candidate in (
        row.get("review_reason"),
        row.get("reason"),
        row.get("why_not_grabbed"),
        row.get("activity_summary"),
    ):
        text = str(candidate or "").strip()
        if not text or looks_like_reason_code(text):
            continue
        detail = text
        break

    if not raw:
        # No reason code at all. Any prose we found is the whole story.
        if detail:
            return {"label": detail, "tone": "warn", "detail": "", "label_source": "prose"}
        return {"label": "", "tone": "", "detail": "", "label_source": ""}

    payload = parse_reason_payload(raw)
    if payload:
        # A code with a payload: give the badge a sentence that says which
        # number is which, and leave the detail line empty rather than
        # repeating the raw token underneath it.
        template = GATE_SENTENCES.get(payload["gate"])
        if template and payload["found"] and payload["expected"]:
            label = template.format(expected=payload["expected"], found=payload["found"])
        else:
            mapped = REASON_META.get(payload["gate"])
            label = mapped[0] if mapped else derive_label(payload["gate"])
        tone = (REASON_META.get(payload["gate"]) or (None, "bad"))[1]
        return {"label": label, "tone": tone, "detail": "", "label_source": "payload"}

    known = REASON_META.get(raw)
    if known:
        label, tone = known
        # Do not repeat the label as its own detail line.
        return {
            "label": label,
            "tone": tone,
            "detail": detail if detail.strip().lower() != label.strip().lower() else "",
            "label_source": "mapped",
        }

    if not looks_like_reason_code(raw):
        # The reason field already held prose.
        return {"label": str(row.get("review_reason") or row.get("reason") or "").strip(), "tone": "warn", "detail": "", "label_source": "prose"}

    return {
        "label": derive_label(raw),
        "tone": "bad" if _BAD_TONE_RE.search(raw) else "warn",
        "detail": detail,
        "label_source": "derived",
    }


def annotate_row(row):
    """Attach the presentation fields to a row in place, and return it."""
    if not isinstance(row, dict):
        return row
    presentation = reason_presentation(row)
    row["reason_label"] = presentation["label"]
    row["reason_tone"] = presentation["tone"]
    row["reason_detail"] = presentation["detail"]
    row["reason_label_source"] = presentation["label_source"]
    return row
