"""One authority for "this release is not an exact single copy -- now what?".

Five refusal codes were answering that same question independently --
``collected_edition_disallowed``, ``pack_requires_review``,
``missing_required_unit_number``, ``coverage_not_unit_number`` and
``wrong_unit_type``. Measured across the 1,289-item rejected backlog on
2026-08-18 they cover 127, 570, 510, 425 and 463 items respectively. They are
one policy surface, so they get one resolver rather than a sixth answer.

**The defect this exists to make impossible.** ``collected_edition_disallowed``
was permanently on. Its escape hatch, ``allow_collected_edition``, was read in
exactly one place and written by no producer anywhere -- so the gate read an
absent key, got falsy, and refused. A flag nobody sets is not a policy; it is
the most restrictive behaviour wearing a policy's clothes. The product rule
is the opposite: content first, edition does not matter, preference is a user
setting defaulting to both, and it is never retroactive.

So the shape here is deliberate:

* :func:`resolve` always returns a **complete** policy. There is no partial
  policy and no caller-supplied default, because "caller forgot" is exactly
  how the original bug happened.
* :func:`require_complete` raises rather than filling a gap in. A missing key
  is a bug in the producer, and it should surface where it was introduced
  instead of silently becoming a refusal three subsystems away.
* Severity is a value (``admit`` / ``review`` / ``refuse``), not a boolean.
  The original gate could only block or not block, which is why "surface this
  to an operator" was unreachable and 127 items went silent.

**Pack containment, and why this value is what it is.** Whether a multi-unit
pack may be auto-grabbed because it declares a range containing the wanted
unit is tracker #209: the 2026-08-15 ruling is that any needed unit justifies
taking the whole pack. The proof that implements it lives in
:func:`inkdrop_candidate_matching.pack_title_range_membership`, and the
reason it was held -- a range proof that accepted the wrong series,
``Berserk v13`` against ``Berserk of Gluttony v01-13`` -- is answered upstream
of it: the provider classifier stamps that pair ``related_series_identity``
on the shipping bytes (run 2026-09-04, ``Berserk v01-40`` reading
``title_match`` as the control), and the proof declines any candidate whose
confidence is a mismatch or a related series.

Decided 2026-09-16 by the owner: the hold is lifted. ``pack_containment`` is
ADMIT, and so is ``collected_edition`` -- content first -- but only where the
release has PROVED it holds the wanted unit: a file manifest naming it, a
declared range holding it, or (for an issue or chapter target) the release
naming that exact number. A collected edition that cannot prove its contents
-- a bare ``Omnibus`` with no range, no manifest and no unit number -- takes
``unidentified_unit`` (REVIEW) instead, so an unproven release is shown to an
operator and never admitted on a claim. The branch that composes these lives
in :func:`inkdrop_candidate_matching.candidate_compatibility`, and
``tests/inkdrop-acquisition-policy-authority-smoke.py`` arm B2 pins both
halves with ``Geiger Omnibus 001-010`` against issue 5 (proven: admitted) and
``Geiger Omnibus`` against issue 5 (unproven: review). The operator setting
still wins in the strict direction: a stored ``review`` or ``refuse`` applies
to the proven case too.
"""

from __future__ import annotations


ACQUISITION_POLICY_VERSION = 1

# What a gate may do about a shortfall. Ordered least to most restrictive.
ADMIT = "admit"
REVIEW = "review"
REFUSE = "refuse"
SEVERITIES = (ADMIT, REVIEW, REFUSE)

# Which unit an operator wants a series in. The default is both (tracker #51):
# completeness beats format preference in the moment, and preference is
# expressed later as replacement, never as a refusal at acquisition.
UNIT_PREFERENCE_BOTH = "both"
UNIT_PREFERENCE_ISSUES = "issues"
UNIT_PREFERENCE_VOLUMES = "volumes"
UNIT_PREFERENCE_CHAPTERS = "chapters"
UNIT_PREFERENCES = (
    UNIT_PREFERENCE_BOTH,
    UNIT_PREFERENCE_ISSUES,
    UNIT_PREFERENCE_VOLUMES,
    UNIT_PREFERENCE_CHAPTERS,
)

SETTING_UNIT_PREFERENCE = "media_management.unit_preference"
SETTING_COLLECTED_EDITION = "media_management.collected_edition_policy"
# The same setting as stored in the media-management settings dict, which is
# keyed by short name. resolve() must bind either shape: a caller passing the
# settings context (short keys) and a caller passing flat dotted keys are both
# legitimate, and accepting only one is how this setting stayed unreachable.
SETTING_COLLECTED_EDITION_SHORT = "collected_edition_policy"

# Where a producer that HAS a db_path leaves the instance settings for a
# producer that does not. The matching path is a chain of pure
# ``(candidate, item)`` functions with no database in scope, so the snapshot
# rides the wanted row -- but it is read at the SETTINGS precedence position,
# never as a wanted-row value. Stamping these keys onto the row directly would
# put an instance-wide setting ahead of a per-row and per-series override and
# silently invert the documented precedence.
SETTINGS_SNAPSHOT_KEY = "acquisition_settings_snapshot"

# A collected edition that contains the wanted unit is content, and content
# comes first (owner ruling 2026-09-16, tracker #209). The matcher applies
# this only once containment is proven -- manifest, range or exact unit
# number -- and composes an unproven edition with UNIDENTIFIED_UNIT_DEFAULT
# below, so "admit" never grabs a release on the strength of its title alone.
COLLECTED_EDITION_DEFAULT = ADMIT

# Tracker #209, decided 2026-09-16: a range that provably holds the wanted
# unit justifies the whole pack. Kept separate from the edition question on
# purpose: whether an omnibus is an acceptable EDITION and whether a
# range-spanning release CONTAINS the wanted unit are two answers, and the
# proof for the second (pack_title_range_membership) declines any candidate
# whose series confidence is a mismatch or a related series.
PACK_CONTAINMENT_DEFAULT = ADMIT

# A release that names no unit at all is not an edition question -- there is
# no evidence of what it contains. It stays a review rather than a refusal so
# it remains visible, but it is never admitted automatically.
UNIDENTIFIED_UNIT_DEFAULT = REVIEW

POLICY_KEYS = (
    "unit_preference",
    "collected_edition",
    "pack_containment",
    "unidentified_unit",
)

DEFAULTS = {
    "unit_preference": UNIT_PREFERENCE_BOTH,
    "collected_edition": COLLECTED_EDITION_DEFAULT,
    "pack_containment": PACK_CONTAINMENT_DEFAULT,
    "unidentified_unit": UNIDENTIFIED_UNIT_DEFAULT,
}


class IncompleteAcquisitionPolicy(ValueError):
    """A producer built a policy with a key missing.

    Raised rather than defaulted on purpose: the whole reason this module
    exists is that an absent policy key silently became the most restrictive
    behaviour.
    """


def _text(value):
    return str(value if value is not None else "").strip().lower()


def _severity(value, fallback):
    text = _text(value)
    if text in SEVERITIES:
        return text
    # Legacy boolean shapes: the old flag said "allowed", which only ever
    # meant admit-or-refuse because it had no third state.
    if value is True:
        return ADMIT
    if value is False:
        return REFUSE
    return fallback


def _unit_preference(value, fallback=UNIT_PREFERENCE_BOTH):
    text = _text(value)
    if text in UNIT_PREFERENCES:
        return text
    # "volume"/"chapter"/"issue" singulars appear in wanted rows and overrides.
    singulars = {
        "volume": UNIT_PREFERENCE_VOLUMES,
        "chapter": UNIT_PREFERENCE_CHAPTERS,
        "issue": UNIT_PREFERENCE_ISSUES,
        "issues_and_volumes": UNIT_PREFERENCE_BOTH,
    }
    return singulars.get(text, fallback)


def _first_present(sources, *keys):
    """First key present in any source, in source order. Absence is not False.

    ``.get()`` returning ``None`` and a producer explicitly writing ``False``
    are different statements, and collapsing them is the original bug.
    """
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in keys:
            if key in source and source[key] is not None:
                return source[key]
    return None


def resolve(wanted_item=None, settings=None, series=None):
    """The complete policy for one acquisition decision.

    Precedence is narrowest-first: an explicit per-wanted-row override beats a
    per-series one, which beats the instance setting, which beats the shipped
    default. Every key is populated, always.

    An explicit ``settings=`` always wins over the carried snapshot: a caller
    that knows its settings is more specific than a producer's projection.
    """
    settings_source = "explicit" if isinstance(settings, dict) and settings else "default"
    if settings is None and isinstance(wanted_item, dict):
        carried = wanted_item.get(SETTINGS_SNAPSHOT_KEY)
        if isinstance(carried, dict) and carried:
            # Third position: instance settings. Not first -- see the note on
            # SETTINGS_SNAPSHOT_KEY.
            settings = carried
            settings_source = "snapshot"
    sources = [wanted_item, series, settings]
    policy = {
        "acquisition_policy_version": ACQUISITION_POLICY_VERSION,
        # Not one of POLICY_KEYS: never required, never validated, purely a
        # diagnostic so a caller passing settings=None with no snapshot on the
        # row is visible in the output instead of indistinguishable from a
        # caller that deliberately wants the shipped default. See tracker #588.
        "settings_source": settings_source,
        "unit_preference": _unit_preference(
            _first_present(sources, "unit_preference", SETTING_UNIT_PREFERENCE, "manga_unit_preference"),
            DEFAULTS["unit_preference"],
        ),
        "collected_edition": _severity(
            _first_present(
                sources,
                "collected_edition",
                SETTING_COLLECTED_EDITION,
                SETTING_COLLECTED_EDITION_SHORT,
                # The dead flags, still honoured where something does set them
                # so this is a strict widening rather than a behaviour swap.
                "allow_collected_edition",
                "collected_editions_allowed",
            ),
            DEFAULTS["collected_edition"],
        ),
        "pack_containment": _severity(
            _first_present(sources, "pack_containment", "allow_multi_unit_pack"),
            DEFAULTS["pack_containment"],
        ),
        "unidentified_unit": _severity(
            _first_present(sources, "unidentified_unit"),
            DEFAULTS["unidentified_unit"],
        ),
    }
    # An operator who marked a wanted row edition-indifferent has already made
    # the collected-edition call for that row.
    if _first_present(sources, "edition_indifferent") is True:
        policy["collected_edition"] = ADMIT
    require_complete(policy)
    return policy


def is_complete(policy):
    return isinstance(policy, dict) and all(
        policy.get(key) not in (None, "") for key in POLICY_KEYS
    )


def require_complete(policy):
    """Raise unless every policy key is populated.

    Deliberately not "fill in the missing ones": a producer that ships an
    incomplete policy has a bug, and defaulting it here would reproduce the
    exact failure this module was written to end.
    """
    if not isinstance(policy, dict):
        raise IncompleteAcquisitionPolicy(f"acquisition policy must be a dict, got {type(policy).__name__}")
    missing = [key for key in POLICY_KEYS if policy.get(key) in (None, "")]
    if missing:
        raise IncompleteAcquisitionPolicy(f"acquisition policy is missing: {', '.join(sorted(missing))}")
    for key in ("collected_edition", "pack_containment", "unidentified_unit"):
        if policy[key] not in SEVERITIES:
            raise IncompleteAcquisitionPolicy(f"{key} must be one of {SEVERITIES}, got {policy[key]!r}")
    if policy["unit_preference"] not in UNIT_PREFERENCES:
        raise IncompleteAcquisitionPolicy(
            f"unit_preference must be one of {UNIT_PREFERENCES}, got {policy['unit_preference']!r}"
        )
    return policy


def stricter(*severities):
    """The most restrictive of several answers.

    Used where two policy questions apply at once and the safe composition is
    the stricter one -- never an average, and never the first one checked.
    """
    order = {ADMIT: 0, REVIEW: 1, REFUSE: 2}
    best = ADMIT
    for value in severities:
        if value not in order:
            raise IncompleteAcquisitionPolicy(f"not a severity: {value!r}")
        if order[value] > order[best]:
            best = value
    return best


def severity_for(policy, aspect):
    """What this policy does about one shortfall. Never guesses."""
    require_complete(policy)
    if aspect not in ("collected_edition", "pack_containment", "unidentified_unit"):
        raise IncompleteAcquisitionPolicy(f"unknown policy aspect {aspect!r}")
    return policy[aspect]


def accepts_unit_type(policy, unit_type):
    """Whether the operator's unit preference admits this unit.

    Per the #51 ruling this is never a refusal at acquisition time -- a
    preference is expressed later as replacement -- so a non-preferred unit is
    still accepted. The value is here for the replacement pass to read, and
    for the setting to mean something without also becoming a gate.
    """
    require_complete(policy)
    unit = _text(unit_type)
    preference = policy["unit_preference"]
    if preference == UNIT_PREFERENCE_BOTH or not unit:
        return True, "both units accepted"
    preferred = {
        UNIT_PREFERENCE_ISSUES: {"issue", "comic_issue"},
        UNIT_PREFERENCE_VOLUMES: {"volume", "vol", "book_volume", "manga_volume"},
        UNIT_PREFERENCE_CHAPTERS: {"chapter", "manga_chapter"},
    }[preference]
    if unit in preferred:
        return True, f"matches the {preference} preference"
    return True, f"accepted despite the {preference} preference; preference is applied as replacement, never refusal"
