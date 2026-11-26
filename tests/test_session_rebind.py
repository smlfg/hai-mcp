"""Contract update -> session rebind: the named step out of a stale lease.

A recontract bumps the contract version and revokes the leases bound to the old version.
The agent must be able to see that state and to name the exact call that moves it forward:
`hai_rebind_session` (same lease, same deadline, narrowed scope) or `hai_authorize_session`
(new lease, new deadline).
"""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from hai_mcp.config import Config
from hai_mcp.server import mcp
from hai_mcp.state import ControlPlane


@pytest.fixture
def plane(tmp_path: Path) -> ControlPlane:
    home = tmp_path / "hai_home"
    # The rebind path is orthogonal to the owner gate; the legacy gate keeps the test
    # independent of the nonce channel.
    return ControlPlane(Config(hai_home=home, owner_gate="ack_legacy"))


@pytest.fixture
def project(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    (p / "src").mkdir(parents=True)
    (p / "tests").mkdir(parents=True)
    return p


def _open(plane: ControlPlane, project: Path, **overrides) -> dict:
    constraints = {
        "project_path": str(project),
        "allowed_paths": ["src/", "tests/"],
        "capabilities": ["read", "write", "test"],
        "max_parallel_sessions": 4,
    }
    constraints.update(overrides.pop("constraints", {}))
    return plane.open_mission(
        objective="Implement drift classifier tests",
        artifact="tests/out.md with green pytest",
        done_criteria=[
            {"id": "dc-1", "description": "mission lifecycle tests exist and pass"},
            {"id": "dc-2", "description": "contract hash is stable"},
        ],
        non_goals=["rewrite entire architecture"],
        constraints=constraints,
        owner="samuel",
        **overrides,
    )


def _authorize(plane: ControlPlane, opened: dict, criterion_ids: list[str] | None = None) -> dict:
    return plane.authorize_session(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        agent_identity="codex-pane-1",
        role="builder",
        contribution="implement the classifier",
        expected_result="green pytest",
        duration_minutes=30,
        criterion_ids=criterion_ids if criterion_ids is not None else ["dc-1", "dc-2"],
    )


def _recontract(plane: ControlPlane, opened: dict, changes: dict, reason: str = "contract update") -> dict:
    r = plane.recontract(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        reason=reason,
        changes=changes,
        owner_ack=True,
    )
    assert r["ok"] is True, r
    return r


def test_rebind_session_is_exposed_as_a_tool() -> None:
    async def _run() -> None:
        tools = await mcp.list_tools()
        names = {tool.name for tool in tools}
        assert "hai_rebind_session" in names

    anyio.run(_run)


def test_stale_lease_error_names_the_rebind_step(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    auth = _authorize(plane, opened)
    rec = _recontract(plane, opened, {"objective": "Implement only the classifier unit tests"})

    got = plane.get_contract(auth["session_id"])
    assert got["ok"] is False
    assert got["error"] == "contract_version_mismatch"
    assert got["session_contract_version"] == 1
    assert got["current_version"] == rec["contract_version"] == 2
    assert got["rebindable"] is True
    assert got["required_action"] == "rebind_session"
    assert "hai_rebind_session" in got["next_action"]
    assert auth["session_id"] in got["next_action"]


def test_check_activity_reports_the_rebind_step(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    auth = _authorize(plane, opened)
    _recontract(plane, opened, {"objective": "Implement only the classifier unit tests"})

    r = plane.check_activity(
        session_id=auth["session_id"],
        activity_step="edit the classifier",
        criterion_id="dc-1",
        affected_paths=[str(project / "src" / "classifier.py")],
    )
    assert r["ok"] is False
    assert r["error"] == "contract_version_mismatch"
    assert r["required_action"] == "rebind_session"
    assert "hai_rebind_session" in r["next_action"]


def test_recontract_lists_the_sessions_that_can_be_rebound(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    auth = _authorize(plane, opened)
    rec = _recontract(plane, opened, {"objective": "Implement only the classifier unit tests"})

    migration = rec["session_migration"]
    assert migration["revoked_sessions"] == [auth["session_id"]]
    assert migration["rebindable_sessions"] == [auth["session_id"]]
    assert migration["not_rebindable_sessions"] == []
    assert "hai_rebind_session" in migration["next_action"]


def test_rebind_moves_the_lease_to_the_current_version(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    auth = _authorize(plane, opened)
    lease_before = auth["session_lease"]
    rec = _recontract(plane, opened, {"objective": "Implement only the classifier unit tests"})

    r = plane.rebind_session(
        mission_id=opened["mission_id"],
        contract_version=rec["contract_version"],
        session_id=auth["session_id"],
    )
    assert r["ok"] is True
    assert r["status"] == "rebound"
    # Same lease, same deadline, same identity: only the contract binding moves.
    assert r["session_id"] == auth["session_id"]
    assert r["session_lease"]["contract_version"] == rec["contract_version"]
    assert r["session_lease"]["agent_identity"] == lease_before["agent_identity"]
    assert r["session_lease"]["expires_at"] == lease_before["expires_at"]
    assert r["session_lease"]["status"] == "active"

    got = plane.get_contract(auth["session_id"])
    assert got["ok"] is True
    assert got["contract_version"] == rec["contract_version"]


def test_rebind_writes_an_audit_event(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    auth = _authorize(plane, opened)
    rec = _recontract(plane, opened, {"objective": "Implement only the classifier unit tests"})

    r = plane.rebind_session(
        mission_id=opened["mission_id"],
        contract_version=rec["contract_version"],
        session_id=auth["session_id"],
    )
    audit_id = r["audit_event_id"]
    assert audit_id
    events = list((plane.mission.audit_dir).glob("*.json"))
    assert any(auth["session_id"] in path.read_text(encoding="utf-8") for path in events)


def test_rebind_is_idempotent(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    auth = _authorize(plane, opened)
    rec = _recontract(plane, opened, {"objective": "Implement only the classifier unit tests"})

    first = plane.rebind_session(
        mission_id=opened["mission_id"],
        contract_version=rec["contract_version"],
        session_id=auth["session_id"],
    )
    second = plane.rebind_session(
        mission_id=opened["mission_id"],
        contract_version=rec["contract_version"],
        session_id=auth["session_id"],
    )
    assert first["ok"] is True
    assert second["ok"] is True
    assert second["status"] == "already_bound"


def test_rebind_refuses_when_a_criterion_was_removed(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    auth = _authorize(plane, opened)
    rec = _recontract(
        plane,
        opened,
        {"done_criteria": [{"id": "dc-1", "description": "mission lifecycle tests exist and pass"}]},
    )

    r = plane.rebind_session(
        mission_id=opened["mission_id"],
        contract_version=rec["contract_version"],
        session_id=auth["session_id"],
    )
    assert r["ok"] is False
    assert r["error"] == "session_not_rebindable"
    assert r["detail"] == "criteria_removed"
    assert "dc-2" in r["message"]
    # The named fallback is a fresh lease on the current version.
    assert "hai_authorize_session" in r["next_action"]
    assert r["rebindable"] is False


def test_rebind_with_narrowed_criteria_succeeds(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    auth = _authorize(plane, opened)
    rec = _recontract(plane, opened, {"objective": "Implement only the classifier unit tests"})

    r = plane.rebind_session(
        mission_id=opened["mission_id"],
        contract_version=rec["contract_version"],
        session_id=auth["session_id"],
        criterion_ids=["dc-1"],
    )
    assert r["ok"] is True
    assert r["criterion_ids"] == ["dc-1"]
    assert r["session_lease"]["criterion_ids"] == ["dc-1"]


def test_rebind_refuses_criteria_outside_the_original_set(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    auth = _authorize(plane, opened, criterion_ids=["dc-1"])
    rec = _recontract(plane, opened, {"objective": "Implement only the classifier unit tests"})

    r = plane.rebind_session(
        mission_id=opened["mission_id"],
        contract_version=rec["contract_version"],
        session_id=auth["session_id"],
        criterion_ids=["dc-2"],
    )
    assert r["ok"] is False
    assert r["error"] == "invalid_args"
    assert "dc-2" in r["message"]


def test_rebind_narrows_removed_capabilities(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    auth = _authorize(plane, opened)
    rec = _recontract(
        plane,
        opened,
        {"constraints": {"capabilities": ["read"]}},
    )

    r = plane.rebind_session(
        mission_id=opened["mission_id"],
        contract_version=rec["contract_version"],
        session_id=auth["session_id"],
    )
    assert r["ok"] is True
    assert r["session_lease"]["capabilities"] == ["read"]
    assert sorted(r["dropped_capabilities"]) == ["test", "write"]


def test_rebind_refuses_when_no_capability_survives(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    auth = _authorize(plane, opened)
    rec = _recontract(plane, opened, {"constraints": {"capabilities": []}})

    r = plane.rebind_session(
        mission_id=opened["mission_id"],
        contract_version=rec["contract_version"],
        session_id=auth["session_id"],
    )
    assert r["ok"] is False
    assert r["error"] == "session_not_rebindable"
    assert r["detail"] == "capabilities_removed"


def test_rebind_refuses_a_lease_revoked_for_another_reason(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    auth = _authorize(plane, opened)
    plane.mission.revoke_all_sessions(opened["mission_id"], reason="day_stopped")

    r = plane.rebind_session(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        session_id=auth["session_id"],
    )
    assert r["ok"] is False
    assert r["error"] == "session_not_rebindable"
    assert r["detail"] == "lease_revoked"
    assert "hai_authorize_session" in r["next_action"]


def test_rebind_refuses_an_expired_lease(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    auth = _authorize(plane, opened)
    rec = _recontract(plane, opened, {"objective": "Implement only the classifier unit tests"})

    session = plane.mission.get_session(auth["session_id"])
    assert session is not None
    session["expires_at"] = "2020-01-01T00:00:00Z"
    plane.mission.save_session(session)

    r = plane.rebind_session(
        mission_id=opened["mission_id"],
        contract_version=rec["contract_version"],
        session_id=auth["session_id"],
    )
    assert r["ok"] is False
    assert r["error"] == "lease_expired"


def test_rebind_refuses_a_stale_target_version(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    auth = _authorize(plane, opened)
    _recontract(plane, opened, {"objective": "Implement only the classifier unit tests"})

    r = plane.rebind_session(
        mission_id=opened["mission_id"],
        contract_version=1,
        session_id=auth["session_id"],
    )
    assert r["ok"] is False
    assert r["error"] == "contract_version_mismatch"
    assert r["current_version"] == 2


def test_rebind_refuses_when_the_parallel_capacity_is_full(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project, constraints={"max_parallel_sessions": 1})
    auth = _authorize(plane, opened)
    rec = _recontract(plane, opened, {"objective": "Implement only the classifier unit tests"})
    fresh = plane.authorize_session(
        mission_id=opened["mission_id"],
        contract_version=rec["contract_version"],
        agent_identity="codex-pane-2",
        role="builder",
        contribution="implement the classifier",
        expected_result="green pytest",
        duration_minutes=30,
        criterion_ids=["dc-1"],
    )
    assert fresh["ok"] is True
    fresh_lease = plane.mission.get_session(fresh["session_id"])
    assert fresh_lease is not None
    fresh_lease["contract_version"] = rec["contract_version"]
    fresh_lease["contract_hash"] = plane.mission.current_contract(opened["mission_id"])["contract_hash"]
    plane.mission.save_session(fresh_lease)

    r = plane.rebind_session(
        mission_id=opened["mission_id"],
        contract_version=rec["contract_version"],
        session_id=auth["session_id"],
    )
    assert r["ok"] is False
    assert r["error"] == "parallel_session_denied"


def test_rebind_refuses_an_unknown_session(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    r = plane.rebind_session(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        session_id="S-does-not-exist",
    )
    assert r["ok"] is False
    assert r["error"] == "invalid_args"


def test_authorize_session_names_the_current_version(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    rec = _recontract(plane, opened, {"objective": "Implement only the classifier unit tests"})

    r = plane.authorize_session(
        mission_id=opened["mission_id"],
        contract_version=1,
        agent_identity="codex-pane-1",
        role="builder",
        contribution="implement the classifier",
        expected_result="green pytest",
        duration_minutes=30,
        criterion_ids=["dc-1"],
    )
    assert r["ok"] is False
    assert r["error"] == "contract_version_mismatch"
    assert r["current_version"] == rec["contract_version"] == 2
    assert "hai_authorize_session" in r["next_action"]
