#!/usr/bin/env python3
"""Score the fixed adversarial candidate-matching corpus against InkDrop."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from core import inkdrop_candidate_matching as matching  # noqa: E402
from core import inkdrop_slskd_source_probe as slskd_probe  # noqa: E402
from core import inkdrop_source_providers as source_providers  # noqa: E402


SCHEMA = "inkdrop.candidate-matching-benchmark"
SCHEMA_VERSION = 1
DEFAULT_CORPUS = (
    Path(__file__).with_name("fixtures")
    / "inkdrop-candidate-matching-benchmark-v1.json"
)


def load_corpus(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("corpus root must be an object")
    if payload.get("schema") != SCHEMA or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported benchmark schema")
    if payload.get("matcher_contract_version") != matching.CONTRACT_VERSION:
        raise ValueError(
            f"corpus pins matcher contract {payload.get('matcher_contract_version')!r}; "
            f"runtime provides {matching.CONTRACT_VERSION!r}"
        )
    cases = payload.get("cases")
    # The upper bound was 60 when this corpus was purely hand-authored
    # adversarial cases. It now also carries ground-truth pairs taken from
    # real production rejections, and Jared is supplying more by hand, so the
    # ceiling is raised rather than forcing a second corpus file that would
    # need its own loader, scorer and gate. The lower bound still guards
    # against a truncated or partially-written corpus.
    if not isinstance(cases, list) or not 40 <= len(cases) <= 400:
        raise ValueError("corpus must contain 40 through 400 cases")
    if payload.get("case_count") != len(cases):
        raise ValueError("case_count does not match cases")
    identifiers = [case.get("id") for case in cases if isinstance(case, dict)]
    if len(identifiers) != len(cases) or any(not value for value in identifiers):
        raise ValueError("every case must be an object with a nonempty id")
    duplicates = [value for value, count in Counter(identifiers).items() if count > 1]
    if duplicates:
        raise ValueError(f"duplicate case ids: {duplicates}")
    return payload


def _slskd_verdict(case: dict, candidate: dict) -> dict:
    """Run a case through the slskd admission filter, then its own authority.

    Mirrors what the probe does per peer file: build the file candidate, ask
    ``shared_candidate_match_details`` whether the file is admissible at all,
    and only then consult ``candidate_identity_compatibility`` -- the slskd
    authority, which merges peer vetoes the shared matcher cannot see.
    """
    wanted = case["wanted"]
    path = candidate.get("filename") or candidate.get("title") or ""
    probe_candidate = dict(candidate)
    probe_candidate.setdefault("username", "benchmark-peer")
    probe_candidate.setdefault("has_free_upload_slot", True)
    probe_candidate.setdefault("queue_length", 0)
    probe_candidate.setdefault("extension", slskd_probe.extension_for(path))
    probe_candidate["size"] = candidate.get("size_bytes") or candidate.get("size") or 40_000_000
    details = slskd_probe.shared_candidate_match_details(path, wanted, candidate=probe_candidate)
    if not details.get("matched"):
        penalties = [str(value) for value in (details.get("penalties") or []) if value]
        return {
            "pipeline": "slskd",
            "normalized_match_confidence": candidate.get("match_confidence"),
            "status": "blocked",
            "code": penalties[0] if penalties else "slskd_file_not_admitted",
            "codes": penalties,
            "positive_evidence": [],
        }
    compatibility, _identity = slskd_probe.candidate_identity_compatibility(
        probe_candidate, path, wanted
    )
    status = compatibility["status"]
    codes = (
        compatibility["rejection_codes"]
        if status == "blocked"
        else compatibility["review_codes"]
        if status == "review"
        else []
    )
    return {
        "pipeline": "slskd",
        "normalized_match_confidence": candidate.get("match_confidence"),
        "status": status,
        "code": codes[0] if codes else None,
        "codes": codes,
        "positive_evidence": compatibility.get("positive_evidence") or [],
    }


def observed_verdict(case: dict) -> dict:
    pipeline = case.get("pipeline", "direct")
    candidate = case["candidate"]
    if pipeline == "prowlarr":
        candidate = source_providers.prowlarr_candidate_from_result(
            candidate,
            case.get("registry_row"),
            case["wanted"],
        )
    elif pipeline == "mangadex":
        candidates = source_providers.mangadex_candidates_from_payload(
            candidate,
            case.get("registry_row"),
            case["wanted"],
            limit=2,
        )
        if len(candidates) != 1:
            raise ValueError(
                f"{case.get('id')}: MangaDex normalization produced "
                f"{len(candidates)} candidates; expected exactly one"
            )
        candidate = candidates[0]
    elif pipeline == "slskd":
        # The real thing: stage one of the slskd pipeline is a hard admission
        # filter with its own title matcher, and nothing it refuses ever
        # reaches the shared matcher. A case that names slskd and runs the
        # generic path measures a code path slskd does not take -- which is
        # how two cases came to pin the same series-less file as compatible
        # with two unrelated works.
        return _slskd_verdict(case, candidate)
    elif pipeline != "direct":
        raise ValueError(f"{case.get('id')}: unsupported pipeline {pipeline!r}")
    compatibility = matching.candidate_compatibility(candidate, case["wanted"])
    status = compatibility["status"]
    codes = (
        compatibility["rejection_codes"]
        if status == "blocked"
        else compatibility["review_codes"]
        if status == "review"
        else []
    )
    return {
        "pipeline": pipeline,
        "normalized_match_confidence": candidate.get("match_confidence"),
        "status": status,
        "code": codes[0] if codes else None,
        "codes": codes,
        "positive_evidence": compatibility.get("positive_evidence") or [],
    }


def case_passes(case: dict, observed: dict) -> tuple[bool, list[str]]:
    expected = case.get("expected")
    if not isinstance(expected, dict):
        raise ValueError(f"{case.get('id')}: expected must be an object")
    if expected.get("status") not in {"compatible", "review", "blocked"}:
        raise ValueError(f"{case.get('id')}: invalid expected status")
    failures = []
    if observed["status"] != expected["status"]:
        failures.append(f"status expected {expected['status']}, observed {observed['status']}")
    if "code" in expected and observed["code"] != expected.get("code"):
        failures.append(f"code expected {expected.get('code')!r}, observed {observed['code']!r}")
    required = set(expected.get("required_codes") or [])
    missing = sorted(required - set(observed["codes"]))
    if missing:
        failures.append(f"missing required codes {missing}")
    forbidden = sorted(set(expected.get("forbidden_codes") or []) & set(observed["codes"]))
    if forbidden:
        failures.append(f"observed forbidden codes {forbidden}")
    return not failures, failures


def score(payload: dict) -> dict:
    by_category = defaultdict(lambda: {"passed": 0, "total": 0})
    failures = []
    for case in payload["cases"]:
        for key in ("category", "description", "wanted", "candidate", "expected"):
            if key not in case:
                raise ValueError(f"{case.get('id')}: missing {key}")
        observed = observed_verdict(case)
        passed, reasons = case_passes(case, observed)
        category = case["category"]
        by_category[category]["total"] += 1
        if passed:
            by_category[category]["passed"] += 1
        else:
            failures.append(
                {
                    "id": case["id"],
                    "category": category,
                    "description": case["description"],
                    "expected": case["expected"],
                    "observed": observed,
                    "differences": reasons,
                }
            )
    total = len(payload["cases"])
    passed = total - len(failures)
    return {
        "benchmark_id": payload["benchmark_id"],
        "benchmark_version": payload["benchmark_version"],
        "matcher_contract_version": matching.CONTRACT_VERSION,
        "passed": passed,
        "total": total,
        "score_percent": round(100.0 * passed / total, 2),
        "by_category": dict(sorted(by_category.items())),
        "failures": failures,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--require-perfect", action="store_true")
    parser.add_argument("--json", action="store_true", help="emit only machine-readable JSON")
    args = parser.parse_args(argv)
    try:
        result = score(load_corpus(args.corpus))
    except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError) as exc:
        print(f"BENCHMARK_INVALID: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(
            f"candidate matching adversarial v{result['benchmark_version']}: "
            f"{result['passed']}/{result['total']} ({result['score_percent']:.2f}%)"
        )
        for category, counts in result["by_category"].items():
            print(f"  {category}: {counts['passed']}/{counts['total']}")
        for failure in result["failures"]:
            print(
                f"  FAIL {failure['id']}: "
                f"expected {failure['expected']}; observed {failure['observed']}"
            )
    return 1 if args.require_perfect and result["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
