from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = Path(__file__).resolve().parents[1]


async def _call(session: ClientSession, name: str, arguments: dict) -> dict:
    result = await session.call_tool(name, arguments)
    assert result.content
    return json.loads(result.content[0].text)


def test_stdio_owner_ack_integer_fails_closed(tmp_path: Path) -> None:
    """FastMCP bool params coerce JSON 1 -> True; Any preserves int for fail-closed gates."""

    async def _run() -> None:
        os.environ["HAI_HOME"] = str(tmp_path / "stdio-home")
        project = tmp_path / "proj"
        (project / "Projek-Managment").mkdir(parents=True)

        params = StdioServerParameters(
            command="uv",
            args=["run", "--directory", str(REPO), "hai-mcp"],
            env={**os.environ, "HAI_OWNER_GATE": "ack_legacy"},  # legacy gate under test; nonce has its own stdio test
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                await _call(
                    session,
                    "hai_propose_next_step",
                    {"project_path": str(project), "content": "# gated\n"},
                )
                denied = await _call(
                    session,
                    "hai_accept_next_step",
                    {"project_path": str(project), "owner_ack": 1, "reason": "truthy int"},
                )
                assert denied["ok"] is False
                assert denied["error"] == "owner_gate_required"
                assert not (project / "Projek-Managment" / "NEXT_STEP.md").is_file()

                ok = await _call(
                    session,
                    "hai_accept_next_step",
                    {"project_path": str(project), "owner_ack": True, "reason": "literal true"},
                )
                assert ok["ok"] is True

    asyncio.run(_run())
