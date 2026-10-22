#!/usr/bin/env python3
"""Manual Touch ID check for the owner's Mac. CI does not run this.

Enable it for hai-mcp by putting these in the MCP server environment and
restarting the server process (the client cannot turn the channel on):

    HAI_OWNER_CHANNEL=touchid
    HAI_OWNER_TOUCHID_TIMEOUT=60          # seconds the prompt waits; default 60

Optional:

    HAI_OWNER_TOUCHID_HELPER=$HOME/.local/bin/hai-touchid
    HAI_OWNER_TOUCHID_FALLBACK=file       # only if Touch ID cannot run at all
    HAI_OWNER_HOME=$HOME/.hai-owner       # used solely by that file fallback

`swift touchid_prompt.swift` compiles on first use and the dialog is slow.
Compile once so the prompt appears immediately:

    mkdir -p "$HOME/.local/bin"
    swiftc -O -o "$HOME/.local/bin/hai-touchid" src/hai_mcp/touchid_prompt.swift
    export HAI_OWNER_TOUCHID_HELPER="$HOME/.local/bin/hai-touchid"

Commands (from the repo, on the Mac — not in CI):

    uv run python scripts/manual_touchid.py prompt
        One native prompt. Exit 0 only after Touch ID, Face ID, or the
        device password succeeds. Cancel and timeout exit 1. Anything
        else (not macOS, helper missing) exits 2.

    uv run python scripts/manual_touchid.py gate
        Temporary HAI_HOME (does not touch ~/.hai). Asks hai_accept_next_step
        to show the prompt, checks the canonical file is still absent, then
        performs the agent's follow-up and checks the file was written.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path


def _cfg(root: Path, timeout: int):
    from hai_mcp.config import Config

    helper_raw = __import__("os").environ.get("HAI_OWNER_TOUCHID_HELPER", "").strip()
    helper = Path(helper_raw).expanduser().resolve() if helper_raw else None
    return Config(
        hai_home=root / "hai_home",
        owner_home=root / "owner_home",
        owner_channel="touchid",
        owner_touchid_timeout_seconds=timeout,
        owner_touchid_helper=helper,
    )


def _require_mac() -> int | None:
    if sys.platform == "darwin":
        return None
    print("Touch ID only runs on the owner's Mac. This machine is not macOS.", file=sys.stderr)
    print("Unit tests mock the helper: uv run pytest tests/test_owner_touchid_gate.py", file=sys.stderr)
    return 2


def cmd_prompt(timeout: int) -> int:
    blocked = _require_mac()
    if blocked is not None:
        return blocked
    from hai_mcp.owner_gate import OwnerChannelError, build_touchid_reason, invoke_touchid_helper

    with tempfile.TemporaryDirectory(prefix="hai-touchid-") as tmp:
        cfg = _cfg(Path(tmp), timeout)
        fingerprint = "sha256:" + ("ab" * 32)
        reason = build_touchid_reason(
            "manual_test",
            "Touch ID manual test",
            {"next_step": "sample approval; no mission is changed"},
            fingerprint,
        )
        print("Approve the Touch ID prompt to pass. Cancel to confirm the gate stays closed.")
        print(f"Reason the dialog should show:\n  {reason}")
        try:
            result = invoke_touchid_helper(cfg, reason=reason, fingerprint=fingerprint, timeout_seconds=timeout)
        except OwnerChannelError as exc:
            print(f"CLOSED {exc.kind} {exc.detail}: {exc}", file=sys.stderr)
            return 1 if exc.kind == "denied" else 2
    if result.get("fingerprint") != fingerprint or result.get("reason") != reason:
        print("CLOSED: helper attested a different prompt than the one requested.", file=sys.stderr)
        return 1
    print(f"PASS method={result.get('method') or 'device_owner'}")
    return 0


def cmd_gate(timeout: int) -> int:
    blocked = _require_mac()
    if blocked is not None:
        return blocked
    from hai_mcp.owner_gate import OwnerChannelError
    from hai_mcp.state import ControlPlane

    with tempfile.TemporaryDirectory(prefix="hai-touchid-gate-") as tmp:
        root = Path(tmp)
        project = root / "proj"
        (project / "Projek-Managment").mkdir(parents=True)
        plane = ControlPlane(_cfg(root, timeout))
        body = "# touchid manual check\n"
        proposed = plane.propose_next_step(str(project), body)
        if not proposed.get("ok"):
            print(f"FAIL propose: {proposed}", file=sys.stderr)
            return 1
        print("Approve the prompt. The canonical NEXT_STEP must not exist until the follow-up.")
        try:
            first = plane.accept_next_step(str(project))
        except OwnerChannelError as exc:
            print(f"CLOSED {exc.kind} {exc.detail}: {exc}", file=sys.stderr)
            return 1 if exc.kind == "denied" else 2
        canonical = project / "Projek-Managment" / "NEXT_STEP.md"
        if first.get("status") != "owner_approved":
            print(f"CLOSED: {first.get('error')} {first.get('detail', '')}: {first.get('message')}", file=sys.stderr)
            return 1 if first.get("error") == "owner_denied" else 2
        if canonical.exists():
            print("FAIL: canonical file was written before the follow-up.", file=sys.stderr)
            return 1
        second = plane.accept_next_step(str(project))
        if not second.get("ok"):
            print(f"FAIL follow-up: {second}", file=sys.stderr)
            return 1
        text = canonical.read_text(encoding="utf-8")
        if "touchid manual check" not in text or "Touch ID" not in text:
            print("FAIL: canonical file does not record the Touch ID approval.", file=sys.stderr)
            return 1
        print("PASS: Touch ID approved this exact change, and the follow-up wrote NEXT_STEP.md.")
        print("A further accept of new content will prompt again; this script does not send one.")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Manual Touch ID check for the HAI owner gate.")
    parser.add_argument("command", choices=("prompt", "gate"))
    parser.add_argument("--timeout", type=int, default=60, help="seconds to wait for Touch ID (default 60)")
    args = parser.parse_args()
    if args.command == "prompt":
        return cmd_prompt(args.timeout)
    return cmd_gate(args.timeout)


if __name__ == "__main__":
    sys.exit(main())
