#!/usr/bin/env python3
"""Local-only recovery and session administration for InkDrop."""

from __future__ import annotations

import argparse
import getpass
import json

import inkdrop_auth
import inkdrop_runtime_config


def main(argv=None):
    parser = argparse.ArgumentParser(description="InkDrop local authentication recovery utility")
    parser.add_argument("--state-db", default=str(inkdrop_runtime_config.state_db_path()))
    sub = parser.add_subparsers(dest="command", required=True)
    token = sub.add_parser("recovery-token", help="Create a short-lived one-time password recovery token")
    token.add_argument("--username")
    token.add_argument("--ttl-seconds", type=int, default=inkdrop_auth.RECOVERY_TTL_SECONDS)
    reset = sub.add_parser("reset-password", help="Reset the admin password using a one-time recovery token")
    cleanup = sub.add_parser("cleanup", help="Delete stale authentication state in a bounded batch")
    cleanup.add_argument("--batch-limit", type=int, default=100)
    args = parser.parse_args(argv)
    if args.command == "recovery-token":
        result = inkdrop_auth.create_recovery_token(
            args.state_db,
            username=args.username,
            ttl_seconds=args.ttl_seconds,
        )
    elif args.command == "reset-password":
        recovery_token = getpass.getpass("One-time InkDrop recovery token: ")
        password = getpass.getpass("New InkDrop admin password: ")
        result = inkdrop_auth.reset_password_with_recovery(args.state_db, recovery_token, password)
    else:
        result = inkdrop_auth.cleanup_auth_state(args.state_db, batch_limit=args.batch_limit)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
