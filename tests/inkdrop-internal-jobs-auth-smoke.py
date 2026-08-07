#!/usr/bin/env python3

import sys
import types

from core import inkdrop_internal_jobs


calls = []
fake_web = types.SimpleNamespace(
    scan_comic_series=lambda payload: calls.append(("comicvine", payload)) or {"status": "configuration_needed"},
    resolve_noop_manual_reviews=lambda: calls.append(("review", None)) or {"status": "ok", "resolved": 2},
)
original = sys.modules.get("core.inkdrop_web")
sys.modules["core.inkdrop_web"] = fake_web
try:
    assert inkdrop_internal_jobs.run_job("comicvine-scan") == (78, {"status": "configuration_needed"})
    assert inkdrop_internal_jobs.run_job("manual-review-noop-resolve") == (
        0,
        {"status": "ok", "resolved": 2},
    )
finally:
    if original is None:
        sys.modules.pop("core.inkdrop_web", None)
    else:
        sys.modules["core.inkdrop_web"] = original

assert calls == [("comicvine", {}), ("review", None)]
print("INKDROP_INTERNAL_JOBS_AUTH_OK")
