#!/usr/bin/env python3
"""Bounded recovery worker for durable Manual Search runs."""

from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))


import argparse
import json
import os
import socket

from core import inkdrop_manual_search_core
from core import inkdrop_manual_search_executor
from core import inkdrop_runtime_config


def main() -> int:
    parser = argparse.ArgumentParser(description="Process queued or expired-lease Manual Search runs.")
    parser.add_argument("--state-db", default=str(inkdrop_runtime_config.state_db_path()))
    parser.add_argument("--limit", type=int, default=3)
    args = parser.parse_args()
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    result = inkdrop_manual_search_core.process_pending_search_runs(
        args.state_db,
        inkdrop_manual_search_executor.runner_for_db(args.state_db),
        limit=args.limit,
        worker_id=worker_id,
    )
    result["cleanup"] = inkdrop_manual_search_core.cleanup_search_runs(
        args.state_db,
        limit=max(100, args.limit * 20),
    )
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
