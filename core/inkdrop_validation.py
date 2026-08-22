"""A refusal is not a fault.

Tracker #535. The API guard in inkdrop_web.py answers ~112 `/api/` routes and
prints a full traceback for every exception it catches, including the ordinary
case of a request being rejected because its input was wrong. A self-hosted
operator typing a bad value into a form gets a stack trace in their container
log, indistinguishable from a genuine crash.

WHY THIS SUBCLASSES ValueError, AND WHY THAT MATTERS MORE THAN IT LOOKS.
    Roughly 132 sites raise a bare `ValueError` to mean "the caller asked for
    something invalid", and code all over the tree already catches `ValueError`
    around them. If validation refusals were given a type outside that
    hierarchy, every one of those `except ValueError` blocks would stop
    catching them on the day the raise site converted -- a flag day, across
    routes nobody has a reason to touch together.

    Subclassing means a converted site keeps being caught by everything that
    catches it today. Conversion becomes safe to do a few routes at a time,
    which is the only reason this is affordable at all.

THIS SHIPS IN THREE DELIBERATE PHASES. DO NOT COLLAPSE THEM.
    Phase 1 (this): the guard type-checks. A ValidationError logs one line and
    returns 400. Everything else behaves exactly as it does today -- 400 *with*
    the traceback. Nothing changes for any caller, and no raise site has
    converted yet, so the observable behaviour of every route is unchanged.

    Phase 2 (separate): convert raise sites, busiest routes first. The residual
    traceback rate in the logs is the progress metric -- it says when the work
    is done instead of leaving that to judgement.

    Phase 3 (separate, deliberate, with release notes): flip the default so an
    unconverted genuine fault returns 500 rather than 400. That is the actual
    contract change, and it is the reason the first two phases must not be
    bundled with it -- bundling is what made this row look expensive enough to
    sit open.
"""

from __future__ import annotations


class ValidationError(ValueError):
    """The request was understood and refused. Nothing is broken.

    Raise this when the caller's input is invalid: a missing field, an
    unparseable value, an id that does not exist, a state transition that is
    not allowed. The operator needs the message; nobody needs the stack.

    Do NOT raise it for a genuine fault. A `TypeError` deep inside a handler,
    a failed database write, an unreachable provider -- those are exactly the
    cases whose tracebacks are worth keeping, and the guard still prints them.
    """
