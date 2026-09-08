from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = Path(__file__).resolve().parents[1]
CODE_RE = re.compile(r"HAI owner code: ([2-9A-HJKMNP-Z]{4}-[2-9A-HJKMNP-Z]{4})")


async def _call(session: ClientSession, name: str, arguments: dict) -> dict:
    result = await session.call_tool(name, arguments)
    assert result.content
    return json.loads(result.content[0].text)


def test_stdio_default_gate_needs_the_owners_code(tmp_path: Path) -> None:
    """Over real stdio, with the DEFAULT configuration: owner_ack=true is not a gate anymore."""

    async def _run() -> None:
        hai_home = tmp_path / "stdio-home"
        owner_home = tmp_path / "stdio-owner"  # next to HAI_HOME, not inside it
        project = tmp_path / "proj"
        (project / "Projek-Managment").mkdir(parents=True)

        env = {k: v for k, v in os.environ.items() if not k.startswith("HAI_OWNER_")}
        env["HAI_HOME"] = str(hai_home)
        env["HAI_OWNER_HOME"] = str(owner_home)
        params = StdioServerParameters(
            command="uv",
            args=["run", "--directory", str(REPO), "hai-mcp"],
            env=env,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                health = await _call(session, "hai_health", {})
                assert health["owner_gate"]["mode"] == "nonce"

                await _call(session, "hai_propose_next_step", {"project_path": str(project), "content": "# gated\n"})

                # The old bypass: the client asserts approval itself.
                pending = await _call(
                    session,
                    "hai_accept_next_step",
                    {"project_path": str(project), "owner_ack": True, "reason": "I promise the owner said yes"},
                )
                assert pending["ok"] is False
                assert pending["error"] == "owner_gate_required"
                assert pending["status"] == "pending_owner_code"
                assert not (project / "Projek-Managment" / "NEXT_STEP.md").is_file()

                # The human reads the code from the owner channel and relays it.
                owner_file = owner_home / f"{pending['challenge_id']}.txt"
                code = CODE_RE.search(owner_file.read_text(encoding="utf-8")).group(1)

                ok = await _call(session, "hai_accept_next_step", {"project_path": str(project), "owner_code": code})
                assert ok["ok"] is True
                assert ok["owner_gate"]["challenge_id"] == pending["challenge_id"]
                assert (project / "Projek-Managment" / "NEXT_STEP.md").is_file()

    asyncio.run(_run())
