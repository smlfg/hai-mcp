"""Tool-definition quality: descriptions, parameter docs, and annotations."""

from __future__ import annotations

import anyio

from hai_mcp.server import mcp

READ_ONLY_TOOLS = {
    "hai_health",
    "hai_status",
    "hai_get_next_step",
    "hai_read_artifacts",
    "hai_recover",
    "hai_get_contract",
}


def test_tool_definitions_are_documented_and_annotated() -> None:
    async def _run() -> None:
        tools = await mcp.list_tools()
        assert tools

        for tool in tools:
            assert tool.description and "Use" in tool.description, tool.name

            properties = tool.inputSchema.get("properties", {})
            for name, schema in properties.items():
                assert schema.get("description"), f"{tool.name}.{name} has no description"

            annotations = tool.annotations
            assert annotations is not None, f"{tool.name} has no annotations"
            assert annotations.readOnlyHint is not None, tool.name
            assert annotations.destructiveHint is not None, tool.name
            assert annotations.openWorldHint is False, tool.name

            expected_read_only = tool.name in READ_ONLY_TOOLS
            assert annotations.readOnlyHint is expected_read_only, tool.name
            if expected_read_only:
                assert annotations.destructiveHint is False, tool.name

    anyio.run(_run)
