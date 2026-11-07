"""Real MCP protocol coverage; only biometric evaluation is replaced by a fake helper."""
from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("client_label", ["Codex Desktop", "01a16479-9479-7eb3-8bf1-df7c27bbfcb2"])
def test_mcp_labels_reach_all_three_native_prompt_requests(tmp_path: Path, client_label: str) -> None:
    async def run() -> None:
        project = tmp_path / "project"
        (project / "Projek-Managment").mkdir(parents=True)
        helper = tmp_path / "helper"
        log = tmp_path / "prompts.jsonl"
        helper.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "request = json.load(sys.stdin)\n"
            "with open(os.environ['PROMPT_LOG'], 'a') as log: log.write(json.dumps(request) + '\\n')\n"
            "print(json.dumps({**request, 'ok': True, 'method': 'touchid'}))\n"
        )
        helper.chmod(0o755)
        env = {k: v for k, v in os.environ.items() if not k.startswith("HAI_OWNER_")}
        env.update(HAI_HOME=str(tmp_path / "hai"), HAI_OWNER_CHANNEL="touchid",
                   HAI_OWNER_TOUCHID_HELPER=str(helper), PROMPT_LOG=str(log),
                   PYTHONPATH=str(REPO / "src"))
        params = StdioServerParameters(command=sys.executable, args=["-c",
            "from hai_mcp import owner_gate; owner_gate._touchid_platform_ok = lambda: True; "
            "from hai_mcp.server import main; main()"], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, client_info=types.Implementation(
                    name="mcp-test-client", title=client_label, version="1")) as session:
                await session.initialize()
                async def call(name: str, args: dict) -> dict:
                    result = await session.call_tool(name, args)
                    assert not result.isError, result
                    return json.loads(result.content[0].text)
                tools = (await session.list_tools()).tools
                for tool in tools:
                    if tool.name in {"hai_accept_next_step", "hai_recontract", "hai_close_mission"}:
                        assert "approval_context" in tool.inputSchema["properties"]
                        assert "ctx" not in tool.inputSchema["properties"]
                context = {"session": "Touch ID lesbar machen", "topic": "Dialog verbessern"}
                accept = {"project_path": str(project), "content": "Dialog prüfen", "approval_context": context}
                assert (await call("hai_accept_next_step", accept))["status"] == "owner_approved"
                assert (await call("hai_accept_next_step", accept))["ok"]
                opened = await call("hai_open_mission", {
                    "objective": "Dialog verbessern", "artifact": "Dialog", "done_criteria": [{"id": "dc-1", "description": "Lesbar"}],
                    "non_goals": ["Server ändern"], "constraints": {"project_path": str(project),
                    "allowed_paths": ["src"], "capabilities": ["read", "write"], "time_limit_hours": 2}, "owner": "Samuel"})
                assert opened["ok"], opened
                recontract = {"mission_id": opened["mission_id"], "contract_version": 1, "reason": "Klarheit",
                              "changes": {"objective": "Lesbarer Dialog"}, "approval_context": {**context, "agent": "Test Worker"}}
                assert (await call("hai_recontract", recontract))["status"] == "owner_approved"
                assert (await call("hai_recontract", recontract))["ok"]
                close = {"mission_id": opened["mission_id"], "contract_version": 2, "closure": "abandoned",
                         "outcome_summary": "Test abgeschlossen", "approval_context": context}
                assert (await call("hai_close_mission", close))["status"] == "owner_approved"
                assert (await call("hai_close_mission", close))["ok"]
        prompts = [json.loads(line) for line in log.read_text().splitlines()]
        assert len(prompts) == 3
        for i, prompt in enumerate(prompts):
            reason = prompt["reason"]
            expected_agent = "Test Worker" if i == 1 else ("Codex Desktop" if client_label == "Codex Desktop" else "Agent nicht benannt")
            assert f"Agent: {expected_agent}" in reason
            assert "Session: Touch ID lesbar machen" in reason
            assert "Thema: Dialog verbessern" in reason
            assert opened["mission_id"] not in reason
            assert prompt["fingerprint"].removeprefix("sha256:")[:12] not in reason
        assert "Nächsten Schritt übernehmen" in prompts[0]["reason"]
        assert "Auftrag ändern" in prompts[1]["reason"] and "Lesbarer Dialog" in prompts[1]["reason"]
        assert "Auftrag abbrechen" in prompts[2]["reason"] and "Test abgeschlossen" in prompts[2]["reason"]
    asyncio.run(run())
