"""One place that answers "do these titles name the same work?", with provenance.

Three paths answer that question today -- the Prowlarr query-confidence
predicate, the shared compatibility matcher, and the slskd file gate -- and
they answer it from different alias sets. Measured 2026-08-18: the operator
alias store (``rss-aliases.json``, written by Manual Review's Add Alias) is
read by slskd and **not** by the Prowlarr path, which builds its aliases from
a provider policy instead. So an operator could add the correct alias for a
series, watch slskd start matching it, and never learn that Prowlarr was still
emitting ``mismatch`` from the alias set it could not see -- before the shared
matcher ran at all.

This module owns the alias set and the evidence record, so all three paths can
read the same answer and say where it came from.

**Aliases are additive and auditable. Nothing here deletes tokens.** A
measured audit of the 906-row title-mismatch population found no affirmative
family for stripping articles, colons-versus-dashes, scanner suffixes,
release-group suffixes, or generic publisher prefixes -- those were
hypotheses, and acting on them would be the global fuzzy-title relaxation this
project has refused repeatedly. It would also destroy the separation between
``Geiger (2021)`` and ``Geiger (2024)``, which are different series with the
same title where the year is the only disambiguator. Every alias produced here
either came from an operator, came from a provider, or is a derivation narrow
enough to name in one line -- and each carries a provenance string so a
reviewer can see which.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

from core import inkdrop_runtime_config


CONTRACT_VERSION = 1

# What a title comparison concluded. `review` exists because the two-value
# world is what produced the defect this module addresses: "not safe to
# auto-accept" and "wrong work, refuse terminally" are different answers and
# were being collapsed into one.
OUTCOME_COMPATIBLE = "compatible"
OUTCOME_REVIEW = "review"
OUTCOME_BLOCKED = "blocked"
OUTCOMES = (OUTCOME_COMPATIBLE, OUTCOME_REVIEW, OUTCOME_BLOCKED)

AUTHORITY_PROVIDER_QUERY = "provider_query_confidence"
AUTHORITY_SHARED_MATCHER = "shared_compatibility"
AUTHORITY_SLSKD_FILE = "slskd_file_identity"

PROVENANCE_CANONICAL = "canonical"
PROVENANCE_OPERATOR = "operator_alias_store"
PROVENANCE_PROVIDER = "provider_metadata"
PROVENANCE_BRANDING_PREFIX = "branding_prefix"

# Branding a publisher puts in front of a catalogue title that no release ever
# carries. This list is deliberately tiny and grows only on operator-proven
# evidence -- one entry, one demonstrated case. It is NOT a general
# "strip the leading word" rule: the entry adds an alias beside the canonical
# title and never removes the canonical title from matching, so a release that
# does carry the branding still matches.
#
# `nickelodeon`: the wanted metadata is "Nickelodeon Avatar: The Last
# Airbender"; no real artifact carries the prefix, so the query-confidence
# predicate emitted mismatch against every genuine copy.
BRANDING_PREFIXES = ("nickelodeon",)

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_APOSTROPHES = "'’ʼ՚＇"
# An apostrophe that follows a word character is punctuation inside a word --
# "What's" and "Whats" are the same word, and so are "dogs'" and "dogs". It is
# removed rather than treated as a separator, because splitting it produces
# ("what", "s") on one side and ("whats",) on the other, which is exactly the
# asymmetry that refused a correct release. A leading apostrophe (the first in
# "Rock 'n' Roll") has no word character before it and stays a separator, so
# that title still reads as ("rock", "n", "roll") either way it is spelled.
_INWORD_APOSTROPHE_RE = re.compile(r"(?<=\w)[" + _APOSTROPHES + r"]")


def _text(value):
    return str(value if value is not None else "").strip()


def normalized_tokens(value):
    """The token sequence a title comparison actually works on.

    Returned as a tuple so it can be persisted in evidence and compared
    literally, which is what lets a reviewer see *which* tokens differed
    rather than being told only that something did.
    """
    folded = unicodedata.normalize("NFKD", _text(value).lower())
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    folded = _INWORD_APOSTROPHE_RE.sub("", folded)
    return tuple(_TOKEN_RE.findall(folded))


def branding_prefix_alias(title):
    """The same title without a known publisher branding prefix, or ``None``.

    Only the named prefixes above, only at the start, and only when something
    substantial remains -- so this can never reduce a title to a fragment.
    """
    text = _text(title)
    if not text:
        return None, ""
    tokens = normalized_tokens(text)
    if len(tokens) < 2:
        return None, ""
    for prefix in BRANDING_PREFIXES:
        prefix_tokens = normalized_tokens(prefix)
        if not prefix_tokens or tokens[: len(prefix_tokens)] != prefix_tokens:
            continue
        remainder = tokens[len(prefix_tokens):]
        if len(remainder) < 2:
            # "Nickelodeon Rugrats" would leave one word; too thin to be a
            # safe work identity on its own.
            continue
        pattern = re.compile(r"(?i)^\W*" + re.escape(prefix) + r"\W+")
        alias = pattern.sub("", text).strip()
        if alias and normalized_tokens(alias) == remainder:
            return alias, prefix
    return None, ""


def operator_alias_file():
    return Path(inkdrop_runtime_config.state_dir()) / "rss-aliases.json"


def _alias_key_matches(key, series):
    return normalized_tokens(key) == normalized_tokens(series) and bool(normalized_tokens(series))


def operator_aliases(series, path=None):
    """Aliases an operator saved for this series, from the one shared store.

    Read directly rather than through the slskd module so the Prowlarr path
    can use it without importing a provider-specific module -- the whole
    point being that all three paths see the same list.
    """
    series = _text(series)
    if not series:
        return []
    path = Path(path) if path is not None else operator_alias_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    out = []
    for key, values in data.items():
        if not _alias_key_matches(key, series):
            continue
        if isinstance(values, list):
            candidates = values
        elif values:
            candidates = [values]
        else:
            candidates = []
        for value in candidates:
            value = _text(value)
            if value and value not in out:
                out.append(value)
    return out


def _series_title(wanted_item):
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    for key in ("series_title", "series", "manga_title", "title"):
        value = _text(wanted_item.get(key))
        if value:
            return value
    return ""


def _provider_alias_values(wanted_item):
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    out = []
    for key in ("alt_titles", "altTitles", "alternate_titles", "provider_alt_titles"):
        value = wanted_item.get(key)
        values = value if isinstance(value, list) else ([value] if value else [])
        for entry in values:
            if isinstance(entry, dict):
                entry = next((v for v in entry.values() if isinstance(v, str) and v.strip()), "")
            entry = _text(entry)
            if entry and entry not in out:
                out.append(entry)
    return out


def trusted_alias_records(wanted_item=None, *, alias_path=None):
    """Every title trusted to name this work, each with where it came from.

    Order is precedence: canonical first, then what a human saved, then what a
    provider supplied, then the one narrow derivation. Duplicates keep their
    first (strongest) provenance. Apostrophe spelling is handled in
    :func:`normalized_tokens`, not by generating alias rows for it.
    """
    series = _series_title(wanted_item)
    records = []
    seen = set()

    def add(title, provenance):
        title = _text(title)
        key = normalized_tokens(title)
        if not title or not key or key in seen:
            return
        seen.add(key)
        records.append({"title": title, "provenance": provenance})

    add(series, PROVENANCE_CANONICAL)
    for value in operator_aliases(series, path=alias_path):
        add(value, PROVENANCE_OPERATOR)
    for value in _provider_alias_values(wanted_item):
        add(value, PROVENANCE_PROVIDER)
    for record in list(records):
        alias, prefix = branding_prefix_alias(record["title"])
        if alias:
            add(alias, f"{PROVENANCE_BRANDING_PREFIX}:{prefix}")
    return records


def trusted_alias_titles(wanted_item=None, *, alias_path=None):
    return [record["title"] for record in trusted_alias_records(wanted_item, alias_path=alias_path)]


def matched_alias(observed_title, wanted_item=None, *, alias_path=None):
    """Which trusted alias the observed title contains, in precedence order.

    Containment of the ordered token sequence, which is the same shape the
    provider predicate uses -- not a similarity score. There is no threshold
    here and there never was one.
    """
    observed = normalized_tokens(observed_title)
    if not observed:
        return None
    for record in trusted_alias_records(wanted_item, alias_path=alias_path):
        alias = normalized_tokens(record["title"])
        if not alias:
            continue
        for start in range(0, len(observed) - len(alias) + 1):
            if observed[start:start + len(alias)] == alias:
                return record
    return None


def evidence(
    *,
    authority,
    observed_title,
    wanted_item=None,
    expected_titles=None,
    alias=None,
    outer_work_match=None,
    match_confidence="",
    positive_evidence=(),
    conflicts=(),
    reasons=(),
    outcome="",
):
    """One record of a title decision, shaped the same whoever made it.

    Persisted so a reviewer can see the expected title beside the observed
    one, which tokens differ, which alias matched and where it came from, and
    every co-reason -- rather than a bare ``candidate_title_mismatch`` that
    names a subsystem nobody needs to look at.
    """
    expected = [
        _text(value) for value in (expected_titles if expected_titles is not None
                                   else trusted_alias_titles(wanted_item))
        if _text(value)
    ]
    observed = _text(observed_title)
    observed_tokens = normalized_tokens(observed)
    expected_tokens = normalized_tokens(expected[0]) if expected else ()
    record = {
        "title_identity_contract_version": CONTRACT_VERSION,
        "authority": _text(authority),
        "expected_titles": expected[:8],
        "observed_title": observed,
        "expected_tokens": list(expected_tokens),
        "observed_tokens": list(observed_tokens),
        "differing_tokens": [token for token in expected_tokens if token not in observed_tokens],
        "outcome": _text(outcome) or "",
        "match_confidence": _text(match_confidence),
        "positive_evidence": [ _text(v) for v in positive_evidence if _text(v)],
        "conflicts": [_text(v) for v in conflicts if _text(v)],
        "reasons": list(dict.fromkeys(_text(v) for v in reasons if _text(v))),
    }
    if outer_work_match is not None:
        record["outer_work_match"] = bool(outer_work_match)
    if isinstance(alias, dict) and alias.get("title"):
        record["matched_alias"] = _text(alias.get("title"))
        record["matched_alias_provenance"] = _text(alias.get("provenance"))
    return record
