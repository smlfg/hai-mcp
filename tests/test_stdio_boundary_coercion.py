"""Stdio integration tests: JSON boundary coercion must not bypass gates or limits."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = Path(__file__).resolve().parents[1]


async def _call(session: ClientSession, name: str, arguments: dict) -> dict:
    result = await session.call_tool(name, arguments)
    assert result.content
    return json.loads(result.content[0].text)


async def _with_stdio(tmp_path: Path):
    os.environ["HAI_HOME"] = str(tmp_path / "stdio-home")
    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)
    (project / "tests").mkdir(parents=True)

    params = StdioServerParameters(
        command="uv",
        args=["run", "--directory", str(REPO), "hai-mcp"],
        env={**os.environ, "HAI_OWNER_GATE": "ack_legacy"},  # legacy gate under test; nonce has its own stdio test
    )
    return project, params


async def _open_mission(session: ClientSession, project: Path) -> dict:
    return await _call(
        session,
        "hai_open_mission",
        {
            "objective": "Boundary coercion probe mission",
            "artifact": "tests/test_stdio_boundary_coercion.py green",
            "done_criteria": [{"id": "dc-1", "description": "stdio coercion tests pass"}],
            "owner": "samuel",
            "constraints": {
                "project_path": str(project),
                "allowed_paths": ["src/", "tests/"],
                "capabilities": ["read", "write", "test"],
                "max_parallel_sessions": 1,
            },
        },
    )


async def _authorize(
    session: ClientSession,
    opened: dict,
    *,
    contract_version: object,
    duration_minutes: object,
) -> dict:
    return await _call(
        session,
        "hai_authorize_session",
        {
            "mission_id": opened["mission_id"],
            "contract_version": contract_version,
            "agent_identity": "composer",
            "role": "builder",
            "contribution": "boundary tests",
            "expected_result": "green",
            "duration_minutes": duration_minutes,
            "criterion_ids": ["dc-1"],
        },
    )


def test_stdio_break_glass_marker_integer_fails_closed(tmp_path: Path) -> None:
    """JSON break_glass_marker: 1 must not satisfy break-glass friction."""

    async def _run() -> None:
        project, params = await _with_stdio(tmp_path)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                opened = await _open_mission(session, project)
                assert opened["ok"] is True

                denied = await _call(
                    session,
                    "hai_recontract",
                    {
                        "mission_id": opened["mission_id"],
                        "contract_version": opened["contract_version"],
                        "reason": "emergency",
                        "changes": {"objective": "changed emergency"},
                        "owner_ack": True,
                        "mode": "break_glass",
                        "break_glass_marker": 1,
                    },
                )
                assert denied["ok"] is False
                assert denied["error"] == "owner_gate_required"

                still_ok = await _authorize(
                    session,
                    opened,
                    contract_version=opened["contract_version"],
                    duration_minutes=30,
                )
                assert still_ok["ok"] is True

    asyncio.run(_run())


def test_stdio_declares_blocker_integer_not_literal_true(tmp_path: Path) -> None:
    """JSON declares_blocker: 1 must not classify as blocker without literal true."""

    async def _run() -> None:
        project, params = await _with_stdio(tmp_path)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                opened = await _open_mission(session, project)
                auth = await _authorize(
                    session,
                    opened,
                    contract_version=opened["contract_version"],
                    duration_minutes=30,
                )
                assert auth["ok"] is True

                with_int = await _call(
                    session,
                    "hai_check_activity",
                    {
                        "session_id": auth["session_id"],
                        "activity_step": "blocked on external dependency",
                        "criterion_id": "dc-1",
                        "declares_blocker": 1,
                    },
                )
                assert with_int.get("classification") != "blocker"

                with_true = await _call(
                    session,
                    "hai_check_activity",
                    {
                        "session_id": auth["session_id"],
                        "activity_step": "blocked on external dependency",
                        "criterion_id": "dc-1",
                        "declares_blocker": True,
                    },
                )
                assert with_true["classification"] == "blocker"

    asyncio.run(_run())


def test_stdio_contract_version_string_rejected(tmp_path: Path) -> None:
    """JSON contract_version: \"1\" must not authorize when literal int is required."""

    async def _run() -> None:
        project, params = await _with_stdio(tmp_path)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                opened = await _open_mission(session, project)
                denied = await _authorize(
                    session,
                    opened,
                    contract_version="1",
                    duration_minutes=30,
                )
                assert denied["ok"] is False
                assert denied["error"] == "invalid_args"

                ok = await _authorize(
                    session,
                    opened,
                    contract_version=opened["contract_version"],
                    duration_minutes=30,
                )
                assert ok["ok"] is True

    asyncio.run(_run())


def test_stdio_contract_version_bool_rejected(tmp_path: Path) -> None:
    """JSON contract_version: true must not coerce to version 1."""

    async def _run() -> None:
        project, params = await _with_stdio(tmp_path)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                opened = await _open_mission(session, project)
                denied = await _authorize(
                    session,
                    opened,
                    contract_version=True,
                    duration_minutes=30,
                )
                assert denied["ok"] is False
                assert denied["error"] == "invalid_args"

    asyncio.run(_run())


def test_stdio_duration_minutes_string_rejected(tmp_path: Path) -> None:
    """JSON duration_minutes: \"30\" must not authorize."""

    async def _run() -> None:
        project, params = await _with_stdio(tmp_path)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                opened = await _open_mission(session, project)
                denied = await _authorize(
                    session,
                    opened,
                    contract_version=opened["contract_version"],
                    duration_minutes="30",
                )
                assert denied["ok"] is False
                assert denied["error"] == "invalid_args"

    asyncio.run(_run())


def test_stdio_duration_minutes_float_rejected(tmp_path: Path) -> None:
    """JSON duration_minutes: 30.9 must not silently truncate."""

    async def _run() -> None:
        project, params = await _with_stdio(tmp_path)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                opened = await _open_mission(session, project)
                denied = await _authorize(
                    session,
                    opened,
                    contract_version=opened["contract_version"],
                    duration_minutes=30.9,
                )
                assert denied["ok"] is False
                assert denied["error"] == "invalid_args"

    asyncio.run(_run())


def test_stdio_time_limit_hours_string_rejected(tmp_path: Path) -> None:
    """JSON time_limit_hours: \"8\" must not be stored as a numeric limit."""

    async def _run() -> None:
        project, params = await _with_stdio(tmp_path)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                denied = await _call(
                    session,
                    "hai_mission_start",
                    {
                        "problem": "Probe time limit coercion",
                        "artifact": "probe artifact",
                        "done_criteria": [{"id": "dc-1", "description": "x"}],
                        "owner": "samuel",
                        "time_limit_hours": "8",
                    },
                )
                assert denied["ok"] is False
                assert denied["error"] == "invalid_args"

    asyncio.run(_run())


def test_stdio_max_parallel_sessions_string_rejected(tmp_path: Path) -> None:
    """JSON max_parallel_sessions: \"999\" via recontract must not widen parallel limit."""

    async def _run() -> None:
        project, params = await _with_stdio(tmp_path)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                opened = await _open_mission(session, project)
                new_constraints = dict(opened["contract"]["constraints"])
                new_constraints["max_parallel_sessions"] = "999"
                denied = await _call(
                    session,
                    "hai_recontract",
                    {
                        "mission_id": opened["mission_id"],
                        "contract_version": opened["contract_version"],
                        "reason": "widen parallel",
                        "changes": {"constraints": new_constraints},
                        "owner_ack": True,
                    },
                )
                assert denied["ok"] is False
                assert denied["error"] == "invalid_args"

    asyncio.run(_run())


def test_stdio_recontract_owner_ack_integer_fails_closed(tmp_path: Path) -> None:
    """JSON owner_ack: 1 on hai_recontract must not apply contract changes."""

    async def _run() -> None:
        project, params = await _with_stdio(tmp_path)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                opened = await _open_mission(session, project)
                new_constraints = dict(opened["contract"]["constraints"])
                new_constraints["allowed_paths"] = ["src/", "tests/", "docs/"]
                denied = await _call(
                    session,
                    "hai_recontract",
                    {
                        "mission_id": opened["mission_id"],
                        "contract_version": opened["contract_version"],
                        "reason": "expand paths",
                        "changes": {"constraints": new_constraints},
                        "owner_ack": 1,
                    },
                )
                assert denied["ok"] is False
                assert denied["error"] == "owner_gate_required"

    asyncio.run(_run())


def test_stdio_close_mission_owner_ack_integer_fails_closed(tmp_path: Path) -> None:
    """JSON owner_ack: 1 on hai_close_mission abandon must not close mission."""

    async def _run() -> None:
        project, params = await _with_stdio(tmp_path)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                opened = await _open_mission(session, project)
                denied = await _call(
                    session,
                    "hai_close_mission",
                    {
                        "mission_id": opened["mission_id"],
                        "contract_version": opened["contract_version"],
                        "closure": "abandoned",
                        "outcome_summary": "coercion probe",
                        "owner_ack": 1,
                    },
                )
                assert denied["ok"] is False
                assert denied["error"] == "owner_gate_required"

    asyncio.run(_run())

