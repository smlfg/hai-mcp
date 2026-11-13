"""Read-only lease guard: may this path be written right now?

Meant for a harness hook that runs before a file write. It turns the voluntary
"report via hai_check_activity" rule into an enforced one for governed projects.

A path is *governed* when it lies under a device mount in ``core/projects.json``
or under the active mission's project root. Outside governed roots the guard
always allows: it must never lock a whole machine.

Inside a governed root a write needs, in this order: an active mission whose
root contains the path, at least one valid session lease, and a match against
``constraints.allowed_paths``.

The guard never writes to HAI_HOME and never calls an LLM. It cannot tell which
session is writing; any valid lease on the active mission is enough.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from hai_mcp.config import Config
from hai_mcp.ids import validate_mission_id
from hai_mcp.paths import PathError, assert_relative_allowed, real_path

EXIT_ALLOW = 0
EXIT_BLOCK = 2  # Claude Code hooks treat exit 2 as "block and show stderr to the agent"


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _read(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _contains(root: Path, target: Path) -> bool:
    try:
        target.relative_to(real_path(root))
        return True
    except ValueError:
        return False


def _mount_roots(hai_home: Path) -> dict[str, list[Path]]:
    """project_id -> mount paths (all devices; paths that do not exist here never match)."""
    data = _read(hai_home / "core" / "projects.json", {})
    out: dict[str, list[Path]] = {}
    for pid, project in (data.get("projects") or {}).items():
        for mount in (project.get("mounts") or {}).values():
            raw = mount.get("path") if isinstance(mount, dict) else None
            if raw:
                out.setdefault(str(pid), []).append(Path(str(raw)))
    return out


def _valid_lease(sessions_dir: Path, meta: dict[str, Any], contract: dict[str, Any]) -> bool:
    if not sessions_dir.is_dir():
        return False
    now = _utc_now()
    for path in sorted(sessions_dir.glob("*.json")):
        session = _read(path, {})
        if session.get("status") != "active":
            continue
        if session.get("contract_version") != meta.get("current_version"):
            continue
        if session.get("contract_hash") != contract.get("contract_hash"):
            continue
        expires_at = session.get("expires_at")
        if expires_at and now > str(expires_at):
            continue
        return True
    return False


def _allow(reason: str, **extra: Any) -> dict[str, Any]:
    return {"allow": True, "reason": reason, **extra}


def _block(reason: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"allow": False, "reason": reason, "message": message, **extra}


def decide(hai_home: Path, raw_path: str) -> dict[str, Any]:
    """Return ``{"allow": bool, "reason": str, ...}`` for a planned write to *raw_path*."""
    if not raw_path or "\x00" in raw_path:
        return _block("invalid_path", "path is empty or contains a null byte")
    try:
        target = real_path(Path(raw_path).expanduser().absolute())
        mounts = _mount_roots(hai_home)
        governing = sorted(pid for pid, roots in mounts.items() if any(_contains(r, target) for r in roots))

        pointer = _read(hai_home / "missions" / "active_mission.json", {})
        mission_id = pointer.get("mission_id") if pointer.get("status") == "active" else None
        meta: dict[str, Any] = {}
        contract: dict[str, Any] = {}
        active_root: Path | None = None
        if mission_id:
            ok, msg = validate_mission_id(str(mission_id))
            if not ok:
                return _block("state_unreadable", f"active mission id is invalid: {msg}")
            mdir = hai_home / "missions" / str(mission_id)
            meta = _read(mdir / "mission.json", {})
            if meta.get("status") != "active":
                mission_id = None
            else:
                contract = _read(mdir / "contracts" / f"v{int(meta['current_version'])}.json", {})
                constraints = contract.get("constraints") or {}
                pid = constraints.get("project_id")
                if pid:
                    roots = mounts.get(str(pid), [])
                else:
                    roots = [Path(str(constraints["project_path"]))] if constraints.get("project_path") else []
                active_root = next((r for r in roots if _contains(r, target)), None)

        if not governing and active_root is None:
            return _allow("ungoverned")
        if not mission_id:
            return _block(
                "no_active_mission",
                f"{target} belongs to project {governing} but no mission is active; open one first",
                projects=governing,
            )
        if active_root is None:
            return _block(
                "other_mission_active",
                f"{target} belongs to project {governing}, but the active mission {mission_id} is for another project",
                mission_id=mission_id,
                projects=governing,
            )
        if not _valid_lease(hai_home / "missions" / str(mission_id) / "sessions", meta, contract):
            return _block(
                "no_valid_lease",
                f"mission {mission_id} has no valid session lease; call hai_authorize_session",
                mission_id=mission_id,
            )
        allowed_paths = (contract.get("constraints") or {}).get("allowed_paths") or []
        try:
            assert_relative_allowed(target, active_root, list(allowed_paths))
        except PathError:
            return _block(
                "path_outside_contract",
                f"{target} is outside allowed_paths {allowed_paths} of mission {mission_id}; park it or recontract",
                mission_id=mission_id,
                allowed_paths=allowed_paths,
            )
        return _allow("in_contract", mission_id=mission_id)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        # Unreadable or malformed control-plane state: fail closed, like every other gate.
        return _block("state_unreadable", f"HAI state could not be read: {exc}")


def _path_from_hook_stdin(raw: str) -> str | None:
    """Extract the target path from a Claude Code PreToolUse hook payload."""
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    tool_input = payload.get("tool_input") if isinstance(payload, dict) else None
    if not isinstance(tool_input, dict):
        return None
    for key in ("file_path", "notebook_path", "path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hai-guard",
        description="Read-only check: may this path be written under the active HAI mission? Exit 0 allow, 2 block.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--path", help="path about to be written")
    source.add_argument(
        "--hook-stdin",
        action="store_true",
        help="read a Claude Code PreToolUse payload from stdin and use tool_input.file_path",
    )
    args = parser.parse_args(argv)

    if args.hook_stdin:
        path = _path_from_hook_stdin(sys.stdin.read())
        if path is None:
            # A tool call without a file path is not a file write; nothing to guard.
            print(json.dumps(_allow("no_path_in_payload")))
            return EXIT_ALLOW
    else:
        path = args.path

    result = decide(Config.from_env().hai_home, path)
    print(json.dumps(result, ensure_ascii=False))
    if result["allow"]:
        return EXIT_ALLOW
    print(f"HAI guard blocked this write ({result['reason']}): {result['message']}", file=sys.stderr)
    return EXIT_BLOCK


if __name__ == "__main__":
    raise SystemExit(main())
