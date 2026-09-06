#!/usr/bin/env python3
"""Decide whether a published public release was actually validated.

The public repo cuts its GitHub Release by hand (see
docs/inkdrop/public-release-process.md, step 5). Nothing in that path consults
the validating workflow, so a release can be -- and every release to date has
been -- published while its own checks were red or still running. v0.1.14 was
published 24 seconds after its validation run started; that run finished five
minutes later having failed.

This tool is the missing consumer of that verdict. It answers one question --
"is this published release backed by a validation run that actually executed
and passed on exactly this commit?" -- and it answers no unless it can prove
yes. Every check fails closed: a run that is missing, still in progress,
concluded anything other than success, or whose required jobs were skipped
rather than executed, is refused.

A skipped job is not a passing job. That distinction is the whole reason this
file exists. The three publishing jobs in the public workflow are gated on
refs/heads/qa, a branch the public repo does not have, so they report skipped;
a workflow whose remaining jobs also skipped would conclude success and look
exactly like a green gate while having validated nothing.
"""

import argparse
import json
import subprocess
import sys

SCHEMA = "inkdrop-public-release-gate"
SCHEMA_VERSION = 1

DEFAULT_REPOSITORY = "jaredbahr/InkDrop"
DEFAULT_WORKFLOW = "inkdrop-public-release.yml"

# The jobs whose verdict a release depends on. A job absent from a run is as
# disqualifying as one that failed: it means the run did not do the work whose
# passing this release would be claiming.
DEFAULT_REQUIRED_JOBS = ("Public release smoke", "Full smoke suite")

# Conclusions that count as "this job actually ran and was satisfied".
# Deliberately a one-element allowlist rather than a denylist of bad states, so
# that a conclusion GitHub adds later cannot silently become acceptable.
PASSING_CONCLUSIONS = frozenset({"success"})


class GateError(RuntimeError):
    """A failure to gather evidence, as distinct from evidence of a failure."""


def gh_api(path, repository=None):
    """Fetch one GitHub API path as parsed JSON via the gh CLI."""
    target = path if repository is None else "repos/" + repository + "/" + path
    try:
        proc = subprocess.run(
            ["gh", "api", target],
            capture_output=True,
            timeout=120,
            text=True,
        )
    except FileNotFoundError as exc:
        raise GateError("the gh CLI is required to reach the GitHub API") from exc
    except subprocess.SubprocessError as exc:
        raise GateError("gh api " + target + " did not complete: " + str(exc)) from exc
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        if "Not Found" in stderr or "404" in stderr:
            return None
        raise GateError("gh api " + target + " failed: " + stderr[:400])
    try:
        return json.loads(proc.stdout or "null")
    except json.JSONDecodeError as exc:
        raise GateError("gh api " + target + " returned unparseable JSON") from exc


def _check(name, status, detail, **extra):
    payload = {"name": name, "status": status, "detail": detail}
    payload.update(extra)
    return payload


def resolve_release_commit(fetch, repository, tag):
    """The commit a tag points at, following annotated tags to their target."""
    ref = fetch("git/ref/tags/" + tag, repository)
    if not ref:
        return None
    obj = ref.get("object") or {}
    if obj.get("type") == "tag":
        annotated = fetch("git/tags/" + str(obj.get("sha")), repository)
        if not annotated:
            return None
        return ((annotated.get("object") or {}).get("sha")) or None
    return obj.get("sha") or None


def evaluate(fetch, repository, tag, workflow, required_jobs, required_assets):
    """Gather every piece of evidence and return the gate's verdict payload."""
    checks = []

    release = fetch("releases/tags/" + tag, repository)
    if not release:
        checks.append(_check(
            "release_exists", "missing",
            "no release is published for " + tag + " on " + repository,
        ))
        return _finish(repository, tag, None, None, checks)
    if release.get("draft"):
        checks.append(_check("release_exists", "failed", tag + " is still a draft"))
    else:
        checks.append(_check(
            "release_exists", "passed",
            tag + " is published (prerelease=" + str(bool(release.get("prerelease"))) + ")",
        ))

    commit = resolve_release_commit(fetch, repository, tag)
    if not commit:
        checks.append(_check(
            "tag_resolves_to_commit", "missing",
            "tag " + tag + " does not resolve to a commit on " + repository,
        ))
        return _finish(repository, tag, None, release, checks)
    checks.append(_check(
        "tag_resolves_to_commit", "passed",
        tag + " resolves to " + commit, commit=commit,
    ))

    checks.extend(
        commit_validation_checks(fetch, repository, commit, workflow, required_jobs)
    )
    if any(item["name"] == "validating_run_exists" and item["status"] != "passed"
           for item in checks):
        return _finish(repository, tag, commit, release, checks)
    return _finish_with_assets(
        repository, tag, commit, release, checks, required_assets
    )


def commit_validation_checks(fetch, repository, commit, workflow, required_jobs):
    """Did a run of `workflow` execute and pass on exactly this commit?

    Split out so the same question has one answer wherever it is asked. The gate
    asks it about a commit it reached through a published tag; the publisher in
    tools/inkdrop_github_release.py asks it about the commit it is ABOUT to
    publish, before any tag exists. Two copies of this would be two definitions
    of "validated", and the whole reason this file exists is that a release went
    out against a definition nobody was applying.

    Returns a list of checks. Every not-passed entry is a reason to refuse.
    """
    checks = []
    runs_payload = fetch(
        "actions/runs?head_sha=" + commit + "&per_page=100", repository
    ) or {}
    runs = [
        run for run in (runs_payload.get("workflow_runs") or [])
        if str(run.get("path") or "").endswith(workflow)
        or str(run.get("name") or "") == workflow
    ]
    if not runs:
        checks.append(_check(
            "validating_run_exists", "missing",
            "no " + workflow + " run exists for commit " + commit
            + "; nothing validated it",
        ))
        return checks

    # Newest run for the commit wins: a re-run is the operator's latest word.
    run = sorted(runs, key=lambda item: str(item.get("created_at") or ""))[-1]
    run_id = run.get("id")
    checks.append(_check(
        "validating_run_exists", "passed",
        "run " + str(run_id) + " targets commit " + commit, run_id=run_id,
    ))

    status = str(run.get("status") or "")
    conclusion = str(run.get("conclusion") or "")
    if status != "completed":
        checks.append(_check(
            "validating_run_completed", "failed",
            "run " + str(run_id) + " is " + (status or "in an unknown state")
            + ", not completed; the release was published before its validation finished",
            run_id=run_id,
        ))
    else:
        checks.append(_check(
            "validating_run_completed", "passed",
            "run " + str(run_id) + " completed", run_id=run_id,
        ))

    if conclusion in PASSING_CONCLUSIONS:
        checks.append(_check(
            "validating_run_passed", "passed",
            "run " + str(run_id) + " concluded " + conclusion, run_id=run_id,
        ))
    else:
        checks.append(_check(
            "validating_run_passed", "failed",
            "run " + str(run_id) + " concluded " + (conclusion or "nothing yet")
            + ", not success",
            run_id=run_id,
        ))

    jobs_payload = fetch(
        "actions/runs/" + str(run_id) + "/jobs?per_page=100", repository
    ) or {}
    jobs = {str(job.get("name")): job for job in (jobs_payload.get("jobs") or [])}
    for name in required_jobs:
        job = jobs.get(name)
        if job is None:
            checks.append(_check(
                "required_job:" + name, "missing",
                "run " + str(run_id) + " has no job named " + repr(name)
                + "; the check this release depends on was never part of the run",
            ))
            continue
        job_conclusion = str(job.get("conclusion") or "")
        if job_conclusion in PASSING_CONCLUSIONS:
            checks.append(_check(
                "required_job:" + name, "passed",
                name + " concluded " + job_conclusion,
            ))
        elif job_conclusion == "skipped":
            # The case this gate exists for. A job that did not run cannot have
            # validated anything, and reading it as a pass is exactly the
            # failure that let every release so far go out unchecked.
            checks.append(_check(
                "required_job:" + name, "failed",
                name + " was skipped, so its checks never executed; "
                "skipping is not passing",
            ))
        else:
            # Executed and was not satisfied, or never reached a verdict.
            # Distinct from the skip case above: here the checks did run.
            checks.append(_check(
                "required_job:" + name, "failed",
                name + " concluded " + (job_conclusion or "nothing")
                + ", not success",
            ))

    return checks


def _finish_with_assets(repository, tag, commit, release, checks, required_assets):
    assets = sorted(str(item.get("name")) for item in ((release or {}).get("assets") or []))
    missing_assets = [name for name in required_assets if name not in assets]
    if not required_assets:
        checks.append(_check(
            "release_assets", "passed",
            str(len(assets)) + " asset(s) attached; none required by this invocation",
            assets=assets,
        ))
    elif missing_assets:
        checks.append(_check(
            "release_assets", "failed",
            "release is missing required asset(s): " + ", ".join(missing_assets)
            + "; attached: " + (", ".join(assets) or "none"),
            assets=assets, missing=missing_assets,
        ))
    else:
        checks.append(_check(
            "release_assets", "passed",
            "all required assets attached: " + ", ".join(required_assets),
            assets=assets,
        ))
    return _finish(repository, tag, commit, release, checks)


def commit_is_validated(fetch, repository, commit, workflow=DEFAULT_WORKFLOW,
                        required_jobs=DEFAULT_REQUIRED_JOBS):
    """(ok, checks) for a commit, with no tag and no release involved.

    The publisher's precondition. Fails closed on anything that is not an
    explicit pass, including an empty check list, so a fetch that silently
    returns nothing cannot read as permission to publish.
    """
    checks = commit_validation_checks(fetch, repository, commit, workflow, required_jobs)
    ok = bool(checks) and all(item["status"] == "passed" for item in checks)
    return ok, checks


def _finish(repository, tag, commit, release, checks):
    blockers = [item for item in checks if item["status"] != "passed"]
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "repository": repository,
        "tag": tag,
        "release_commit": commit,
        "release_published_at": (release or {}).get("published_at"),
        "checks": checks,
        "blockers": blockers,
        # Both keys carry the same verdict and are both emitted, so a caller
        # cannot read a passing value out of a payload that has neither.
        "ok": not blockers,
        "release_validated": not blockers,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Refuse a public release that no passing validation run backs."
    )
    parser.add_argument("--tag", required=True, help="Release tag, e.g. v0.1.14.")
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY)
    parser.add_argument("--workflow", default=DEFAULT_WORKFLOW)
    parser.add_argument(
        "--required-job", action="append", default=None,
        help="Job that must have concluded success. Repeatable. "
             "Defaults to the two gating jobs.",
    )
    parser.add_argument(
        "--require-asset", action="append", default=None,
        help="Asset name the release must carry. Repeatable.",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    required_jobs = tuple(args.required_job or DEFAULT_REQUIRED_JOBS)
    required_assets = tuple(args.require_asset or ())

    try:
        payload = evaluate(
            gh_api,
            args.repository,
            args.tag,
            args.workflow,
            required_jobs,
            required_assets,
        )
    except GateError as exc:
        # Could not gather evidence. That is not permission to publish.
        print("release gate: could not verify -- " + str(exc), file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for item in payload["checks"]:
            print(item["status"] + ": " + item["name"] + " -- " + item["detail"])
        print("")
        if payload["ok"]:
            print("release gate: " + args.tag + " is backed by a passing validation run.")
        else:
            print("release gate: " + args.tag + " is NOT validated. Blockers:")
            for item in payload["blockers"]:
                print("  - " + item["status"] + ": " + item["name"] + " -- " + item["detail"])
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
