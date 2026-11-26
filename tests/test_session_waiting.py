"""Issue #29: every session says what it waits for.

The point of the waiting state is that a human and an orchestrator can see
whether somebody has to act: an agent denied for capacity, a mission whose
contract moved on, or an owner decision (Touch ID / one-time code) that is
still outstanding. The field is visible in the tool answers and in the board.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from hai_mcp.config import Config
from hai_mcp.state import ControlPlane

CODE_RE = re.compile(r"HAI owner code: ([2-9A-HJKMNP-Z]{4}-[2-9A-HJKMNP-Z]{4})")


@pytest.fixture
def plane(tmp_path: Path) -> ControlPlane:
    # owner_home lives NEXT TO hai_home — never inside it.
    return ControlPlane(Config(hai_home=tmp_path / "hai_home", owner_home=tmp_path / "owner_home"))


@pytest.fixture
def project(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    p.mkdir()
    return p


def _open(plane: ControlPlane, project: Path, max_parallel: int = 1) -> dict[str, Any]:
    opened = plane.open_mission(
        objective="Make the waiting state of every session visible",
        artifact="docs/TOOL_CONTRACT.md with the waiting_for field",
        done_criteria=[{"id": "dc-1", "description": "the board shows what a session waits for"}],
        non_goals=["rewrite the server"],
        constraints={
            "project_path": str(project),
            "allowed_paths": ["src"],
            "capabilities": ["read", "write"],
            "max_parallel_sessions": max_parallel,
        },
        owner="samuel",
    )
    assert opened["ok"] is True, opened
    return opened


def _authorize(
    plane: ControlPlane,
    mission_id: str,
    agent: str = "codex-pane-1",
    version: int = 1,
) -> dict[str, Any]:
    return plane.authorize_session(
        mission_id=mission_id,
        contract_version=version,
        agent_identity=agent,
        role="builder",
        contribution="dc-1",
        expected_result="the board shows the waiting state",
        duration_minutes=30,
        capabilities=["read"],
        criterion_ids=["dc-1"],
    )


def _owner_code(plane: ControlPlane, pending: dict[str, Any]) -> str:
    path = plane.cfg.resolved_owner_home() / f"{pending['challenge_id']}.txt"
    match = CODE_RE.search(path.read_text(encoding="utf-8"))
    assert match, "owner file must carry the code"
    return match.group(1)


def _recontract(plane: ControlPlane, mission_id: str, version: int = 1, **kwargs: Any) -> dict[str, Any]:
    return plane.recontract(
        mission_id=mission_id,
        contract_version=version,
        reason="the scope moved",
        changes={"objective": "Make the waiting state of every session visible, precisely"},
        **kwargs,
    )


def test_granted_session_waits_for_nothing(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    granted = _authorize(plane, opened["mission_id"])
    assert granted["ok"] is True
    assert granted["waiting_for"] == "none"
    assert granted["session_lease"]["waiting_for"] == "none"

    board = plane.status()["mission_board"]
    assert board is not None
    assert board["session_count"] == 1
    assert board["sessions"][0]["waiting_for"] == "none"
    assert board["waiting_count"] == 0


def test_denied_session_waits_for_capacity_and_the_board_shows_it(
    plane: ControlPlane, project: Path
) -> None:
    opened = _open(plane, project, max_parallel=1)
    assert _authorize(plane, opened["mission_id"], agent="codex-pane-1")["ok"] is True

    denied = _authorize(plane, opened["mission_id"], agent="codex-pane-2")
    assert denied["ok"] is False
    assert denied["error"] == "parallel_session_denied"
    assert denied["waiting_for"] == "capacity"
    assert denied["waiting_since"]

    board = plane.status()["mission_board"]
    assert board["waiting_count"] == 1
    waiter = board["waiting"][0]
    assert waiter["agent_identity"] == "codex-pane-2"
    assert waiter["waiting_for"] == "capacity"
    assert waiter["reason"]
    # the granted lease itself is not waiting for anything
    assert board["sessions"][0]["waiting_for"] == "none"


def test_capacity_waiter_is_cleared_once_a_lease_is_granted(
    plane: ControlPlane, project: Path
) -> None:
    opened = _open(plane, project, max_parallel=1)
    mission_id = opened["mission_id"]
    assert _authorize(plane, mission_id, agent="codex-pane-1")["ok"] is True
    assert _authorize(plane, mission_id, agent="codex-pane-2")["waiting_for"] == "capacity"

    # the first lease ends — the slot is free again
    plane.mission.revoke_all_sessions(mission_id, reason="day_stopped")
    granted = _authorize(plane, mission_id, agent="codex-pane-2")
    assert granted["ok"] is True
    assert granted["waiting_for"] == "none"

    board = plane.status()["mission_board"]
    assert board["waiting_count"] == 0


def test_stale_contract_request_waits_for_contract(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    denied = _authorize(plane, opened["mission_id"], agent="codex-pane-2", version=7)
    assert denied["ok"] is False
    assert denied["error"] == "contract_version_mismatch"
    assert denied["waiting_for"] == "contract"

    board = plane.status()["mission_board"]
    assert board["waiting_count"] == 1
    assert board["waiting"][0]["waiting_for"] == "contract"


def test_pending_owner_decision_marks_the_session_waiting_for_owner_touchid(
    plane: ControlPlane, project: Path
) -> None:
    opened = _open(plane, project)
    granted = _authorize(plane, opened["mission_id"])
    assert granted["ok"] is True

    pending = _recontract(plane, opened["mission_id"])
    assert pending["ok"] is False
    assert pending["error"] == "owner_gate_required"

    board = plane.status()["mission_board"]
    assert board["sessions"][0]["waiting_for"] == "owner_touchid"
    assert board["owner_pending"][0]["action"] == "recontract"

    contract = plane.get_contract(granted["session_id"])
    assert contract["ok"] is True
    assert contract["waiting_for"] == "owner_touchid"

    activity = plane.check_activity(
        session_id=granted["session_id"],
        activity_step="write the board",
        criterion_id="dc-1",
    )
    assert activity["waiting_for"] == "owner_touchid"


def test_applied_recontract_leaves_the_old_lease_waiting_for_contract(
    plane: ControlPlane, project: Path
) -> None:
    opened = _open(plane, project)
    granted = _authorize(plane, opened["mission_id"])
    assert granted["ok"] is True

    pending = _recontract(plane, opened["mission_id"])
    code = _owner_code(plane, pending)
    applied = _recontract(plane, opened["mission_id"], owner_code=code)
    assert applied["ok"] is True, applied

    board = plane.status()["mission_board"]
    session = board["sessions"][0]
    assert session["status"] == "revoked"
    assert session["waiting_for"] == "contract"
    assert board["owner_pending"] == []

    contract = plane.get_contract(granted["session_id"])
    assert contract["ok"] is False
    assert contract["error"] == "contract_version_mismatch"
    assert contract["waiting_for"] == "contract"


def test_board_is_none_without_an_active_mission(plane: ControlPlane) -> None:
    assert plane.status()["mission_board"] is None
