#!/usr/bin/env python3
"""Does this deployment agree with itself about which artifact it is running?

Five independent sources describe one deployed artifact, and on 2026-08-20 the
bytes were correct while four of the five descriptions were stale. The image
was the CI-validated `sha256:a2fdbee8...`; Compose still exported the previous
`sha256:878e55ae...`, the candidate manifest on the host was eight days and
three versions behind, and the product's own identity surface reported a digest
nobody was running. Docker health stayed green throughout, correctly, because
it answers a different question.

This module answers the identity question and nothing else. It does not decide
whether the container should be restarted, and it must never be wired into a
liveness probe -- see the note on scopes below.

THE SOURCES
    1. The container's own environment (what the image says it is).
    2. The CI candidate manifest on disk (what CI said it built).
    3. The worker's declared digest (whether both services are the same bytes).
    4. Docker's RepoDigests (what the registry actually vouches for).
    5. The channel-approved repository (whether that registry is an approved
       one for this release channel).

FAILS CLOSED ON UNSET, WHICH IS THE POINT
    The previous comparison in `inkdrop_version.build_metadata()` builds two of
    its eight comparisons conditionally:

        if image_repository:      comparisons["image_repository"] = ...
        if state_schema_version:  comparisons["state_schema_version"] = ...

    Neither variable is exported by the production Compose file, so both
    comparisons are skipped entirely and the surface reports "five mismatches"
    out of *six evaluated*, not out of eight. An unset variable buys a pass.
    Here, an undeclared value is a named failure. A description that is absent
    is not a description that agrees.

TWO SCOPES, AND WHY BLIND IS NOT A PASS
    The web process cannot see Docker's RepoDigests -- it has no socket, and it
    should not have one. So a check that needs them is *unverified* in-product
    rather than passed. `state` is only ever "matched" when every check passed
    AND nothing was left unverified; an in-product report with no RepoDigests
    supplied reports "incomplete", which is not a pass and must not be read as
    one. The deploy tool supplies them and requires "matched".
"""

from __future__ import annotations

import os
import re

from core import inkdrop_version


DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

IDENTITY_SCHEMA = "inkdrop.release_identity.v1"
IDENTITY_SCHEMA_VERSION = 1

# "Equal to Docker's own RepoDigest" is not well defined when an image carries
# more than one. The production image is tagged into both
# ghcr.io/jaredbahr/inkdrop-qa and ghcr.io/jaredbahr/inkdrop at the same digest,
# so "the RepoDigest" names two different strings. The gate resolves that by
# selecting the repository approved for the release channel and requiring the
# digest to be the one published under *that* name.
CHANNEL_REPOSITORIES = dict(inkdrop_version.UPDATE_IMAGE_REPOSITORIES)

# Channels whose bytes no registry vouches for. Identity is still reported, but
# a local build can never reach "matched" and says so rather than failing in a
# way that reads like a broken deploy.
LOCAL_BUILD_CHANNELS = frozenset(inkdrop_version.LOCAL_BUILD_CHANNELS)


def _text(value):
    return str(value if value is not None else "").strip()


def _parse_repo_digests(repo_digests):
    """`["repo@sha256:...", ...]` -> `{repo: digest}`. Unparseable entries drop."""
    parsed = {}
    for entry in repo_digests or []:
        raw = _text(entry)
        if "@" not in raw:
            continue
        repository, _, digest = raw.partition("@")
        repository = repository.strip().lower()
        digest = digest.strip().lower()
        if repository and DIGEST_RE.match(digest):
            parsed[repository] = digest
    return parsed


class _Report:
    """Accumulates named checks so every failure can be asserted on by name."""

    def __init__(self):
        self.checks = []

    def record(self, name, outcome, detail):
        self.checks.append({"check": name, "outcome": outcome, "detail": detail})

    def passed(self, name, detail=""):
        self.record(name, "pass", detail)

    def failed(self, name, detail):
        self.record(name, "fail", detail)

    def unverified(self, name, detail):
        self.record(name, "unverified", detail)

    def names(self, outcome):
        return sorted(item["check"] for item in self.checks if item["outcome"] == outcome)


def deployment_identity_report(environ=None, *, repo_digests=None, manifest=None, manifest_status=None):
    """Whether every identity source agrees on the running artifact.

    ``repo_digests`` is Docker's own ``RepoDigests`` list for the running image,
    e.g. ``["ghcr.io/jaredbahr/inkdrop-qa@sha256:a2fd..."]``. Pass ``None`` when
    it cannot be read -- the affected checks are reported ``unverified`` and the
    overall state can then never be ``matched``.

    ``manifest``/``manifest_status`` override the on-disk candidate manifest;
    they exist so tests can sabotage one source without writing files.
    """
    env = os.environ if environ is None else environ
    report = _Report()

    channel = _text(env.get("INKDROP_RELEASE_CHANNEL")).lower() or inkdrop_version.DEFAULT_RELEASE_CHANNEL
    local_build = channel in LOCAL_BUILD_CHANNELS

    declared_digest = _text(env.get("INKDROP_IMAGE_DIGEST")).lower()
    worker_digest = _text(env.get("INKDROP_WORKER_IMAGE_DIGEST")).lower()
    declared_repository = _text(env.get("INKDROP_IMAGE_REPOSITORY")).lower()
    declared_schema = _text(env.get("INKDROP_STATE_SCHEMA_VERSION"))
    commit_sha = _text(env.get("INKDROP_COMMIT_SHA")).lower()
    version = _text(env.get("INKDROP_VERSION"))

    # --- 1. the web image's own declared digest ------------------------------
    if not declared_digest:
        report.failed("image_digest_declared", "INKDROP_IMAGE_DIGEST is not set")
    elif not DIGEST_RE.match(declared_digest):
        report.failed("image_digest_declared", f"INKDROP_IMAGE_DIGEST is not a sha256 digest: {declared_digest!r}")
    else:
        report.passed("image_digest_declared", declared_digest)

    # --- 2. the worker is the same bytes as the web --------------------------
    if not worker_digest:
        report.failed("worker_digest_declared", "INKDROP_WORKER_IMAGE_DIGEST is not set")
    elif not DIGEST_RE.match(worker_digest):
        report.failed("worker_digest_declared", f"INKDROP_WORKER_IMAGE_DIGEST is not a sha256 digest: {worker_digest!r}")
    else:
        report.passed("worker_digest_declared", worker_digest)
        if declared_digest and DIGEST_RE.match(declared_digest):
            if worker_digest == declared_digest:
                report.passed("worker_matches_web", worker_digest)
            else:
                report.failed(
                    "worker_matches_web",
                    f"worker is {worker_digest} but web is {declared_digest}; the two services are different bytes",
                )

    # --- 3. the repository is declared, and approved for this channel --------
    expected_repository = _text(CHANNEL_REPOSITORIES.get(channel)).lower()
    if local_build:
        report.unverified("image_repository_approved", f"channel {channel!r} is a local build; no registry vouches for it")
    elif not declared_repository:
        # The hole this module exists to close: build_metadata() skips its
        # image_repository comparison entirely when this is unset, so an
        # undeclared repository currently reads as agreement.
        report.failed(
            "image_repository_approved",
            "INKDROP_IMAGE_REPOSITORY is not set, so no repository can be checked against the channel",
        )
    elif not expected_repository:
        report.failed("image_repository_approved", f"no approved repository is defined for channel {channel!r}")
    elif declared_repository != expected_repository:
        report.failed(
            "image_repository_approved",
            f"declared repository {declared_repository!r} is not the approved repository {expected_repository!r} for channel {channel!r}",
        )
    else:
        report.passed("image_repository_approved", declared_repository)

    # --- 4. the state schema version is declared -----------------------------
    # Same conditional-skip hole as the repository above.
    if not declared_schema:
        report.failed("state_schema_version_declared", "INKDROP_STATE_SCHEMA_VERSION is not set")
    elif not declared_schema.isdigit() or int(declared_schema) <= 0:
        report.failed("state_schema_version_declared", f"INKDROP_STATE_SCHEMA_VERSION is not a positive integer: {declared_schema!r}")
    else:
        report.passed("state_schema_version_declared", declared_schema)

    # --- 5. the CI candidate manifest describes these same bytes -------------
    if manifest is None or manifest_status is None:
        manifest, manifest_status = inkdrop_version.load_candidate_manifest(env)
    if local_build:
        report.unverified("candidate_manifest_agrees", f"channel {channel!r} is a local build; CI produced no candidate manifest")
    elif manifest_status != "loaded":
        report.failed("candidate_manifest_agrees", f"candidate manifest is {manifest_status!r}, not loaded")
    else:
        disagreements = _manifest_disagreements(manifest, env)
        if disagreements:
            report.failed(
                "candidate_manifest_agrees",
                "candidate manifest disagrees on " + ", ".join(f"{k} (manifest {m!r} vs deployment {d!r})" for k, m, d in disagreements),
            )
        else:
            report.passed("candidate_manifest_agrees", f"commit {commit_sha[:12]} version {version}")

    # --- 6. the registry vouches for these bytes under the approved name -----
    if local_build:
        report.unverified("repo_digest_matches", f"channel {channel!r} is a local build; there is no RepoDigest to check")
    elif repo_digests is None:
        # Not a pass. The web process has no Docker socket, so in-product this
        # is genuinely unknown, and unknown keeps the overall state out of
        # "matched" rather than quietly counting as agreement.
        report.unverified("repo_digest_matches", "Docker RepoDigests were not supplied; this check needs deploy-time access to the daemon")
    else:
        published = _parse_repo_digests(repo_digests)
        if not expected_repository:
            report.failed("repo_digest_matches", f"no approved repository is defined for channel {channel!r}")
        elif expected_repository not in published:
            report.failed(
                "repo_digest_matches",
                f"the running image carries no RepoDigest for the approved repository {expected_repository!r} "
                f"(it has: {sorted(published) or 'none'})",
            )
        elif not declared_digest:
            report.failed("repo_digest_matches", "INKDROP_IMAGE_DIGEST is not set, so it cannot be compared to the RepoDigest")
        elif published[expected_repository] != declared_digest:
            report.failed(
                "repo_digest_matches",
                f"{expected_repository} publishes {published[expected_repository]} but the deployment declares {declared_digest}",
            )
        else:
            report.passed("repo_digest_matches", f"{expected_repository}@{published[expected_repository]}")

    failures = report.names("fail")
    unverified = report.names("unverified")
    if failures:
        state = "mismatched"
    elif unverified:
        state = "incomplete"
    else:
        state = "matched"

    return {
        "ok": not failures,
        "schema": IDENTITY_SCHEMA,
        "identity_schema_version": IDENTITY_SCHEMA_VERSION,
        # "matched" is the only value that means every source agreed and every
        # source was actually consulted. "incomplete" is not a pass.
        "state": state,
        "label": _label(state),
        "detail": _detail(state, failures, unverified),
        "release_channel": channel,
        "local_build": local_build,
        "failures": failures,
        "unverified": unverified,
        "checks": report.checks,
        "declared": {
            "image_digest": declared_digest,
            "worker_image_digest": worker_digest,
            "image_repository": declared_repository,
            "state_schema_version": declared_schema,
            "commit_sha": commit_sha,
            "version": version,
        },
        "expected_repository": expected_repository,
    }


def _manifest_disagreements(manifest, env):
    """Every candidate-manifest field that disagrees, as (key, manifest, deployment).

    Unconditional. `build_metadata()` compares image_repository and
    state_schema_version only when the deployment declares them, which turns a
    missing declaration into a silent pass; here a missing declaration has
    already been failed above and is still compared, so the manifest's own
    value cannot slip through unexamined.
    """
    def manifest_text(key):
        return _text(manifest.get(key))

    comparisons = [
        ("commit_sha", manifest_text("full_commit_sha").lower() or manifest_text("commit_sha").lower(), _text(env.get("INKDROP_COMMIT_SHA")).lower()),
        ("version", manifest_text("version"), _text(env.get("INKDROP_VERSION"))),
        ("build_date", manifest_text("build_date"), _text(env.get("INKDROP_BUILD_DATE"))),
        ("release_channel", manifest_text("release_channel").lower(), _text(env.get("INKDROP_RELEASE_CHANNEL")).lower()),
        ("image_digest", manifest_text("image_digest").lower(), _text(env.get("INKDROP_IMAGE_DIGEST")).lower()),
        ("qa_build_number", manifest_text("qa_build_number"), _text(env.get("INKDROP_QA_BUILD_NUMBER"))),
        ("image_repository", manifest_text("image_repository").lower(), _text(env.get("INKDROP_IMAGE_REPOSITORY")).lower()),
        ("state_schema_version", manifest_text("state_schema_version"), _text(env.get("INKDROP_STATE_SCHEMA_VERSION"))),
    ]
    return [(key, expected, actual) for key, expected, actual in comparisons if expected != actual]


def _label(state):
    return {
        "matched": "Identity confirmed",
        "incomplete": "Identity not fully checked",
        "mismatched": "Identity does not agree",
    }.get(state, "Unknown")


def _detail(state, failures, unverified):
    if state == "matched":
        return "Every identity source agrees on the running artifact."
    if state == "incomplete":
        return (
            "Every source that could be checked agrees, but "
            + ", ".join(unverified)
            + " could not be checked from inside the container. This is not a pass."
        )
    parts = [f"{len(failures)} identity source{'s' if len(failures) != 1 else ''} disagree: " + ", ".join(failures) + "."]
    if unverified:
        parts.append("Also unchecked: " + ", ".join(unverified) + ".")
    return " ".join(parts)


def readiness_payload(environ=None, *, repo_digests=None):
    """The `/api/system/release-readiness` body.

    Deliberately a separate surface from `/api/system/version`. That endpoint is
    descriptive -- About renders it and the update-awareness path consumes it --
    and putting a pass/fail verdict inside a descriptive payload invites someone
    to wire it into a probe. Keeping the verdict on its own surface means that
    mistake has to be a deliberate act rather than a plausible one.

    NOT A LIVENESS PROBE. A container that fails this check is serving fine and
    must keep serving; the operator needs it up in order to read why it failed.
    Liveness stays with core/inkdrop_container_healthcheck.py.
    """
    report = deployment_identity_report(environ, repo_digests=repo_digests)
    report["probe"] = {
        "kind": "readiness",
        "answers": "is this the artifact we think it is",
        "does_not_answer": "can this container serve requests",
        "liveness_owner": "core/inkdrop_container_healthcheck.py (Docker HEALTHCHECK)",
        "safe_for_restart_policy": False,
    }
    return report
