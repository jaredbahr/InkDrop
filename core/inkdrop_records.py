#!/usr/bin/env python3
"""Versioned, typed records for the shapes InkDrop hands across boundaries.

Most InkDrop data crosses module boundaries as a bare dict. That is cheap to
write and it is where a specific, recurring class of production bug lives: the
same concept gets spelled two ways on either side of a boundary, nothing
complains, and the reader silently sees "absent" or "unknown" instead of the
value that was actually there. Real examples this module exists to end:

* NZBGet reports ``ParStatus`` but InkDrop's own snapshots lowercase it to
  ``par_status``; a health check that read only one spelling treated an
  unverified download as verified.
* Every torrent client publishes its item id as ``torrent_hash`` while the
  telemetry normalizer read only ``hash``/``nzo_id``, so transfer identity
  came back ``None`` for Deluge, Transmission, uTorrent and rTorrent.
* ``deluge_status`` and friends emitted the state token ``active`` while the
  normalizer's state machine only understood ``downloading``, so a transfer
  at 42% reported ``unknown`` and counted as zero active transfers.
* SABnzbd's ``percentage`` is 0-100 and qBittorrent's ``progress`` is 0-1;
  both were assigned to a field named ``progress`` and told apart by
  magnitude, so a SABnzbd download at 1% read as 100% complete.

The fix is not another alias fallback. It is a record with one spelling per
concept, one declared unit per number, and one place -- ``from_*`` -- where
provider-shaped input is converted. Downstream code consumes only the record,
so a producer that invents a new spelling fails at construction instead of
going quiet three modules away.

Records carry an explicit ``schema`` string. Changing a field's meaning means
minting a new version rather than mutating v1 under readers that still expect
it.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field


RECORD_NAMESPACE = "inkdrop"


class RecordShapeError(ValueError):
    """A record was constructed with a value its contract does not allow.

    Raised rather than coerced on purpose: a producer handing over an
    out-of-vocabulary state or an out-of-range percent is a bug in the
    producer, and the whole point of these records is that such a bug stops
    at the boundary instead of turning into a silent "unknown" downstream.
    """


def schema_id(concept, version):
    return f"{RECORD_NAMESPACE}.{concept}.v{int(version)}"


# --------------------------------------------------------------------------
# Scalars
# --------------------------------------------------------------------------

def _text(value, default=""):
    text = str(value).strip() if value is not None else ""
    return text or default


def _number(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _non_negative_int(value):
    number = _number(value)
    if number is None or number < 0:
        return None
    return int(number)


# --------------------------------------------------------------------------
# Client identity
# --------------------------------------------------------------------------

# The union of every client spelling InkDrop has had to recognize. This is the
# only copy; `inkdrop_client_status` and `inkdrop_transfer` both defer here so
# a newly supported client is taught to the codebase once.
_CLIENT_ALIASES = {
    "qbit": "qbittorrent",
    "qb": "qbittorrent",
    "qbittorrent": "qbittorrent",
    "sab": "sabnzbd",
    "sabnzbd": "sabnzbd",
    "soulseek": "slskd",
    "slskd": "slskd",
    "transmission": "transmission",
    "transmissionbt": "transmission",
    "deluge": "deluge",
    "delugeweb": "deluge",
    "nzb": "nzbget",
    "nzbget": "nzbget",
    "utorrent": "utorrent",
    "utorrentweb": "utorrent",
    "utorrentwebui": "utorrent",
    "rtorrent": "rtorrent",
    "rtorrentxmlrpc": "rtorrent",
}

CLIENT_DISPLAY_NAMES = {
    "qbittorrent": "qBittorrent",
    "sabnzbd": "SABnzbd",
    "slskd": "SLSKD",
    "transmission": "Transmission",
    "deluge": "Deluge",
    "nzbget": "NZBGet",
    "utorrent": "uTorrent",
    "rtorrent": "rTorrent",
}


def coerce_client_id(value):
    """Canonical client id for any spelling a client has ever arrived under.

    Unrecognized values pass through lowercased rather than becoming
    ``unknown``: a client InkDrop does not model yet is still a real
    attribution, and erasing it would lose more than it protects.
    """
    raw = _text(value).lower()
    key = raw.replace("-", "").replace("_", "").replace(" ", "")
    return _CLIENT_ALIASES.get(key, raw)


def client_display_name(value):
    client = coerce_client_id(value)
    return CLIENT_DISPLAY_NAMES.get(client, client or "Download client")


# --------------------------------------------------------------------------
# Transfer state vocabulary
# --------------------------------------------------------------------------

# The canonical vocabulary. Every producer maps into exactly these tokens and
# every consumer branches on exactly these tokens. Adding a token here is a
# deliberate, reviewable act; inventing one at a call site is an error.
TRANSFER_STATES = (
    "queued",
    "downloading",
    "stalled",
    "paused",
    "completed",
    "seeding",
    "failed",
    "removed",
    "unknown",
)

TRANSFER_STATES_ACTIVE = frozenset({"downloading", "stalled"})
TRANSFER_STATES_QUEUED = frozenset({"queued", "paused"})
TRANSFER_STATES_COMPLETE = frozenset({"completed", "seeding"})
TRANSFER_STATES_TERMINAL = frozenset({"completed", "seeding", "failed", "removed"})

# Legacy and provider-side spellings for tokens already in the vocabulary.
# `active` is the one that mattered: the per-client status builders emitted it
# for years while the normalizer only understood `downloading`.
_TRANSFER_STATE_ALIASES = {
    "active": "downloading",
    "downloading": "downloading",
    "download": "downloading",
    "importing": "downloading",
    "queued": "queued",
    "queueing": "queued",
    "waiting": "queued",
    "pending": "queued",
    "paused": "paused",
    "stopped": "paused",
    "stalled": "stalled",
    "stale": "stalled",
    "completed": "completed",
    "complete": "completed",
    "completed_in_client": "completed",
    "finished": "completed",
    "seeding": "seeding",
    "uploading": "seeding",
    "failed": "failed",
    "error": "failed",
    "failed_download": "failed",
    "removed": "removed",
    "deleted": "removed",
    "unknown": "unknown",
    "": "unknown",
}


def coerce_transfer_state(value, *, default="unknown"):
    """Map a provider- or legacy-spelled state into the vocabulary.

    Forgiving by design: this is the ingestion side, where junk from a remote
    client is expected. Producers inside InkDrop should use
    `require_transfer_state` so a typo cannot quietly become ``unknown``.
    """
    token = _text(value).lower().replace("-", "_").replace(" ", "_")
    if token in TRANSFER_STATES:
        return token
    return _TRANSFER_STATE_ALIASES.get(token, default)


def require_transfer_state(value):
    """Return `value` if it is already a canonical state, else raise.

    Used by InkDrop's own status builders. It is what makes the `active`
    class of bug structurally impossible: a builder that emits a token the
    consumers do not understand cannot construct a record at all.
    """
    token = _text(value).lower()
    if token not in TRANSFER_STATES:
        raise RecordShapeError(
            f"{token or '(empty)'!r} is not an InkDrop transfer state; "
            f"expected one of {', '.join(TRANSFER_STATES)}"
        )
    return token


# --------------------------------------------------------------------------
# Download identity
# --------------------------------------------------------------------------

# Every key an item id has ever arrived under, newest-canonical first. One
# table, so a client that spells it a ninth way is taught here and nowhere
# else. Order matters only for which alias wins when a payload carries more
# than one.
IDENTITY_KEYS = (
    "client_item_id",
    "external_id",
    "client_external_id",
    "client_id",
    "torrent_hash",
    "info_hash",
    "hash",
    "hashString",
    "nzo_id",
    "nzoId",
    "nzb_id",
    "NZBID",
    "transfer_id",
)

_PROTOCOL_BY_CLIENT = {
    "qbittorrent": "torrent",
    "transmission": "torrent",
    "deluge": "torrent",
    "utorrent": "torrent",
    "rtorrent": "torrent",
    "sabnzbd": "usenet",
    "nzbget": "usenet",
    "slskd": "soulseek",
}


@dataclass(frozen=True)
class DownloadIdentity:
    """Who is holding a download, and what that holder calls it.

    ``item_id`` is the download client's own identifier -- an info hash, an
    NZB id, a SABnzbd nzo id. ``handoff_key`` is InkDrop's identifier, which
    InkDrop stamps on the item when it hands it over and which survives a
    client that renumbers or renames.
    """

    client: str = ""
    item_id: str = ""
    protocol: str = ""
    handoff_key: str = ""

    def __post_init__(self):
        object.__setattr__(self, "client", coerce_client_id(self.client))
        object.__setattr__(self, "item_id", _text(self.item_id))
        object.__setattr__(self, "handoff_key", _text(self.handoff_key))
        protocol = _text(self.protocol).lower()
        object.__setattr__(self, "protocol", protocol or _PROTOCOL_BY_CLIENT.get(self.client, ""))

    @property
    def resolved(self):
        """True when the client actually told us which item this is."""
        return bool(self.item_id)

    @classmethod
    def from_mapping(cls, payload, *, client=None, protocol=None, handoff_key=None):
        """Build an identity from any provider or InkDrop payload.

        This is the normalization boundary for download identity. Nothing
        downstream should read the alias keys itself.
        """
        payload = payload if isinstance(payload, dict) else {}
        item_id = ""
        for key in IDENTITY_KEYS:
            value = payload.get(key)
            # 0 is a real NZBGet id only in theory, but an empty string and
            # None are always absence; keep the distinction explicit rather
            # than leaning on falsiness.
            if value is None or value == "":
                continue
            item_id = _text(value)
            if item_id:
                break
        resolved_client = client if client is not None else (
            payload.get("client")
            or payload.get("download_client")
            or payload.get("source")
        )
        resolved_handoff = handoff_key if handoff_key is not None else (
            payload.get("handoff_key")
            or payload.get("handoff_tag")
            or payload.get("unique_tag")
        )
        return cls(
            client=resolved_client,
            item_id=item_id,
            protocol=protocol if protocol is not None else payload.get("protocol"),
            handoff_key=resolved_handoff,
        )

    def aliases(self):
        """The legacy alias keys, for payloads still read by older code.

        Emitting these from one place is what keeps them consistent; they are
        output only, and no InkDrop reader should treat them as input.
        """
        if not self.item_id:
            return {}
        out = {
            "client_item_id": self.item_id,
            "external_id": self.item_id,
            "client_external_id": self.item_id,
            "client_id": self.item_id,
        }
        if self.protocol == "torrent":
            out["torrent_hash"] = self.item_id
        elif self.client == "nzbget":
            out["nzb_id"] = self.item_id
            out["nzo_id"] = self.item_id
        elif self.client == "sabnzbd":
            out["nzo_id"] = self.item_id
        return out

    def to_dict(self):
        return {
            "schema": schema_id("download_identity", 1),
            "client": self.client,
            "item_id": self.item_id or None,
            "protocol": self.protocol or None,
            "handoff_key": self.handoff_key or None,
        }


# --------------------------------------------------------------------------
# Candidate identity
# --------------------------------------------------------------------------

# A candidate identity is a 24-character lowercase hex digest -- the first 24
# characters of a SHA-256 over a labelled, pipe-joined tuple. That shape was
# never written down anywhere, but three safety gates already depend on it:
# `_owned_task_identity_matches` and `_safe_alternate_candidate_matches_task`
# in inkdrop_source_suppression, and `owned_page_pack_rebase_target` in
# inkdrop_page_pack_downloader each carried their own copy of
# `re.fullmatch(r"[0-9a-f]{24}", identity)` and failed closed on anything else.
#
# Failing closed is the right instinct and is kept. What was wrong is that the
# rule lived only in the readers, so producers were free to violate it:
# `direct_file_probe_candidates_from_payload` assigned a raw 64-character
# `download_url_hash`, and the Suwayomi managed-folder record built a
# colon-joined f-string. Neither could satisfy a gate. For the probe path that
# was a real cost -- a transient infrastructure failure was held for the full
# source-memory cooldown instead of being retried. The Suwayomi record never
# reaches those gates at all (`_owned_staging_task` dispatches on
# `download_client` and handles only `inkdrop_direct`/`inkdrop_page_pack`), so
# fixing it is conformance rather than a behaviour change.
#
# Declaring the shape here, once, is what turns "the reader rejects it" into
# "the producer cannot build it".
CANDIDATE_IDENTITY_DIGEST_LENGTH = 24

_CANDIDATE_IDENTITY_RE = re.compile(r"[0-9a-f]{%d}" % CANDIDATE_IDENTITY_DIGEST_LENGTH)


def candidate_identity_digest(label, *parts):
    """Mint a conforming candidate identity from a label and its parts.

    Deliberately byte-for-byte identical to `inkdrop_sources.stable_id`, which
    minted every identity already persisted. This is a second implementation
    only so that `inkdrop_records` does not depend on the provider layer.
    `tests/inkdrop-typed-candidate-identity-smoke.py` asserts the two agree,
    which is what protects the ~17k identities already in `download_tasks`.
    """
    raw = "|".join(str(part or "") for part in (label, *parts))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:CANDIDATE_IDENTITY_DIGEST_LENGTH]


def is_candidate_identity(value):
    """True when `value` is a well-formed candidate identity.

    The single copy of the shape test the three ownership gates used to keep
    privately.
    """
    return bool(_CANDIDATE_IDENTITY_RE.fullmatch(_text(value).lower()))


def require_candidate_identity(value, *, field_name="candidate_identity"):
    """Return `value` normalized, or raise if it is not a candidate identity.

    Used by InkDrop's own candidate builders. A producer that hands over a
    download URL hash, a torrent info hash, a SABnzbd nzo id or a
    colon-joined string fails here rather than three modules away, where the
    only symptom is a safety gate quietly declining to act.
    """
    text = _text(value).lower()
    if not is_candidate_identity(text):
        raise RecordShapeError(
            f"{field_name} must be {CANDIDATE_IDENTITY_DIGEST_LENGTH} lowercase hex "
            f"characters, got {value!r} (length {len(_text(value))}); "
            "build it with candidate_identity_digest() rather than reusing a "
            "download URL hash, a client item id or a joined string"
        )
    return text


# Where an artifact-level candidate identity has been found under a provider's
# own spelling. Read in order; the first conforming value wins. Keys that hold
# a *different* concept are deliberately absent -- `download_url_hash` is a
# locator, and `external_id`/`info_hash`/`nzo_id` are download-client handles
# that belong to `DownloadIdentity`. Three production `download_tasks` rows
# from 2026-07-08 carry a 40-character info hash and a 36-character nzo id in
# `candidate_identity`; treating those keys as identity aliases is what would
# make that mistake look correct.
CANDIDATE_IDENTITY_KEYS = (
    "candidate_identity",
    "provider_candidate_identity",
    "indexer_candidate_key",
    "mangadex_candidate_key",
    "suwayomi_candidate_key",
    "direct_artifact_key",
)

CANDIDATE_FAMILY_IDENTITY_KEYS = (
    "candidate_family_identity",
    "indexer_suppression_key",
    "mangadex_suppression_key",
    "suwayomi_suppression_key",
    "direct_suppression_key",
)


def _first_conforming(payload, keys):
    for key in keys:
        value = payload.get(key)
        if value in (None, ""):
            continue
        text = _text(value).lower()
        if is_candidate_identity(text):
            return text
    return ""


@dataclass(frozen=True)
class CandidateIdentity:
    """Which candidate this is, at both levels InkDrop actually reasons about.

    ``artifact`` names *this* artifact -- this torrent, this file, this
    chapter render. Two different releases of the same issue have two
    different artifact identities, which is what makes "this exact download
    failed" recordable.

    ``family`` names the logical release, collapsing mirrors and peers of the
    same thing. It is what makes "stop offering me this release" possible.

    Both are carried explicitly, and neither is reachable through a generic
    ``.identity``. That is the point. Before this record, one field named
    ``candidate_identity`` held the artifact identity on every provider path
    and the *family* identity on anything that went through
    `inkdrop_source_worker_runtime.parse_candidates`, which overwrites it at
    line 189. Both are 24-hex, so the shape gates could not tell them apart --
    a consumer comparing the two was comparing different questions and simply
    got no match. Making the caller name the level is the same reasoning that
    makes `TransferProgress` refuse to guess its own unit.
    """

    artifact: str = ""
    family: str = ""
    provider: str = ""

    def __post_init__(self):
        for name in ("artifact", "family"):
            value = _text(getattr(self, name)).lower()
            if value:
                require_candidate_identity(value, field_name=f"candidate identity ({name})")
            object.__setattr__(self, name, value)
        object.__setattr__(self, "provider", _text(self.provider).lower())

    @property
    def resolved(self):
        """True when this candidate can be spoken about at all."""
        return bool(self.artifact or self.family)

    @classmethod
    def from_mapping(cls, payload, *, provider=None):
        """Read a candidate identity off any provider or InkDrop payload.

        This is the normalization boundary for candidate identity. Nothing
        downstream should walk the alias keys itself. Non-conforming values
        are skipped rather than raising: on the *reading* side a legacy row
        carrying a client handle is data to be tolerated, and the record
        simply reports it as unresolved -- exactly what the gates already did.
        """
        payload = payload if isinstance(payload, dict) else {}
        return cls(
            artifact=_first_conforming(payload, CANDIDATE_IDENTITY_KEYS),
            family=_first_conforming(payload, CANDIDATE_FAMILY_IDENTITY_KEYS),
            provider=provider if provider is not None else (
                payload.get("provider_id") or payload.get("provider") or payload.get("source")
            ),
        )

    def matches(self, other):
        """True when both name the same candidate at the same level.

        Compared level by level. An artifact identity is never allowed to
        satisfy a family comparison or the reverse, which is the mismatch the
        single ``candidate_identity`` field used to hide.
        """
        if not isinstance(other, CandidateIdentity):
            other = CandidateIdentity.from_mapping(other)
        if self.artifact and other.artifact:
            return self.artifact == other.artifact
        if self.family and other.family:
            return self.family == other.family
        return False

    def aliases(self):
        """The legacy alias keys, for payloads still read by older code.

        Output only; no InkDrop reader should treat them as input.
        """
        out = {}
        if self.artifact:
            out["candidate_identity"] = self.artifact
        if self.family:
            out["candidate_family_identity"] = self.family
        return out

    def to_dict(self):
        return {
            "schema": schema_id("candidate_identity", 1),
            "artifact_identity": self.artifact or None,
            "family_identity": self.family or None,
            "provider": self.provider or None,
        }


# --------------------------------------------------------------------------
# Progress
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class TransferProgress:
    """How far along a transfer is, with the unit fixed by the constructor.

    ``percent_complete`` is always 0-100. It is never inferred from a bare
    number's magnitude -- callers say which unit they hold by choosing
    `from_percent`, `from_fraction` or `from_bytes`. Guessing is what turned a
    SABnzbd download at 1% into a completed one.
    """

    percent_complete: float | None = None
    bytes_completed: int | None = None
    bytes_total: int | None = None

    def __post_init__(self):
        percent = _number(self.percent_complete)
        if percent is not None and not (0.0 <= percent <= 100.0):
            raise RecordShapeError(
                f"percent_complete must be 0-100, got {self.percent_complete!r}; "
                "use TransferProgress.from_fraction for a 0-1 value"
            )
        object.__setattr__(self, "percent_complete", round(percent, 2) if percent is not None else None)
        total = _non_negative_int(self.bytes_total)
        completed = _non_negative_int(self.bytes_completed)
        if completed is not None and total is not None:
            completed = min(completed, total)
        object.__setattr__(self, "bytes_total", total)
        object.__setattr__(self, "bytes_completed", completed)

    @property
    def determinate(self):
        return self.percent_complete is not None

    @property
    def progress_kind(self):
        return "determinate" if self.determinate else "indeterminate"

    @property
    def complete(self):
        return self.percent_complete is not None and self.percent_complete >= 100.0

    @classmethod
    def from_percent(cls, value, *, bytes_completed=None, bytes_total=None):
        """Build from a 0-100 value (NZBGet, SABnzbd `percentage`, InkDrop)."""
        percent = _number(value)
        if percent is not None:
            percent = max(0.0, min(100.0, percent))
        return cls(percent_complete=percent, bytes_completed=bytes_completed, bytes_total=bytes_total)

    @classmethod
    def from_fraction(cls, value, *, bytes_completed=None, bytes_total=None):
        """Build from a 0-1 value (qBittorrent `progress`, Transmission).

        A caller that passes 42 here has a unit bug, and gets an error rather
        than a transfer that reads as finished.
        """
        fraction = _number(value)
        if fraction is None:
            return cls(bytes_completed=bytes_completed, bytes_total=bytes_total)
        if not (0.0 <= fraction <= 1.0):
            raise RecordShapeError(
                f"fraction must be 0-1, got {value!r}; "
                "use TransferProgress.from_percent for a 0-100 value"
            )
        return cls(
            percent_complete=fraction * 100.0,
            bytes_completed=bytes_completed,
            bytes_total=bytes_total,
        )

    @classmethod
    def from_bytes(cls, bytes_completed, bytes_total):
        """Derive percent from byte counts, the only unambiguous source."""
        total = _non_negative_int(bytes_total)
        completed = _non_negative_int(bytes_completed)
        if not total or completed is None:
            return cls(bytes_completed=completed, bytes_total=total)
        return cls(
            percent_complete=max(0.0, min(100.0, completed * 100.0 / total)),
            bytes_completed=completed,
            bytes_total=total,
        )

    @classmethod
    def unknown(cls):
        return cls()


# --------------------------------------------------------------------------
# Transfer status
# --------------------------------------------------------------------------

# NZBGet post-processing verdicts. `unknown` is not a synonym for `clean`: an
# outcome InkDrop cannot read is a download it cannot vouch for.
HEALTH_VERDICTS = ("clean", "damaged", "unknown")

_NEXT_STEP_BY_STATE = {
    "queued": "Waiting for the download client to start",
    "downloading": "Downloader transfer is active",
    "stalled": "Waiting for downloader progress or automatic retry",
    "completed": "Transfer complete; import or verification is next",
    "paused": "Downloader transfer is paused",
    "removed": "Downloader item was removed",
    "seeding": "Torrent transfer is complete and seeding",
    "failed": "Automatic source retry is next when eligible",
    "unknown": "Waiting for the next downloader status refresh",
}


@dataclass(frozen=True)
class TransferStatus:
    """One download client's account of one transfer, at one moment.

    Construct it directly from InkDrop code (strict: an unknown state raises)
    or via `normalize_transfer_status` in `inkdrop_transfer`, which is the
    forgiving boundary for provider-shaped input.
    """

    identity: DownloadIdentity = field(default_factory=DownloadIdentity)
    state: str = "unknown"
    native_state: str = ""
    progress: TransferProgress = field(default_factory=TransferProgress)
    download_rate_bytes_per_second: int | None = None
    upload_rate_bytes_per_second: int | None = None
    eta_seconds: int | None = None
    elapsed_seconds: int | None = None
    started_at: float | None = None
    last_updated_at: float | None = None
    completed_at: float | None = None
    stalled_reason: str = ""
    client_error: str = ""
    import_stage: str = ""
    health_verdict: str = ""
    health_reason: str = ""
    completed_output_path: str = ""
    display_title: str = ""
    source: str = ""
    provider: str = ""

    def __post_init__(self):
        object.__setattr__(self, "state", require_transfer_state(self.state))
        if not isinstance(self.identity, DownloadIdentity):
            raise RecordShapeError("identity must be a DownloadIdentity")
        if not isinstance(self.progress, TransferProgress):
            raise RecordShapeError("progress must be a TransferProgress")
        verdict = _text(self.health_verdict).lower()
        if verdict and verdict not in HEALTH_VERDICTS:
            raise RecordShapeError(
                f"{verdict!r} is not a health verdict; expected one of {', '.join(HEALTH_VERDICTS)}"
            )
        object.__setattr__(self, "health_verdict", verdict)
        for attr in ("native_state", "stalled_reason", "client_error", "import_stage",
                     "health_reason", "completed_output_path", "display_title", "source", "provider"):
            object.__setattr__(self, attr, _text(getattr(self, attr)))
        object.__setattr__(self, "client_error", self.client_error[:500])

    @property
    def stalled(self):
        return self.state == "stalled"

    @property
    def seeding(self):
        return self.state == "seeding"

    @property
    def active(self):
        return self.state in TRANSFER_STATES_ACTIVE

    @property
    def terminal(self):
        return self.state in TRANSFER_STATES_TERMINAL

    @property
    def needs_review(self):
        """Terminal, but InkDrop cannot vouch for what it got.

        A `damaged` or unreadable post-processing verdict on a finished item
        is an operator decision, not something a later poll will resolve.
        """
        return bool(self.health_verdict) and self.health_verdict != "clean"

    @property
    def next_step(self):
        if self.import_stage:
            return "Import and verification are in progress"
        return _NEXT_STEP_BY_STATE[self.state]

    def to_dict(self):
        """The wire/telemetry shape.

        Keys match the contract `normalize_transfer_status` has always
        returned, so existing readers keep working; `schema` and the health
        fields are additions. Identity aliases are emitted from
        `DownloadIdentity.aliases` rather than assembled here.
        """
        return {
            "schema": schema_id("transfer_status", 1),
            "client": self.identity.client,
            "client_item_id": self.identity.item_id or None,
            "handoff_key": self.identity.handoff_key or None,
            "protocol": self.identity.protocol or None,
            "transfer_state": self.state,
            "native_state": self.native_state or None,
            "progress_kind": self.progress.progress_kind,
            "percent_complete": self.progress.percent_complete,
            "bytes_completed": self.progress.bytes_completed,
            "bytes_total": self.progress.bytes_total,
            "download_rate_bytes_per_second": self.download_rate_bytes_per_second,
            "upload_rate_bytes_per_second": self.upload_rate_bytes_per_second,
            "eta_seconds": self.eta_seconds,
            "elapsed_seconds": self.elapsed_seconds,
            "started_at": self.started_at,
            "last_updated_at": self.last_updated_at,
            "completed_at": self.completed_at,
            "stalled": self.stalled,
            "seeding": self.seeding,
            "stalled_reason": self.stalled_reason or None,
            "client_error": self.client_error or None,
            "import_stage": self.import_stage or None,
            "health_verdict": self.health_verdict or None,
            "health_reason": self.health_reason or None,
            "needs_review": self.needs_review,
            "completed_output_path": self.completed_output_path or None,
            "next_step": self.next_step,
            "source": self.source or None,
            "provider": self.provider or None,
            "display_title": self.display_title or None,
        }
