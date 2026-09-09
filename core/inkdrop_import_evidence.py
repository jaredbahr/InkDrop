#!/usr/bin/env python3
"""The inputs a person needs to decide an import, as structured data.

Reported live 2026-08-20: Needs Attention cards read

    Trusted Issue Mismatch:198!=001
    Source Target Identity Mismatch

and the operator's words were "we need to ensure that it says what series and
expected item was. then what we found so a user can decide the correct action."
That is a request for decision inputs, not for friendlier wording.

TWO BARE NUMBERS, AND THE ORIENTATION IS THE OPPOSITE OF THE OBVIOUS READING.
    The producer is `trusted_issue_mismatch_reason()` in
    core/inkdrop_completed_import.py:7797:

        return f"trusted_issue_mismatch:{actual or 'unknown'}!={expected}"

    `actual` is parsed out of the file path; `expected` is the issue the queue
    asked for. So in `198!=001` the LEFT number is what was found and the RIGHT
    is what was wanted. A person reading "198 != 001" will almost always assume
    the first is the target. The bare form is not merely terse -- it invites the
    wrong decision, which is worse than saying nothing.

    That orientation is pinned by test. If the producer's format ever flips,
    the test fails rather than the UI quietly lying.

WHY THE COMPOSITE FORM ALSO DEFEATS THE REASON-CODE GUARD
    `REASON_CODE_RE` (the shared guard that stops identifiers being printed as
    prose) is anchored to the WHOLE string: `^[a-z0-9]+(?:_[a-z0-9]+)+$`. The
    `:198!=001` suffix breaks that anchor, so `trusted_issue_mismatch:198!=001`
    is not recognised as an identifier and is passed through as though it were
    prose. The defect is the anchor, not a missing lookup entry -- any reason
    that carries a payload after the code has always slipped past.

A PATH IS NOT A SOURCE NAME
    The row's `source` field carries two different kinds of value: a provider
    id (`slskd`) and, on staged-file rows, a filesystem path. Running the second
    through a source-naming function mutates it --
    `source_display_label('/media/library/staged/vol01.cbz')` returns
    `'/Media/Library/Staged/Vol01.Cbz'`, which on Linux is not a valid path. Anyone
    copying it out of the UI gets a broken one. Uglifying a label is cosmetic;
    mutating a path destroys data, so this module classifies the value and
    leaves paths alone.
"""

from __future__ import annotations

import posixpath
import re


# `gate:left!=right`. Deliberately narrow: only the shape the producers
# actually emit, so an unrelated colon in prose is not misread as a gate.
COMPOSITE_REASON_RE = re.compile(r"^(?P<gate>[a-z0-9]+(?:_[a-z0-9]+)+):(?P<left>[^!]*)!=(?P<right>.*)$")

# A bare identifier, anchored whole-string -- the same rule the reason
# vocabulary uses. Kept here so this module can classify without importing the
# vocabulary, and so the composite case above can be handled BEFORE this rule
# gets a chance to mis-classify it as prose.
BARE_CODE_RE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)+$")

# Sentences for the gates we have seen. The point of each is to say what
# disagreed, in terms of the decision the operator has to make.
GATE_EXPLANATIONS = {
    "trusted_issue_mismatch": (
        "The issue number in the file's name is not the issue this download was for."
    ),
    "source_target_identity_mismatch": (
        "The file does not look like it belongs to the series this download was for."
    ),
    "trusted_issue_missing_source_number": (
        "The file's name carries no issue number, so it cannot be checked against the one that was wanted."
    ),
}


def looks_like_path(value):
    """Is this a filesystem path rather than a provider name?

    Deliberately structural rather than a list of known roots: a path is
    anything with a separator, and a provider id never has one.
    """
    text = str(value or "").strip()
    if not text:
        return False
    return "/" in text or "\\" in text


# Extensions the importer actually handles. A bare name carrying one of these IS
# identity even though it has no separator -- `Berserk_Vol.42.cbz` is the whole of
# what some staging rows know about themselves.
ARTIFACT_EXTENSIONS = {
    ".cbz", ".cbr", ".cb7", ".cbt", ".zip", ".rar", ".7z",
    ".pdf", ".epub", ".mobi", ".azw3",
}


def looks_like_artifact(value):
    """A bare artifact name: no separator, but an extension the importer handles.

    looks_like_path() answers for anything with a separator. This answers for the
    other half of the same question, so a caller can ask "does this string carry
    identity?" without treating a provider id like `slskd` as a file name.
    """
    text = str(value or "").strip()
    if not text or looks_like_path(text):
        return False
    _stem, _dot, suffix = text.rpartition(".")
    return bool(_dot) and ("." + suffix.lower()) in ARTIFACT_EXTENSIONS


def path_leaf(value):
    """The last segment of a path, whichever separator it uses.

    posixpath.basename() splits on "/" only, and an SLSKD peer path is
    backslash-separated (`@@adarr\\Literature\\Comics\\...\\Steel v2 #06.cbz`),
    so it returned the whole path where a file name belonged. looks_like_path()
    directly above already accepts either separator; this is the same judgement
    applied to the split.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    return re.split(r"[\\/]", text)[-1]


def parse_composite_reason(value):
    """`trusted_issue_mismatch:198!=001` -> gate/found/expected, or None.

    LEFT IS FOUND, RIGHT IS EXPECTED. See the module docstring; this follows
    the producer at core/inkdrop_completed_import.py:7797 and is pinned by
    test rather than inferred from the punctuation.
    """
    text = str(value or "").strip()
    match = COMPOSITE_REASON_RE.match(text)
    if not match:
        return None
    found = match.group("left").strip()
    expected = match.group("right").strip()
    if not found and not expected:
        return None
    return {
        "gate": match.group("gate"),
        "found": found,
        "expected": expected,
    }


def looks_like_reason_identifier(value):
    """True for a bare code AND for a code carrying a payload.

    The whole-string anchor is why `trusted_issue_mismatch:198!=001` was being
    treated as prose. Recognising the composite form here is what closes that.
    """
    text = str(value or "").strip().lower()
    if not text:
        return False
    if BARE_CODE_RE.match(text):
        return True
    return bool(COMPOSITE_REASON_RE.match(text))


def _first_text(row, keys):
    for key in keys:
        text = str(row.get(key) or "").strip()
        if text:
            return text
    return ""


def decision_evidence(row):
    """What was wanted, what was found, and why they disagree.

    Returns a dict with `expected`, `found`, `disagreement` and `gate`. Every
    field is either populated from the row or left empty -- nothing is
    inferred, and an absent value is reported absent rather than guessed at,
    because a confident wrong "expected" is worse for a decision than a blank.
    """
    row = row if isinstance(row, dict) else {}

    raw_reason = _first_text(row, ("review_reason", "reason"))
    parsed = parse_composite_reason(raw_reason)
    gate = parsed["gate"] if parsed else (raw_reason if BARE_CODE_RE.match(raw_reason.lower()) else "")

    source_value = _first_text(row, ("source", "current_source"))
    source_is_path = looks_like_path(source_value)

    expected = {
        "series": str(row.get("series") or "").strip(),
        # The unit the queue asked for. From the gate's own payload when it has
        # one, else the row's issue number.
        "unit": (parsed or {}).get("expected") or str(row.get("issue_number") or "").strip(),
        # Some writers record their expectation as a sentence rather than a
        # series and a unit -- a save path, a destination folder. Before this
        # they had nowhere to put it, so `incomplete` went true and the card
        # said "InkDrop did not record what it expected for this item" about a
        # row whose own `detail` read "qBittorrent save_path=..., expected one
        # of [...]". The record held the expectation; only this shape could not
        # carry it. Verbatim, like every other field here.
        "text": _first_text(row, ("detail",)),
    }

    # The candidate this row is about, when the writer recorded one. A
    # circuit-breaker row's `source` is the provider id ("slskd"), not a file,
    # so before this the found side of those rows was always empty -- see
    # repeat_bad_candidate_review_row(). Read first, because it names the
    # artifact directly where `source` only sometimes does.
    candidate_path = _first_text(row, ("candidate_path",))
    found_path = candidate_path or (source_value if source_is_path else "")
    # 65.6% of bad_source_candidates carry a source_path; 100% carry a title
    # (2026-08-25 snapshot, 10,444 rows). The title is what the candidate is
    # called -- a file or folder name off the peer, e.g. "The Wicked + The
    # Divine 1923 1 (2018).cbz" -- so a row with no path still has something
    # true to put opposite the expected side. `path` stays empty for those: it
    # is a path field, and a name is not a path.
    found = {
        # Verbatim. Never through a label function -- see the module docstring.
        "path": found_path,
        "file_name": path_leaf(found_path) or _first_text(row, ("candidate_title",)),
        "unit": (parsed or {}).get("found", ""),
    }

    disagreement = GATE_EXPLANATIONS.get(gate, "")
    if parsed and gate == "trusted_issue_mismatch" and parsed["found"] and parsed["expected"]:
        disagreement = (
            f"This download was for issue {parsed['expected']}, but the file is named as issue "
            f"{parsed['found']}."
        )

    return {
        "gate": gate,
        "expected": expected,
        "found": found,
        "disagreement": disagreement,
        # So a surface can render a path as a path (wrapping, copyable) and a
        # provider as a pill, instead of one rule mangling the other.
        "source_is_path": source_is_path,
        "source_name": "" if source_is_path else source_value,
        # True when we could not say what was wanted or what was found. A card
        # should say "no evidence recorded" rather than render empty fields
        # that read as "nothing was expected".
        # file_name counts: a candidate InkDrop can name is a candidate the
        # operator can judge, whether or not a full path was recorded.
        # A recorded statement settles it: if the writer said what it
        # expected, in whatever shape, the card must not claim nothing was
        # recorded. Absent that, the original rule stands unchanged -- a row
        # that genuinely recorded neither side still says so, which is the
        # honest message this flag exists to produce.
        "incomplete": not expected["text"]
        and (
            not (expected["series"] or expected["unit"])
            or not (found["path"] or found["unit"] or found["file_name"])
        ),
    }


def annotate_row(row):
    """Attach `decision_evidence` to a row in place, and return it."""
    if not isinstance(row, dict):
        return row
    row["decision_evidence"] = decision_evidence(row)
    return row
