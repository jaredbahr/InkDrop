"""Prowlarr per-indexer availability, and what a search result does not say.

Prowlarr's ``/api/v1/search`` answers with a flat array of releases and no
per-indexer envelope. When one of the selected indexers is in failure backoff
it is simply skipped: the call still returns HTTP 200, still carries whatever
the healthy indexers found, and carries nothing at all to say a lane was
missing. Only when *every* selected indexer is unavailable does Prowlarr fail
loudly, with HTTP 400 ``"Search failed due to all selected indexers being
unavailable"``.

Measured against the live instance on 2026-08-15, indexer 15 (down) selected
alongside 47 (up):

    indexerIds=15          -> HTTP 400, no results
    indexerIds=47          -> HTTP 200, 35 results
    indexerIds=15&47       -> HTTP 200, 35 results, all from 47, no error field
    indexerIds=15&48       -> HTTP 400 (both down)

So a partial outage is indistinguishable from "nobody has this" unless we ask
a second question. That is what this module is for: the answer lives in
``/api/v1/indexerstatus``, which InkDrop had never read -- three of twelve
indexers were in backoff during that same measurement (15 DOGnzb, 45 Tokyo
Toshokan, 48 AltHub, i.e. *both* usenet providers) and nothing surfaced it.

The functions here are deliberately pure and stdlib-only so they can be
exercised against a live Prowlarr from the host without deploying anything.
"""

from __future__ import annotations

import re
import time


CONTRACT_VERSION = 1

INDEXER_STATUS_PATH = "/indexerstatus"
HEALTH_PATH = "/health"

# Prowlarr's own wording for the all-indexers-down case. Matched loosely: the
# point is to recognise the shape, not to depend on the exact sentence.
TOTAL_OUTAGE_MESSAGE_PATTERN = re.compile(
    r"all\s+selected\s+indexers\s+being\s+unavailable", re.I
)

# The IndexerStatusCheck health row enumerates unavailable indexers by *name*,
# which is the only place a name-to-outage mapping is published without a
# second lookup.
HEALTH_UNAVAILABLE_PATTERN = re.compile(
    r"indexers?\s+unavailable\s+due\s+to\s+failures?\s*:\s*(.+)", re.I
)


def _text(value):
    return str(value if value is not None else "").strip()


def _id_key(value):
    return _text(value).lower()


def _parse_timestamp(value):
    """Parse Prowlarr's ISO-8601 ``disabledTill`` into an epoch float.

    Prowlarr emits ``2026-08-16T03:00:46Z``. ``datetime.fromisoformat`` did not
    accept a trailing ``Z`` before Python 3.11, and this module is imported by
    the source worker on whatever interpreter the container ships, so the
    suffix is normalised by hand rather than trusted to the stdlib.
    """
    text = _text(value)
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        from datetime import datetime

        parsed = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        from datetime import timezone

        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def indexer_status_request_path(base_url):
    """Path for ``/indexerstatus`` honouring the ``/api/v1`` base-path quirk.

    A configured base URL may already end in ``/api/v1`` or may be the bare
    host; both shapes are in the wild, and getting this wrong reads as an
    unreachable Prowlarr rather than a malformed URL.
    """
    base = _text(base_url).rstrip("/")
    if not base:
        return ""
    if base.endswith("/api/v1"):
        return f"{base}{INDEXER_STATUS_PATH}"
    return f"{base}/api/v1{INDEXER_STATUS_PATH}"


def health_request_path(base_url):
    base = _text(base_url).rstrip("/")
    if not base:
        return ""
    if base.endswith("/api/v1"):
        return f"{base}{HEALTH_PATH}"
    return f"{base}/api/v1{HEALTH_PATH}"


def unavailable_indexer_ids(indexer_status_payload, *, now=None):
    """Indexer ids Prowlarr is currently refusing to query.

    A row exists in ``/indexerstatus`` for any indexer with a failure record,
    including ones whose backoff has already expired -- ``disabledTill`` in the
    past means recovered, not down. Rows without a parseable ``disabledTill``
    are treated as down, because a failure record with an unreadable expiry is
    still a failure record and the safe reading is "do not call this evidence
    of absence".
    """
    now = time.time() if now is None else float(now)
    out = []
    seen = set()
    rows = indexer_status_payload if isinstance(indexer_status_payload, list) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        indexer_id = _text(row.get("indexerId") or row.get("indexer_id") or row.get("id"))
        if not indexer_id:
            continue
        disabled_till = _parse_timestamp(row.get("disabledTill") or row.get("disabled_till"))
        if disabled_till is not None and disabled_till <= now:
            continue
        key = _id_key(indexer_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(indexer_id)
    return out


def health_unavailable_indexer_names(health_payload):
    """Indexer *names* named by Prowlarr's own IndexerStatusCheck health row."""
    names = []
    seen = set()
    rows = health_payload if isinstance(health_payload, list) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        match = HEALTH_UNAVAILABLE_PATTERN.search(_text(row.get("message")))
        if not match:
            continue
        for name in match.group(1).split(","):
            name = name.strip()
            key = name.lower()
            if not name or key in seen:
                continue
            seen.add(key)
            names.append(name)
    return names


def selected_indexer_ids(params):
    """Indexer ids a search request selected, or ``None`` for "every indexer".

    Prowlarr accepts ``indexerIds`` as a scalar or a repeated parameter, and
    omitting it means search everything -- which is a different claim from
    selecting nothing, so the two must not collapse to the same value.
    """
    params = params if isinstance(params, dict) else {}
    raw = params.get("indexerIds")
    if raw is None:
        raw = params.get("indexerId")
    if raw is None:
        return None
    values = raw if isinstance(raw, (list, tuple, set)) else [raw]
    out = []
    seen = set()
    for value in values:
        text = _text(value)
        key = _id_key(text)
        if not text or key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out or None


def answered_indexer_ids(rows):
    """Indexer ids that actually contributed at least one release."""
    out = []
    seen = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        indexer_id = _text(row.get("indexerId") or row.get("indexer_id"))
        key = _id_key(indexer_id)
        if not indexer_id or key in seen:
            continue
        seen.add(key)
        out.append(indexer_id)
    return out


def is_total_outage_response(status_code, body_text):
    """Whether a failed search failed because every selected indexer was down.

    This is the loud case, and it is already handled correctly upstream -- it
    surfaces as a fetch error rather than an empty result. Recognising it here
    keeps the two cases named in one place.
    """
    try:
        status_code = int(status_code)
    except (TypeError, ValueError):
        status_code = 0
    if status_code and status_code not in {400, 404, 500, 503}:
        return False
    return bool(TOTAL_OUTAGE_MESSAGE_PATTERN.search(_text(body_text)))


def indexer_coverage(selected, unavailable, answered=None):
    """What a search actually covered, and what it silently did not.

    ``selected=None`` means the request named no indexers, i.e. Prowlarr
    searched every enabled indexer -- so *any* unavailable indexer degrades it.
    """
    unavailable_keys = {_id_key(value) for value in unavailable or []}
    answered_list = list(answered or [])
    answered_keys = {_id_key(value) for value in answered_list}

    if selected is None:
        degraded_ids = [value for value in unavailable or []]
        selected_ids = []
        selected_all = True
    else:
        selected_ids = list(selected)
        selected_all = False
        degraded_ids = [value for value in selected_ids if _id_key(value) in unavailable_keys]

    # An indexer that was selected, is not in backoff, and still returned
    # nothing is a genuine zero from that lane -- worth reporting, but it is
    # evidence of absence rather than a gap in coverage.
    silent_ids = [
        value
        for value in selected_ids
        if _id_key(value) not in answered_keys and _id_key(value) not in unavailable_keys
    ]

    return {
        "contract_version": CONTRACT_VERSION,
        "selected_all_indexers": selected_all,
        "selected_indexer_ids": selected_ids,
        "unavailable_indexer_ids": degraded_ids,
        "answered_indexer_ids": answered_list,
        "silent_indexer_ids": silent_ids,
        "degraded": bool(degraded_ids),
        "fully_unavailable": bool(degraded_ids)
        and not selected_all
        and len(degraded_ids) >= len(selected_ids),
    }


def coverage_wait_reason(coverage, *, candidate_count=0):
    """The reason code for a search whose coverage cannot support "not found".

    Only zero-candidate searches are reclassified. If a release was found and
    kept, the outcome stands on its own regardless of which lanes were missing
    -- downgrading a successful search because an unrelated indexer was in
    backoff would trade one wrong answer for another.
    """
    if candidate_count:
        return ""
    coverage = coverage if isinstance(coverage, dict) else {}
    if not coverage.get("degraded"):
        return ""
    if coverage.get("fully_unavailable"):
        return "indexer_unavailable_all_selected"
    return "indexer_unavailable_partial"


INDEXER_LIST_PATH = "/indexer"


def indexer_list_request_path(base_url):
    base = _text(base_url).rstrip("/")
    if not base:
        return ""
    if base.endswith("/api/v1"):
        return f"{base}{INDEXER_LIST_PATH}"
    return f"{base}/api/v1{INDEXER_LIST_PATH}"


def declared_category_ids(indexer_row):
    """Every category id an indexer declares, parents and subcategories alike.

    Newznab categories are hierarchical: 7000 Books is the parent of 7010
    Books/Mags, 7020 Books/EBook, 7030 Books/Comics. An indexer is free to
    declare only the parent, and several here do -- Nyaa (6, 46) and Tokyo
    Toshokan (45) publish 7000 with no subcategories at all.
    """
    row = indexer_row if isinstance(indexer_row, dict) else {}
    capabilities = row.get("capabilities") if isinstance(row.get("capabilities"), dict) else {}
    out = []
    seen = set()

    def walk(entries):
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            raw = entry.get("id")
            if raw is not None:
                try:
                    value = int(raw)
                except (TypeError, ValueError):
                    value = None
                if value is not None and value not in seen:
                    seen.add(value)
                    out.append(value)
            walk(entry.get("subCategories") or entry.get("sub_categories"))

    walk(capabilities.get("categories"))
    return out


def _declared_host(value):
    text = _text(value).lower()
    if not text:
        return ""
    if "://" not in text:
        text = "https://" + text
    try:
        from urllib.parse import urlparse

        host = urlparse(text).hostname or ""
    except ValueError:
        return ""
    return host.strip().strip("[]").lower()


def declared_hosts(indexer_row):
    """The hosts an indexer definition itself points at.

    A Prowlarr indexer row carries ``indexerUrls`` (the site URLs the
    definition currently uses) and ``legacyUrls`` (ones it used to). These are
    the operator's own configuration as Prowlarr reports it, so a pack-detail
    fetch that follows a result's ``infoUrl`` to one of them is going where the
    operator already sends every search -- not to an arbitrary host a release
    named. Order is kept, duplicates dropped.
    """
    row = indexer_row if isinstance(indexer_row, dict) else {}
    out = []
    seen = set()
    for key in ("indexerUrls", "indexer_urls", "legacyUrls", "legacy_urls"):
        values = row.get(key)
        if isinstance(values, str):
            values = [values]
        for value in values or []:
            host = _declared_host(value)
            if host and host not in seen:
                seen.add(host)
                out.append(host)
    return out


def hosts_by_indexer_id(indexer_list_payload):
    """Map indexer id -> declared hosts, from a /api/v1/indexer payload."""
    out = {}
    rows = indexer_list_payload if isinstance(indexer_list_payload, list) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        indexer_id = _text(row.get("id"))
        if not indexer_id:
            continue
        hosts = declared_hosts(row)
        if hosts:
            out[indexer_id] = hosts
    return out


def capabilities_by_indexer_id(indexer_list_payload):
    """Map indexer id -> declared category ids, from a /api/v1/indexer payload."""
    out = {}
    rows = indexer_list_payload if isinstance(indexer_list_payload, list) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        indexer_id = _text(row.get("id"))
        if not indexer_id:
            continue
        out[indexer_id] = declared_category_ids(row)
    return out


def _category_parent(category_id):
    """The Newznab parent of a subcategory, or None if it has no useful one.

    7030 -> 7000. Top-level ids (7000) and indexer-private custom categories
    (100000+, which carry no shared hierarchy) return None: substituting a
    made-up ancestor for those would widen the search into unrelated content.
    """
    if category_id >= 100000 or category_id < 1000:
        return None
    parent = (category_id // 1000) * 1000
    return None if parent == category_id else parent


def resolve_categories(requested, indexer_ids, capabilities):
    """Rewrite requested categories into ones the chosen indexers actually declare.

    The bug this closes: we ask Nyaa for 7030, Nyaa declares only 7000, Prowlarr
    matches nothing and answers 0 in ~25ms without ever querying the indexer.
    Measured live -- `Chainsaw Man` returns 7 results under the 7030 filter and
    361 without it.

    A requested category survives untouched if any selected indexer declares it,
    so indexers that do publish 7030 (DOGnzb 15, TorrentLeech 44/47, AltHub 48)
    are unaffected. Otherwise its nearest declared ancestor is substituted.

    Returns (resolved_ids, substitutions). An empty `resolved_ids` means the
    caller should send no category filter at all rather than send one that
    cannot match -- an unfiltered search is recoverable, a guaranteed-zero one
    is not. If capabilities are unknown for every selected indexer, the request
    is returned unchanged: guessing is worse than the status quo.
    """
    requested_ids = []
    for value in requested or []:
        try:
            requested_ids.append(int(value))
        except (TypeError, ValueError):
            continue
    if not requested_ids:
        return [], []

    capabilities = capabilities if isinstance(capabilities, dict) else {}
    if indexer_ids:
        known = [
            capabilities[_text(indexer_id)]
            for indexer_id in indexer_ids
            if _text(indexer_id) in capabilities
        ]
    else:
        known = list(capabilities.values())
    if not known:
        return [str(value) for value in requested_ids], []

    declared = set()
    for entry in known:
        declared.update(entry or [])
    if not declared:
        return [str(value) for value in requested_ids], []

    resolved = []
    substitutions = []
    seen = set()
    for category_id in requested_ids:
        if category_id in declared:
            target = category_id
        else:
            parent = _category_parent(category_id)
            if parent is not None and parent in declared:
                target = parent
                substitutions.append({"requested": str(category_id), "used": str(parent)})
            else:
                substitutions.append({"requested": str(category_id), "used": ""})
                continue
        if target not in seen:
            seen.add(target)
            resolved.append(target)
    return [str(value) for value in resolved], substitutions


def coverage_detail(coverage, *, names_by_id=None):
    """One human-readable line naming the lanes that were missing."""
    coverage = coverage if isinstance(coverage, dict) else {}
    missing = list(coverage.get("unavailable_indexer_ids") or [])
    if not missing:
        return ""
    names_by_id = names_by_id if isinstance(names_by_id, dict) else {}
    labels = []
    for indexer_id in missing:
        name = _text(names_by_id.get(indexer_id) or names_by_id.get(_id_key(indexer_id)))
        labels.append(f"{name} (#{indexer_id})" if name else f"#{indexer_id}")
    scope = "every indexer" if coverage.get("selected_all_indexers") else "this source's indexers"
    joined = ", ".join(labels)
    if len(labels) == 1:
        return f"Searched {scope}, but {joined} was unavailable and never answered."
    return f"Searched {scope}, but {joined} were unavailable and never answered."
