#!/usr/bin/env python3
"""Hold the adversarial candidate-matching corpus at its committed baseline.

The corpus (tests/fixtures/inkdrop-candidate-matching-benchmark-v1.json,
scored by tests/inkdrop-candidate-matching-benchmark.py) is a curated,
adversarial gold set -- 50 hand-picked hard cases, not a general accuracy
measurement. Its score says how the matcher handles those 50 cases, not what
fraction of real-world matches succeed.

The gate here is non-regression against
tests/fixtures/inkdrop-candidate-matching-benchmark-baseline.json, not a
hardcoded 100%. Pinning this to "must always be perfect" would break CI the
moment someone adds a new, harder corpus case that nothing has fixed yet --
punishing exactly the growth that makes the corpus more useful over time.
Instead: the score must not drop below the committed baseline. Raising the
baseline (a gap got closed -- lock in the improvement) or lowering it (a new,
not-yet-solved case was added on purpose) are both explicit, reviewed edits to
that file, made in the same PR as the change that causes the move.

It exists as a separate file because the smoke suite discovers its tests with
`git ls-files tests/inkdrop-*smoke*.py` (tools/inkdrop_run_smoke_suite.py) and
the scorer is not a smoke test -- it is a scoring tool that reports a number
and exits 0 whatever that number is, which is what makes it useful to run by
hand while working on the matcher. Wrapping it here turns the same run into a
gate without taking that away.

A failure here means one of two things, and the printed diff says which:
either a matcher change regressed a case the corpus pins, or the corpus grew
a case that needs its own reviewed baseline bump. Do not "fix" it by editing
an expected verdict, or by lowering the baseline, without deciding which one
first.
"""

import importlib.util
import json
import sys
from pathlib import Path


SCORER = Path(__file__).with_name("inkdrop-candidate-matching-benchmark.py")
BASELINE = Path(__file__).with_name("fixtures") / "inkdrop-candidate-matching-benchmark-baseline.json"
BASELINE_SCHEMA = "inkdrop.candidate-matching-benchmark-baseline"
BASELINE_SCHEMA_VERSION = 1


def load_scorer():
    spec = importlib.util.spec_from_file_location("inkdrop_candidate_matching_benchmark", SCORER)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load the benchmark scorer at {SCORER}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_baseline():
    payload = json.loads(BASELINE.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise AssertionError(f"baseline root must be an object at {BASELINE}")
    if payload.get("schema") != BASELINE_SCHEMA or payload.get("schema_version") != BASELINE_SCHEMA_VERSION:
        raise AssertionError(f"unsupported baseline schema at {BASELINE}")
    score = payload.get("score_percent")
    if not isinstance(score, (int, float)):
        raise AssertionError(f"baseline missing numeric score_percent at {BASELINE}")
    return payload


def main():
    if not SCORER.exists():
        print(f"CANDIDATE_MATCHING_BENCHMARK_FAIL: scorer missing at {SCORER}")
        return 1
    if not BASELINE.exists():
        print(f"CANDIDATE_MATCHING_BENCHMARK_FAIL: baseline missing at {BASELINE}")
        return 1

    baseline = load_baseline()
    scorer = load_scorer()

    try:
        result = scorer.score(scorer.load_corpus(scorer.DEFAULT_CORPUS))
    except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError) as exc:
        print(f"CANDIDATE_MATCHING_BENCHMARK_FAIL: corpus invalid: {type(exc).__name__}: {exc}")
        return 2

    floor = baseline["score_percent"]
    current = result["score_percent"]

    if current < floor:
        print(
            f"CANDIDATE_MATCHING_BENCHMARK_FAIL: curated-corpus score regressed to "
            f"{result['passed']}/{result['total']} ({current:.2f}%), below the committed "
            f"baseline of {baseline['passed']}/{baseline['total']} ({floor:.2f}%) in "
            f"{BASELINE.name}. Either a matcher change regressed a pinned case, or a newly "
            "added corpus case needs its own reviewed baseline bump -- decide which before "
            "editing either."
        )
        for failure in result["failures"]:
            print(f"  FAIL {failure['id']}: expected {failure['expected']}; observed {failure['observed']}")
        return 1

    print(
        f"CANDIDATE_MATCHING_BENCHMARK_OK: curated-corpus score "
        f"{result['passed']}/{result['total']} ({current:.2f}%) holds the committed baseline "
        f"of {floor:.2f}% in {BASELINE.name}"
    )
    if current > floor:
        print(
            f"  note: score is above baseline -- consider bumping {BASELINE.name} to "
            "score_percent "
            f"{current:.2f} ({result['passed']}/{result['total']}) to lock in the improvement"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
