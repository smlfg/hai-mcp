#!/usr/bin/env python3
"""Slice-3 stdio smoke: real MCP client against isolated HAI_HOME.

Writes a project-local artifact under evals/stdio_smoke/.
Never touches ~/.hai or global Cursor MCP config.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = REPO / "evals" / "stdio_smoke"

EXPECTED_TOOLS = frozenset(
    {
        "hai_health",
        "hai_status",
        "hai_get_next_step",
        "hai_read_artifacts",
        "hai_park",
        "hai_set_focus",
        "hai_propose_next_step",
        "hai_accept_next_step",
        "hai_checkpoint",
        "hai_recover",
        "hai_open_mission",
        "hai_bind_project",
        "hai_authorize_session",
        "hai_get_contract",
        "hai_check_activity",
        "hai_park_item",
        "hai_recontract",
        "hai_close_mission",
        "hai_intake",
        "hai_distill",
        "hai_mission_start",
        "hai_drift_check",
        "hai_proof",
        "hai_stop",
    }
)


async def _call(session: ClientSession, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await session.call_tool(name, arguments)
    text = result.content[0].text if result.content else "{}"
    return json.loads(text)


async def run_smoke(*, hai_home: Path, project: Path) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    env = {**os.environ, "HAI_HOME": str(hai_home)}
    command = "uv"
    args = ["run", "--directory", str(REPO), "hai-mcp"]
    params = StdioServerParameters(command=command, args=args, env=env)

    started = time.time()
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            steps.append(
                {
                    "step": "initialize",
                    "ok": True,
                    "server": getattr(getattr(init, "serverInfo", None), "name", None),
                }
            )

            listed = await session.list_tools()
            names = sorted(t.name for t in listed.tools)
            missing = sorted(EXPECTED_TOOLS - set(names))
            extra = sorted(set(names) - EXPECTED_TOOLS)
            steps.append(
                {
                    "step": "list_tools",
                    "ok": not missing,
                    "tool_count": len(names),
                    "tools": names,
                    "missing": missing,
                    "extra": extra,
                }
            )

            health = await _call(session, "hai_health", {})
            steps.append(
                {
                    "step": "hai_health",
                    "ok": health.get("ok") is True and health.get("hai_home") == str(hai_home),
                    "response": health,
                }
            )

            # Gated fail-closed: integer owner_ack must not promote
            (project / "Projek-Managment").mkdir(parents=True, exist_ok=True)
            await _call(
                session,
                "hai_propose_next_step",
                {"project_path": str(project), "content": "# smoke gate\n"},
            )
            denied = await _call(
                session,
                "hai_accept_next_step",
                {"project_path": str(project), "owner_ack": 1, "reason": "smoke truthy int"},
            )
            steps.append(
                {
                    "step": "hai_accept_next_step_owner_ack_int",
                    "ok": denied.get("ok") is False and denied.get("error") == "owner_gate_required",
                    "response": denied,
                }
            )

            # Lifecycle: mission_start -> authorize -> drift (outside path)
            opened = await _call(
                session,
                "hai_mission_start",
                {
                    "problem": "write a single smoke note into out.md",
                    "artifact": "out.md",
                    "done_criteria": [
                        {"id": "c1", "description": "out.md exists", "evidence": "file"}
                    ],
                    "owner": "smoke",
                    "time_limit_hours": 1,
                    "constraints": {
                        "project_path": str(project),
                        "allowed_paths": ["."],
                        "capabilities": ["read", "write"],
                    },
                },
            )
            mid = opened.get("mission_id")
            ver = opened.get("contract_version")
            steps.append(
                {
                    "step": "hai_mission_start",
                    "ok": opened.get("ok") is True and opened.get("status") == "active" and bool(mid),
                    "response": {
                        k: opened.get(k)
                        for k in ("ok", "status", "mission_id", "contract_version", "error", "issues")
                    },
                }
            )

            auth: dict[str, Any] = {"ok": False}
            drift: dict[str, Any] = {"ok": False}
            if mid and ver is not None:
                auth = await _call(
                    session,
                    "hai_authorize_session",
                    {
                        "mission_id": mid,
                        "contract_version": ver,
                        "agent_identity": "smoke-agent",
                        "role": "coder",
                        "contribution": "smoke write",
                        "expected_result": "out.md",
                        "duration_minutes": 30,
                        "criterion_ids": ["c1"],
                        "capabilities": ["read", "write"],
                    },
                )
                steps.append(
                    {
                        "step": "hai_authorize_session",
                        "ok": auth.get("ok") is True and bool(auth.get("session_id")),
                        "response": {
                            k: auth.get(k) for k in ("ok", "session_id", "error", "message")
                        },
                    }
                )
                sid = auth.get("session_id")
                if sid:
                    drift = await _call(
                        session,
                        "hai_drift_check",
                        {
                            "session_id": sid,
                            "activity_step": "touching secrets",
                            "criterion_id": "c1",
                            "affected_paths": ["/etc/passwd"],
                            "declares_blocker": True,
                        },
                    )
                    steps.append(
                        {
                            "step": "hai_drift_check_outside_path",
                            "ok": (
                                drift.get("classification") == "drift"
                                and drift.get("required_action") == "stop"
                            ),
                            "response": {
                                k: drift.get(k)
                                for k in (
                                    "ok",
                                    "classification",
                                    "required_action",
                                    "reason",
                                    "error",
                                )
                            },
                        }
                    )

    elapsed_ms = int((time.time() - started) * 1000)
    all_ok = all(s.get("ok") for s in steps)
    return {
        "ok": all_ok,
        "exit_code": 0 if all_ok else 1,
        "elapsed_ms": elapsed_ms,
        "command": [command, *args],
        "env_boundary": {
            "HAI_HOME": str(hai_home),
            "project_path": str(project),
            "cwd": str(REPO),
            "touches_live_dot_hai": False,
        },
        "steps": steps,
    }


def write_artifact(payload: dict[str, Any]) -> Path:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    stamped = ARTIFACT_DIR / f"smoke-{stamp}.json"
    latest = ARTIFACT_DIR / "latest.json"
    text = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    stamped.write_text(text, encoding="utf-8")
    latest.write_text(text, encoding="utf-8")
    return latest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hai-home",
        type=Path,
        default=None,
        help="Isolated HAI_HOME (default: fresh /tmp dir)",
    )
    args = parser.parse_args(argv)

    hai_home = args.hai_home or Path(tempfile.mkdtemp(prefix="hai-mcp-stdio-smoke-"))
    hai_home.mkdir(parents=True, exist_ok=True)
    project = hai_home / "project"
    project.mkdir(parents=True, exist_ok=True)
    (project / "out.md").write_text("smoke\n", encoding="utf-8")

    try:
        payload = asyncio.run(run_smoke(hai_home=hai_home, project=project))
    except Exception as exc:  # noqa: BLE001 — smoke must record failure
        payload = {
            "ok": False,
            "exit_code": 2,
            "error": f"{type(exc).__name__}: {exc}",
            "env_boundary": {"HAI_HOME": str(hai_home), "project_path": str(project)},
            "steps": [],
        }

    path = write_artifact(payload)
    print(json.dumps({"artifact": str(path), "ok": payload.get("ok"), "exit_code": payload.get("exit_code")}, indent=2))
    return int(payload.get("exit_code", 1))


if __name__ == "__main__":
    sys.exit(main())
