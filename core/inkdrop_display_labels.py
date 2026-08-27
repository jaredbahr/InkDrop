#!/usr/bin/env python3
"""One rule for turning a value into a label, and for knowing when not to.

Every display-label function in InkDrop has the same shape: look the value up
in a small table of friendly names, and on a miss, make something readable out
of it. The second half is where the bug lives. `word.title()` over an
unrecognised value assumes every value it will ever see is a lowercase machine
key, and several of the fields it is applied to carry operator data instead.

Reported live from the running build, on a staged-file row and on a release
name (both shown here against a neutral path -- the reported strings are
recorded verbatim in tests/inkdrop-display-label-free-text-smoke.py, which
does not ship):

    /media/library/staged/vol01.cbz
      -> /Media/Library/Staged/Vol01.Cbz
    ...(Minutemen PhD).cbr
      -> ...(Minutemen Phd).Cbr

The first is not the same path on Linux; copying it out of the UI gives a
broken one. The second is a release-group name that no longer matches the group
it identifies. Uglifying a label is cosmetic, but rewriting a path or a name
destroys data, and a label function has no business doing either.

WHY THIS IS A MODULE AND NOT SEVEN CORRECTIONS.
    The judgement "is this a key I may dress up, or text I must leave alone"
    was written out longhand at seven call sites. Fixing them in place would
    have produced seven copies that agree on the day they are written and
    drift afterwards -- which is how this bug arrived. It had already been
    fixed once, in core/inkdrop_review_reasons.py, in a module the affected
    field never passes through; the operator saw no change because the copy
    that actually rendered the field was untouched.

    So the rule lives here, once, and the seven sites call it. A site that
    needs different wording passes an argument. A site that needs a different
    rule does not exist yet, and if one appears, it belongs in this function.

WHAT COUNTS AS A KEY.
    Structural, not a list: a key is lowercase alphanumerics with optional
    underscore or hyphen separators, and nothing else. That admits `slskd`,
    `source_ladder` and `pack_membership_not_proven`, and refuses anything
    carrying a separator, a dot, a space, or a capital letter. The refusals are
    the point -- `PhD` is refused for its capital, and a path for its slashes.

    The path arm delegates to core/inkdrop_import_evidence.looks_like_path()
    rather than restating it, so the two cannot disagree about what a path is.
"""

from __future__ import annotations

import re

from core import inkdrop_import_evidence


# Lowercase alphanumerics, optionally separated by single underscores or
# hyphens. Anchored whole-string: a key with anything else in it is not a key.
ENUM_KEY_RE = re.compile(r"^[a-z0-9]+(?:[_-][a-z0-9]+)*$")


def looks_like_free_text(value):
    """Is this operator data rather than a machine key?

    True for paths, filenames, release-group names, prose, and anything
    carrying a capital letter. False only for a bare lowercase key.
    """
    text = str(value or "").strip()
    if not text:
        return False
    if inkdrop_import_evidence.looks_like_path(text):
        return True
    return not ENUM_KEY_RE.match(text)


def display_label(value, labels=None, *, acronyms=(), style="title", empty="", suffix=""):
    """A friendly label for a key, or the value back untouched.

    ``labels``   -- optional vocabulary, keyed lowercase. A hit wins outright.
    ``acronyms`` -- words to upper-case rather than title-case (`nzb` -> `NZB`).
    ``style``    -- "title" for `Source Ladder`, "sentence" for `Source ladder`.
    ``empty``    -- returned when the value is blank, so a caller can keep its
                    own "nothing recorded" wording.
    ``suffix``   -- appended to a DERIVED label only. Never to a value returned
                    untouched, and never to one the vocabulary supplied: a
                    caller writing full sentences has already punctuated those.

    A value that is not a key is returned exactly as it came in -- not
    stripped, not cased, not re-spaced. That is the whole point of the module,
    so it is deliberately the one branch that does no work at all.
    """
    raw = str(value or "")
    text = raw.strip()
    if not text:
        return empty

    if labels:
        mapped = labels.get(text.lower())
        if mapped:
            return mapped

    if looks_like_free_text(text):
        return raw

    words = [word for word in text.lower().replace("_", " ").replace("-", " ").split(" ") if word]
    if not words:
        return raw

    if style == "sentence":
        sentence = " ".join(words)
        return sentence[:1].upper() + sentence[1:] + suffix

    lowered_acronyms = {str(item).lower() for item in acronyms}
    return " ".join(word.upper() if word in lowered_acronyms else word.title() for word in words) + suffix
