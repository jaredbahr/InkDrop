#!/usr/bin/env python3
"""Which ComicInfo.xml is the archive's own, and when two of them disagree.

Nested ComicInfo.xml documents are ordinary. A tagger writes one at the archive
root, a scanner leaves another inside the release folder, and the archive is
perfectly good. `inkdrop_archive_conversion` already knew this and already had
the rule, inline in its extraction loop:

    # Nested ComicInfo.xml files happen; the shallowest, shortest path
    # is the one readers treat as the archive's own.

That rule was never available to the acceptance gate, which is the one consumer
that persists a verdict into durable, content-identity keyed memory. So the gate
counted the documents instead of reading them: two meant `conflicting`, and the
branch that parses Series, Number and Volume was only reachable with exactly
one. Two documents that AGREED were recorded as an archive contradicting itself,
and the block then followed the file for a week.

Measured on the deployed build: of twenty archives refused for conflicting
member unit identity, five had multiple ComicInfo documents and all five pairs
agreed -- zero disagreed.

This module is that rule, lifted so both callers share it rather than each
carrying a copy. It answers two separate questions, and keeping them separate is
the point:

  * `own_comicinfo_name()` -- which document is authoritative. A resolution.
  * `unit_identities_disagree()` -- whether the documents make incompatible
    claims about which unit this is. A verdict, and one that must still be
    reachable: a fix that stops counting without starting to compare would
    accept an archive whose two documents really do describe different issues.
"""

MEMBER_BASENAME = "comicinfo.xml"


def is_comicinfo_member(name):
    """True for a member that is a ComicInfo.xml, at any depth."""
    text = str(name or "")
    if not text or text.endswith("/"):
        return False
    return text.replace("\\", "/").rsplit("/", 1)[-1].casefold() == MEMBER_BASENAME


def _depth_and_length(name):
    posix = str(name or "").replace("\\", "/")
    return (posix.count("/"), len(posix), posix)


def own_comicinfo_name(names):
    """The archive's own ComicInfo: shallowest path, then shortest, then stable.

    The third key is the path itself, so two documents at equal depth and equal
    length resolve the same way on every run rather than inheriting whatever
    order the archive's central directory happened to use. A resolution that
    varies between reads is not a resolution.
    """
    candidates = [name for name in (names or []) if is_comicinfo_member(name)]
    if not candidates:
        return None
    return min(candidates, key=_depth_and_length)


def _norm_field(value):
    return " ".join(str(value or "").split()).casefold()


def _norm_number(value):
    """Compare 1, 01 and 1.0 as the same number, and anything else as text."""
    text = _norm_field(value)
    if not text:
        return ""
    try:
        number = float(text)
    except (TypeError, ValueError):
        return text
    return ("%f" % number).rstrip("0").rstrip(".") or "0"


def unit_identity(series=None, number=None, volume=None):
    """The tuple two documents must agree on to be describing the same unit.

    Title and Format are deliberately excluded. They differ between taggers for
    reasons that say nothing about which unit the archive holds, and treating a
    cosmetic difference as a contradiction is the defect this module exists to
    remove, reintroduced one field over.
    """
    return (_norm_field(series), _norm_number(number), _norm_number(volume))


def unit_identities_disagree(identities):
    """True only when two documents make DIFFERENT non-empty claims.

    A document that states nothing is not disagreeing with one that does -- it
    is silent, and silence is not a contradiction. Only populated fields are
    compared, field by field, so a sparse root document and a detailed nested
    one agree as long as they never contradict.
    """
    seen = [tuple(identity) for identity in (identities or [])]
    if len(seen) < 2:
        return False
    for position in range(3):
        stated = {identity[position] for identity in seen if identity[position]}
        if len(stated) > 1:
            return True
    return False
