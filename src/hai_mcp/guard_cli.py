"""`hai-guard`: hook entry point for Claude Code, Codex and any harness that can run a command.

Protocol (shared by Claude Code and Codex hooks): the hook payload arrives as JSON on stdin.
Exit 0 = allow. Exit 2 = block; stderr carries the reason, which the harness shows to the
model (PreToolUse) or the human (UserPromptSubmit). Exit 2 is also used for every internal
error, so a broken guard blocks instead of silently letting work through (fail-closed).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

BLOCK = 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HAI enforcement hook (reads hook JSON on stdin)")
    parser.add_argument(
        "--harness",
        default=os.environ.get("HAI_HARNESS_ID", "unknown"),
        help="harness id recorded in the audit chain (claude-code, codex, opencode, ...)",
    )
    parser.add_argument(
        "--device",
        default=os.environ.get("HAI_DEVICE_ID") or None,
        help="device id for project_id missions (env HAI_DEVICE_ID)",
    )
    parser.add_argument("--json", action="store_true", help="also print the decision as JSON on stdout")
    args = parser.parse_args(argv)

    try:
        from hai_mcp.config import Config
        from hai_mcp.guard import Guard, event_from_hook_payload
        from hai_mcp.state import ControlPlane

        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            raise ValueError("hook payload must be a JSON object")
        event = event_from_hook_payload(payload, args.harness, args.device)
        decision = Guard(ControlPlane(Config.from_env())).evaluate(event)
    except Exception as exc:  # fail closed: never let a crash become an implicit allow
        print(f"HAI guard error ({type(exc).__name__}: {exc}); blocking (fail-closed).", file=sys.stderr)
        return BLOCK

    if args.json:
        print(json.dumps(decision.as_dict(), ensure_ascii=False, sort_keys=True))
    if decision.allow:
        return 0
    suffix = f" Parked as intake {decision.intake_id}." if decision.intake_id else ""
    print(f"HAI blocked [{decision.code}]: {decision.reason}{suffix}", file=sys.stderr)
    return BLOCK


def entry() -> None:
    sys.exit(main())


if __name__ == "__main__":
    entry()
