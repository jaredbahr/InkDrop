#!/usr/bin/env python3
"""A typed, versioned release date: what kind of date, and how precisely known.

InkDrop stores release dates as bare strings and compares them with `!=`. Two
things go wrong with that, and both are live on qa today.

**Precision.** `issues.release_date` is 80% `YYYY-MM-DD` and 14% a timezone-aware
ISO instant (MangaDex `publishAt`), while `series.year` is year-only. Comparing
across those means comparing a day to a year. `inkdrop_release_calendar._iso_day`
already works around this by discarding anything shorter than 10 characters and
counting it as "unreadable" -- a bare year is a real release date at lower
precision, not a broken one.

**Semantics.** `inkdrop_state.record_issue` collapses ten provider fields into
one column:

    date, release_date, releaseDate, store_date, storeDate,
    cover_date, coverDate, publishAt, publishedAt, readableAt

Those are not the same date. A store date is when a book reached shops; a cover
date is printed on the cover and by publishing convention sits about two months
later (`inkdrop_release_calendar`'s own module docstring says so); `publishAt` is
the instant a chapter became readable. After ingestion the distinction is gone --
6,656 of 6,666 rows on prod keep `date: None` in `raw_json`, so which concept a
stored value represents is unrecoverable.

The cost is not hypothetical. The series-folder year guard compares
`series.year` against the year in a folder name. For manga those are different
concepts: `series.year` is the original Japanese serialization year and the
folder carries the English edition year. Measured on prod, 14 manga series --
Battle Angel Alita 1990 vs `Battle Angel Alita (1993)`, Vinland Saga 2005 vs
`(2013)`, Dorohedoro 2000 vs `(2010)` -- were judged "different year, not this
series" when the honest answer is "these two numbers do not measure the same
thing." Zero comics are affected, because there both numbers do mean the same
thing.

So comparison here is deliberately **tri-state**: True, False, or None for "not
comparable." Collapsing None into False is the bug this module exists to remove.

Versioned as `inkdrop.release_date.v1`. This mirrors the record pattern in
`core/inkdrop_records.py` (PR #667, download identity/status) and should be
folded into that module once it lands; kept separate here only so the two
branches do not both create the same file.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass


SCHEMA = "inkdrop.release_date.v1"


class ReleaseDateShapeError(ValueError):
    """A release date was built with a value its contract does not allow."""


# --------------------------------------------------------------------------
# Precision
# --------------------------------------------------------------------------

# Ordered coarse -> fine. Two dates are compared at the coarsest precision they
# both actually have, so a year-only value and a full date can still answer
# "same year?" without either pretending to know more than it does.
PRECISIONS = ("unknown", "year", "month", "day", "instant")
_PRECISION_RANK = {name: index for index, name in enumerate(PRECISIONS)}


# --------------------------------------------------------------------------
# Semantics
# --------------------------------------------------------------------------

# What the date is actually measuring.
SEMANTICS = (
    "publication",  # when the work first appeared (JP serialization, original run)
    "edition",      # when this edition or printing was released
    "store",        # when it went on sale
    "cover",        # printed on the cover; leads the store date by ~2 months
    "available",    # when it became readable (MangaDex publishAt / readableAt)
    "unknown",      # provenance was not recorded -- comparable with nothing
)

# Two dates can only be compared when they measure the same kind of thing.
# `store`, `cover`, `edition` and `available` all describe *this release*, so
# they share a family. `publication` describes the underlying work and is a
# different question entirely. `unknown` joins no family: a value whose origin
# was not recorded cannot be used to contradict anything.
_FAMILY = {
    "publication": "work",
    "edition": "release",
    "store": "release",
    "cover": "release",
    "available": "release",
    "unknown": "",
}

# Which provider key means what. This is the one place the mapping lives; the
# ingestion boundary reads keys in this order and records which one won, so the
# semantic survives instead of being flattened into an undifferentiated string.
PROVIDER_DATE_KEYS = (
    ("store_date", "store"),
    ("storeDate", "store"),
    ("publishAt", "available"),
    ("publishedAt", "available"),
    ("readableAt", "available"),
    ("release_date", "edition"),
    ("releaseDate", "edition"),
    ("cover_date", "cover"),
    ("coverDate", "cover"),
    ("date", "unknown"),
)


def require_semantic(value):
    text = str(value or "").strip().lower()
    if text not in SEMANTICS:
        raise ReleaseDateShapeError(
            f"{text or '(empty)'!r} is not a release-date semantic; "
            f"expected one of {', '.join(SEMANTICS)}"
        )
    return text


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

_YEAR = re.compile(r"^(\d{4})$")
_YEAR_MONTH = re.compile(r"^(\d{4})[-/](\d{1,2})$")
_YMD = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$")


def _valid(year, month=None, day=None):
    if year is None or not (1 <= int(year) <= 9999):
        return False
    if month is not None and not (1 <= int(month) <= 12):
        return False
    if day is not None:
        try:
            _dt.date(int(year), int(month), int(day))
        except ValueError:
            return False
    return True


@dataclass(frozen=True)
class ReleaseDate:
    """One release date, carrying how precisely and in what sense it is known.

    Build these with `from_value` or `from_payload` -- those are the
    normalization boundary. Constructing one directly is allowed but validates
    strictly, so an impossible date or an unrecognized semantic raises here
    rather than turning into a wrong comparison later.
    """

    year: int | None = None
    month: int | None = None
    day: int | None = None
    instant: str = ""
    precision: str = "unknown"
    semantic: str = "unknown"
    source: str = ""

    def __post_init__(self):
        object.__setattr__(self, "semantic", require_semantic(self.semantic))
        object.__setattr__(self, "source", str(self.source or "").strip())
        object.__setattr__(self, "instant", str(self.instant or "").strip())
        precision = str(self.precision or "unknown").strip().lower()
        if precision not in PRECISIONS:
            raise ReleaseDateShapeError(
                f"{precision!r} is not a precision; expected one of {', '.join(PRECISIONS)}"
            )
        object.__setattr__(self, "precision", precision)
        if precision == "unknown":
            object.__setattr__(self, "year", None)
            object.__setattr__(self, "month", None)
            object.__setattr__(self, "day", None)
            return
        if not _valid(self.year, self.month if precision in ("month", "day", "instant") else None,
                      self.day if precision in ("day", "instant") else None):
            raise ReleaseDateShapeError(
                f"{self.year!r}-{self.month!r}-{self.day!r} is not a real date at {precision} precision"
            )

    # -- state ------------------------------------------------------------

    @property
    def known(self):
        return self.precision != "unknown"

    def __bool__(self):
        return self.known

    @property
    def comparable(self):
        """A date with no family cannot contradict anything."""
        return self.known and bool(_FAMILY.get(self.semantic))

    def iso(self):
        if self.precision == "instant" and self.instant:
            return self.instant
        if self.precision in ("day", "instant"):
            return f"{self.year:04d}-{self.month:02d}-{self.day:02d}"
        if self.precision == "month":
            return f"{self.year:04d}-{self.month:02d}"
        if self.precision == "year":
            return f"{self.year:04d}"
        return ""

    # -- boundary ---------------------------------------------------------

    @classmethod
    def unknown(cls, *, semantic="unknown", source=""):
        return cls(precision="unknown", semantic=semantic, source=source)

    @classmethod
    def from_value(cls, value, *, semantic="unknown", source=""):
        """Parse any provider or stored date string into a typed value.

        Never raises on junk -- an unparseable value becomes an unknown date,
        which is honest and comparable with nothing. Raising here would only
        push the guessing back onto every caller.
        """
        semantic = require_semantic(semantic)
        text = str(value or "").strip()
        if not text:
            return cls.unknown(semantic=semantic, source=source)

        match = _YMD.match(text)
        if match:
            y, m, d = (int(g) for g in match.groups())
            if _valid(y, m, d):
                return cls(year=y, month=m, day=d, precision="day", semantic=semantic, source=source)
            return cls.unknown(semantic=semantic, source=source)

        match = _YEAR_MONTH.match(text)
        if match:
            y, m = int(match.group(1)), int(match.group(2))
            if _valid(y, m):
                return cls(year=y, month=m, precision="month", semantic=semantic, source=source)
            return cls.unknown(semantic=semantic, source=source)

        match = _YEAR.match(text)
        if match:
            y = int(match.group(1))
            if _valid(y):
                return cls(year=y, precision="year", semantic=semantic, source=source)
            return cls.unknown(semantic=semantic, source=source)

        # An ISO value, with or without a time. MangaDex sends full instants;
        # `fromisoformat` also accepts the basic form (``20230920``), which is
        # a plain day and must not be filed as an instant just because the same
        # parser handled it -- an instant claims a precision it does not have.
        try:
            parsed = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return cls.unknown(semantic=semantic, source=source)
        has_time = bool(re.search(r"[T ]\d{1,2}:", text))
        if not has_time:
            return cls(year=parsed.year, month=parsed.month, day=parsed.day,
                       precision="day", semantic=semantic, source=source)
        return cls(
            year=parsed.year, month=parsed.month, day=parsed.day,
            instant=text, precision="instant", semantic=semantic, source=source,
        )

    @classmethod
    def from_payload(cls, payload):
        """Read a provider issue payload, keeping which field the date came from.

        This is the ingestion boundary. `record_issue` used to `or`-chain these
        ten keys into one string, which is where the semantic was lost.
        """
        payload = payload if isinstance(payload, dict) else {}
        for key, semantic in PROVIDER_DATE_KEYS:
            raw = payload.get(key)
            if raw in (None, ""):
                continue
            parsed = cls.from_value(raw, semantic=semantic, source=key)
            if parsed.known:
                return parsed
        return cls.unknown()

    @classmethod
    def from_series_year(cls, value, *, media_type=""):
        """A series' own year, whose meaning depends on what the series is.

        For manga this is the original Japanese serialization year -- the work,
        not the edition. For comics it is the run's start year, which is the
        same thing the library folder is named after. Getting this distinction
        right is the whole reason the folder guard can stop rejecting manga on
        a year that was never comparable.
        """
        manga = str(media_type or "").strip().lower() in {"manga", "manhwa", "manhua", "webtoon"}
        return cls.from_value(value, semantic="publication" if manga else "edition",
                              source="series.year")

    @classmethod
    def from_folder_year(cls, value):
        """The year in a library folder name -- always this edition's year."""
        return cls.from_value(value, semantic="edition", source="folder_name")

    # -- comparison -------------------------------------------------------

    def same_kind_as(self, other):
        """Whether these two measure the same kind of thing at all.

        Distinct from `comparable_with`, which also requires both to carry a
        value. A folder with no year in its name is still an *edition* year --
        an absent one -- so it is the same kind as a comic's series year and a
        different kind from a manga's serialization year. Callers that want to
        keep a strict rule for same-kind pairs while relaxing it for
        different-kind pairs need that distinction, and collapsing the two
        cases is how a comic rule ends up applied to manga.
        """
        if not isinstance(other, ReleaseDate):
            return False
        family = _FAMILY.get(self.semantic)
        return bool(family) and family == _FAMILY.get(other.semantic)

    def comparable_with(self, other):
        if not isinstance(other, ReleaseDate):
            return False
        if not (self.comparable and other.comparable):
            return False
        return _FAMILY[self.semantic] == _FAMILY[other.semantic]

    def matches(self, other):
        """True / False / None, where None means "these are not comparable".

        Compared at the coarsest precision both sides actually carry, so a
        year-only value and a full date agree when the years agree. Returning
        None rather than False for an incomparable pair is the point: the
        caller has to decide what to do about not knowing, instead of silently
        being told "different".
        """
        if not self.comparable_with(other):
            return None
        rank = min(_PRECISION_RANK[self.precision], _PRECISION_RANK[other.precision])
        # An instant and a day are both day-precision for comparison purposes;
        # nothing downstream asks whether two releases happened at the same
        # second.
        rank = min(rank, _PRECISION_RANK["day"])
        if rank >= _PRECISION_RANK["day"]:
            return (self.year, self.month, self.day) == (other.year, other.month, other.day)
        if rank == _PRECISION_RANK["month"]:
            return (self.year, self.month) == (other.year, other.month)
        return self.year == other.year

    def differs_from(self, other):
        """True only when the two are comparable AND genuinely different.

        This is the predicate a guard wants. `a != b` on raw strings answers a
        different question, and answers it wrongly whenever the two values were
        never measuring the same thing.
        """
        return self.matches(other) is False

    # -- serialization ----------------------------------------------------

    def to_dict(self):
        return {
            "schema": SCHEMA,
            "value": self.iso() or None,
            "year": self.year,
            "month": self.month,
            "day": self.day,
            "precision": self.precision,
            "semantic": self.semantic,
            "source": self.source or None,
        }

    @classmethod
    def from_dict(cls, payload):
        payload = payload if isinstance(payload, dict) else {}
        if payload.get("schema") not in (None, "", SCHEMA):
            raise ReleaseDateShapeError(f"unsupported release-date schema {payload.get('schema')!r}")
        return cls.from_value(
            payload.get("value"),
            semantic=payload.get("semantic") or "unknown",
            source=payload.get("source") or "",
        )
