#!/usr/bin/env python3
"""Preconditions the unattended deploy must satisfy before it acts.

Both guards here answer the same question in two places: CAN THIS STEP
ESTABLISH ITS OWN PRECONDITION? When it cannot, it must refuse rather than
proceed, because both failures are silent and both destroy something.

Kept as plain functions taking injected readers so they can be exercised
without ssh, a container, or a database. The deploy tool wires the real
readers in.
"""

from __future__ import annotations


def deploy_would_discard(
    *,
    target: str,
    running_sha: str,
    changed_paths,
    read_container,
    read_blob,
):
    """Shipped files whose running content the target does not account for.

    Returns the list of paths that would be silently discarded by swapping to
    `target`. Empty means the swap is safe.

    WHY THIS EXISTS. On 2026-08-31 a hand deploy found the instance running
    `b4f7dd61ec0d` because a peer had deployed after the previous swap. That
    target happened to contain it, so nothing was lost -- but nothing in the
    tooling checked, and nothing would have reported it. `qa` merges
    auto-deploy to the production instance, so this pipeline races that
    same condition unattended, with no human doing the containment check.

    WHY CONTENT AND NOT ANCESTRY. This repository squash-merges, so
    `git merge-base --is-ancestor` answers "no" for work that HAS landed: the
    pre-squash commit is genuinely not an ancestor of the squash that contains
    it. An ancestry gate would refuse healthy deploys, and a gate that cries
    wolf gets weakened until it stops protecting anything. What actually
    matters is whether the bytes now running are accounted for by the bytes
    about to be deployed, and that is a content question.

    THE RULE. For each shipped file the deploy would change, the content in the
    container must equal either the target's version (already deployed) or the
    claimed running commit's version (an ordinary forward deploy). Content
    matching neither means the container holds work the target does not
    contain.

    An unreadable file is REFUSED, not skipped. Skipping would make the gate
    weakest exactly when the container is least healthy, which is the
    fail-open shape this module exists to avoid.
    """
    discarded = []
    for path in changed_paths or []:
        try:
            actual = read_container(path)
        except Exception:
            actual = None
        running_blob = read_blob(running_sha, path) if running_sha else None
        if actual is None:
            # A FILE THE TARGET ADDS IS ABSENT FROM THE RUNNING CONTAINER BY
            # DEFINITION, and refusing on that would block every deploy that
            # adds a file -- a guard that over-refuses legitimate input, which
            # is its own defect class and one this project found four of in a
            # single day. Absent is only suspicious when the running commit
            # says the file should be there.
            if running_sha and running_blob is None:
                continue
            discarded.append(path)
            continue
        if actual == read_blob(target, path):
            continue
        if running_sha and actual == running_blob:
            continue
        discarded.append(path)
    return discarded


def rollback_blocked_by_schema(*, previous_schema, database_schema):
    """True when a rollback must NOT proceed on schema grounds.

    Nothing in InkDrop refuses a schema downgrade: `init_schema` never reads
    the stored version, so older code will migrate a newer database backwards
    and leave no record that it happened. The rollback path is therefore the
    one place that has to refuse.

    THE FIX THIS CARRIES. The original gate read

        if prev is not None and db is not None and prev < db: refuse

    so an unreadable value on EITHER side -- the image regex missing, the
    database query erroring -- fell through and the rollback proceeded anyway.
    The one path protecting the database against a silent backwards migration
    acted confidently when its own input was missing. That inverts the
    convention this codebase already applies elsewhere: `inkdrop_sab_failed_
    cleanup` fails closed on the explicit reasoning that a destructive action
    must not default to acting because its configuration went missing.

    Unknown is now refused. The cost of a false refusal is a human looking at a
    deploy; the cost of a false pass is an unrecorded backwards migration of
    the library database.
    """
    if previous_schema is None or database_schema is None:
        return True
    try:
        return int(previous_schema) < int(database_schema)
    except (TypeError, ValueError):
        return True
