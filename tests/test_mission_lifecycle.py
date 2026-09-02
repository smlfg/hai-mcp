from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from hai_mcp.config import Config
from hai_mcp.ids import IdentifierError
from hai_mcp.mission import MissionEngine, contract_hash
from hai_mcp.state import ControlPlane


@pytest.fixture
def plane(tmp_path: Path) -> ControlPlane:
    home = tmp_path / "hai_home"
    # These tests exercise the legacy honor-system gate explicitly; the default gate is
    # 'nonce' and is covered by tests/test_owner_nonce_gate.py.
    return ControlPlane(Config(hai_home=home, owner_gate="ack_legacy"))


@pytest.fixture
def project(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    (p / "src").mkdir(parents=True)
    (p / "tests").mkdir(parents=True)
    return p


def _valid_contract(project: Path) -> dict:
    return {
        "objective": "Implement drift classifier tests",
        "artifact": "tests/test_mission_lifecycle.py with green pytest",
        "done_criteria": [
            {"id": "dc-1", "description": "mission lifecycle tests exist and pass"},
            {"id": "dc-2", "description": "contract hash is stable"},
        ],
        "non_goals": ["rewrite entire architecture", "deploy to production"],
        "constraints": {
            "project_path": str(project),
            "allowed_paths": ["src/", "tests/"],
            "capabilities": ["read", "write", "test"],
            "max_parallel_sessions": 1,
        },
        "owner": "samuel",
    }


def _open(plane: ControlPlane, project: Path) -> dict:
    c = _valid_contract(project)
    return plane.open_mission(
        objective=c["objective"],
        artifact=c["artifact"],
        done_criteria=c["done_criteria"],
        non_goals=c["non_goals"],
        constraints=c["constraints"],
        owner=c["owner"],
    )


def test_open_mission_happy_path(plane: ControlPlane, project: Path) -> None:
    r = _open(plane, project)
    assert r["ok"] is True
    assert r["status"] == "active"
    assert r["contract_version"] == 1
    assert r["contract_hash"].startswith("sha256:")
    assert r["contract"]["objective"] == _valid_contract(project)["objective"]
    assert plane.mission.load_active_pointer()["mission_id"] == r["mission_id"]


def test_open_mission_review_required_missing_artifact(plane: ControlPlane, project: Path) -> None:
    r = plane.open_mission(
        objective="Do something",
        artifact="",
        done_criteria=[{"id": "dc-1", "description": "x"}],
        non_goals=[],
        constraints={"project_path": str(project)},
        owner="samuel",
    )
    assert r["ok"] is False
    assert r["error"] == "review_required"
    assert r["status"] == "review_required"
    assert "artifact is required" in r["issues"]
    assert plane.mission.load_active_pointer()["mission_id"] is None


def test_open_mission_review_required_vague_objective(plane: ControlPlane, project: Path) -> None:
    r = plane.open_mission(
        objective="an HAI arbeiten",
        artifact="some/file.py",
        done_criteria=[{"id": "dc-1", "description": "x"}],
        non_goals=[],
        constraints={"project_path": str(project)},
        owner="samuel",
    )
    assert r["ok"] is False
    assert r["error"] == "review_required"
    assert r["status"] == "review_required"
    assert any("too broad" in i for i in r["issues"])


def test_second_active_mission_denied(plane: ControlPlane, project: Path) -> None:
    first = _open(plane, project)
    second = _open(plane, project)
    assert second["ok"] is False
    assert second["error"] == "active_mission_exists"
    assert second["active_mission_id"] == first["mission_id"]


def test_contract_hash_stable(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    contract = opened["contract"]
    body = {k: v for k, v in contract.items() if k != "contract_hash"}
    assert contract_hash(body) == opened["contract_hash"]


def test_authorize_and_get_contract_exact(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    auth = plane.authorize_session(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        agent_identity="composer",
        role="builder",
        contribution="write lifecycle tests",
        expected_result="pytest green",
        duration_minutes=45,
        criterion_ids=["dc-1"],
        capabilities=["read", "write", "test"],
    )
    assert auth["ok"] is True
    assert auth["status"] == "granted"
    session_id = auth["session_id"]

    got = plane.get_contract(session_id)
    assert got["ok"] is True
    assert got["contract"] == opened["contract"]
    assert got["contract_hash"] == opened["contract_hash"]
    assert got["remaining_seconds"] > 0


def test_authorize_wrong_version_denied(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    r = plane.authorize_session(
        mission_id=opened["mission_id"],
        contract_version=99,
        agent_identity="composer",
        role="builder",
        contribution="x",
        expected_result="y",
        duration_minutes=10,
        criterion_ids=["dc-1"],
    )
    assert r["ok"] is False
    assert r["error"] == "contract_version_mismatch"


def test_parallel_session_denied(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    a = plane.authorize_session(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        agent_identity="a",
        role="builder",
        contribution="first",
        expected_result="x",
        duration_minutes=30,
        criterion_ids=["dc-1"],
    )
    b = plane.authorize_session(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        agent_identity="b",
        role="builder",
        contribution="second",
        expected_result="y",
        duration_minutes=30,
        criterion_ids=["dc-1"],
    )
    assert a["ok"] is True
    assert b["ok"] is False
    assert b["error"] == "parallel_session_denied"


def test_expired_lease_cannot_get_contract(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    auth = plane.authorize_session(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        agent_identity="composer",
        role="builder",
        contribution="x",
        expected_result="y",
        duration_minutes=1,
        criterion_ids=["dc-1"],
    )
    session = plane.mission.get_session(auth["session_id"])
    assert session is not None
    session["expires_at"] = "2000-01-01T00:00:00Z"
    plane.mission.save_session(session)

    got = plane.get_contract(auth["session_id"])
    assert got["ok"] is False
    assert got["error"] == "lease_expired"
    assert got["required_action"] == "pause"


def test_check_activity_in_scope(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    auth = plane.authorize_session(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        agent_identity="composer",
        role="builder",
        contribution="tests",
        expected_result="green",
        duration_minutes=30,
        criterion_ids=["dc-1"],
    )
    r = plane.check_activity(
        session_id=auth["session_id"],
        activity_step="add lifecycle test cases",
        criterion_id="dc-1",
        affected_paths=[str(project / "tests" / "test_mission_lifecycle.py")],
    )
    assert r["classification"] == "in_scope"
    assert r["required_action"] == "continue"


def test_check_activity_blocker(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    auth = plane.authorize_session(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        agent_identity="composer",
        role="builder",
        contribution="tests",
        expected_result="green",
        duration_minutes=30,
        criterion_ids=["dc-1"],
    )
    r = plane.check_activity(
        session_id=auth["session_id"],
        activity_step="missing dependency blocks tests",
        criterion_id="dc-1",
        declares_blocker=True,
    )
    assert r["classification"] == "blocker"
    assert r["required_action"] == "pause"


def test_check_activity_park_candidate(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    auth = plane.authorize_session(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        agent_identity="composer",
        role="builder",
        contribution="tests",
        expected_result="green",
        duration_minutes=30,
        criterion_ids=["dc-1"],
    )
    r = plane.check_activity(
        session_id=auth["session_id"],
        activity_step="rewrite entire architecture for fun",
        criterion_id="dc-1",
    )
    assert r["classification"] == "park_candidate"
    assert r["required_action"] == "park"


def test_check_activity_drift_path(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    auth = plane.authorize_session(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        agent_identity="composer",
        role="builder",
        contribution="tests",
        expected_result="green",
        duration_minutes=30,
        criterion_ids=["dc-1"],
    )
    r = plane.check_activity(
        session_id=auth["session_id"],
        activity_step="edit docs",
        criterion_id="dc-1",
        affected_paths=[str(project / "outside" / "secret.txt")],
    )
    assert r["classification"] == "drift"
    assert r["required_action"] == "stop"


def test_check_activity_drift_sensitive_trace(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    auth = plane.authorize_session(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        agent_identity="composer",
        role="builder",
        contribution="tests",
        expected_result="green",
        duration_minutes=30,
        criterion_ids=["dc-1"],
        capabilities=["read", "write", "test"],
    )
    r = plane.check_activity(
        session_id=auth["session_id"],
        activity_step="commit changes",
        criterion_id="dc-1",
        trace_events=[{"action": "commit"}],
    )
    assert r["classification"] == "drift"
    assert r["required_action"] == "stop"


def test_check_activity_unclear_missing_criterion(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    auth = plane.authorize_session(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        agent_identity="composer",
        role="builder",
        contribution="tests",
        expected_result="green",
        duration_minutes=30,
        criterion_ids=["dc-1"],
    )
    r = plane.check_activity(
        session_id=auth["session_id"],
        activity_step="do something",
        criterion_id=None,
    )
    assert r["classification"] == "unclear"
    assert r["required_action"] == "pause"


def test_park_item_mission_linked(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    auth = plane.authorize_session(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        agent_identity="composer",
        role="builder",
        contribution="tests",
        expected_result="green",
        duration_minutes=30,
        criterion_ids=["dc-1"],
    )
    before_contract = plane.mission.current_contract(opened["mission_id"])
    r = plane.park_item(
        idea="maybe add kubernetes later",
        origin_session_id=auth["session_id"],
        trigger_event="check_activity park_candidate",
        mission_id=opened["mission_id"],
        rationale="out of current mission scope",
    )
    assert r["ok"] is True
    assert r["status"] == "parked"
    assert r["record"]["actionable"] is False
    after_contract = plane.mission.current_contract(opened["mission_id"])
    assert after_contract == before_contract


def test_recontract_denied_without_owner_ack(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    r = plane.recontract(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        reason="allow docs path",
        changes={"constraints": {**opened["contract"]["constraints"], "allowed_paths": ["src/", "tests/", "docs/"]}},
        owner_ack=False,
    )
    assert r["ok"] is False
    assert r["error"] == "owner_gate_required"
    assert r["status"] == "pending_owner_confirmation"
    assert r["owner_ack_required"] is True
    assert r["diff"]
    # denial must not create a new contract version
    assert plane.mission.load_contract(opened["mission_id"], 2) is None


def test_recontract_approval_diff_version_revokes_leases(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    auth = plane.authorize_session(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        agent_identity="composer",
        role="builder",
        contribution="tests",
        expected_result="green",
        duration_minutes=30,
        criterion_ids=["dc-1"],
    )
    new_constraints = dict(opened["contract"]["constraints"])
    new_constraints["allowed_paths"] = ["src/", "tests/", "docs/"]
    r = plane.recontract(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        reason="need docs",
        changes={"constraints": new_constraints},
        owner_ack=True,
    )
    assert r["ok"] is True
    assert r["status"] == "approved"
    assert r["contract_version"] == 2
    assert r["diff"]
    assert auth["session_id"] in r["revoked_sessions"]

    old_contract = plane.mission.load_contract(opened["mission_id"], 1)
    new_contract = plane.mission.load_contract(opened["mission_id"], 2)
    assert old_contract is not None and new_contract is not None
    assert old_contract["contract_version"] == 1
    assert new_contract["contract_version"] == 2

    got = plane.get_contract(auth["session_id"])
    assert got["ok"] is False


def test_recontract_break_glass_friction(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    denied = plane.recontract(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        reason="emergency",
        changes={"objective": "changed"},
        owner_ack=True,
        mode="break_glass",
        break_glass_marker=False,
    )
    assert denied["ok"] is False
    assert denied["error"] == "owner_gate_required"

    ok = plane.recontract(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        reason="emergency",
        changes={"objective": "changed emergency"},
        owner_ack=True,
        mode="break_glass",
        break_glass_marker=True,
    )
    assert ok["ok"] is True
    assert ok["audit_classification"] == "break_glass"


def test_close_mission_incomplete_proof(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    r = plane.close_mission(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        evidence={"dc-1": {"path": str(project / "tests" / "x.py")}},
        outcome_summary="done",
        closure="completed",
    )
    assert r["status"] == "incomplete"
    assert "dc-2" in r["missing_criteria"]


def test_close_mission_path_traversal_rejected(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    outside = project.parent / "outside.txt"
    outside.write_text("nope", encoding="utf-8")
    r = plane.close_mission(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        evidence={
            "dc-1": {"path": str(project / "tests" / "ok.py")},
            "dc-2": {"path": str(outside)},
        },
        outcome_summary="done",
        closure="completed",
    )
    assert r["status"] == "incomplete"
    assert r["invalid_evidence"]


def test_close_mission_symlink_escape_rejected(plane: ControlPlane, project: Path, tmp_path: Path) -> None:
    opened = _open(plane, project)
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    link = project / "tests" / "escape.link"
    link.symlink_to(outside)
    ev1 = project / "tests" / "proof1.txt"
    ev1.write_text("ok", encoding="utf-8")
    ev2 = project / "tests" / "proof2.txt"
    ev2.write_text("ok", encoding="utf-8")
    r = plane.close_mission(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        evidence={
            "dc-1": {"path": str(ev1)},
            "dc-2": {"path": str(link)},
        },
        outcome_summary="done",
        closure="completed",
    )
    assert r["status"] == "incomplete"
    assert any(
        "symlink" in str(item.get("message", "")) or item.get("error") == "path_outside_root"
        for item in r["invalid_evidence"]
    )


def test_close_mission_completed_with_hashes(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    ev1 = project / "tests" / "proof1.txt"
    ev2 = project / "tests" / "proof2.txt"
    ev1.write_text("proof one", encoding="utf-8")
    ev2.write_text("proof two", encoding="utf-8")
    r = plane.close_mission(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        evidence={"dc-1": {"path": str(ev1)}, "dc-2": {"path": str(ev2)}},
        outcome_summary="lifecycle tests implemented",
        closure="completed",
    )
    assert r["ok"] is True
    assert r["status"] == "completed"
    assert r["verified_criteria"]["dc-1"]["sha256"].startswith("sha256:")
    assert r["next_mission"] is None
    assert plane.mission.load_active_pointer()["mission_id"] is None


def test_close_mission_abandoned_gate(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    auth = plane.authorize_session(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        agent_identity="composer",
        role="builder",
        contribution="tests",
        expected_result="green",
        duration_minutes=30,
        criterion_ids=["dc-1"],
    )
    denied = plane.close_mission(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        evidence={},
        outcome_summary="no longer needed",
        closure="abandoned",
        owner_ack=False,
    )
    assert denied["error"] == "owner_gate_required"

    ok = plane.close_mission(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        evidence={},
        outcome_summary="no longer needed",
        closure="abandoned",
        owner_ack=True,
    )
    assert ok["status"] == "abandoned"
    assert auth["session_id"] in ok["revoked_sessions"]
    assert plane.mission.load_active_pointer()["mission_id"] is None


def test_audit_entries_unique_immutable(plane: ControlPlane, project: Path) -> None:
    before = set(plane.mission.list_audit_ids())
    _open(plane, project)
    after = set(plane.mission.list_audit_ids())
    new_ids = after - before
    assert len(new_ids) >= 1
    audit_dir = plane.mission.audit_dir
    paths = sorted(audit_dir.glob("A-*.json"))
    assert len(paths) == len({p.name for p in paths})


def test_full_happy_lifecycle(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    auth = plane.authorize_session(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        agent_identity="composer",
        role="builder",
        contribution="implement and verify tests",
        expected_result="pytest green",
        duration_minutes=60,
        criterion_ids=["dc-1", "dc-2"],
    )
    assert plane.get_contract(auth["session_id"])["ok"] is True
    assert plane.check_activity(
        session_id=auth["session_id"],
        activity_step="write tests",
        criterion_id="dc-1",
        affected_paths=[str(project / "tests" / "lifecycle.py")],
    )["required_action"] == "continue"

    ev1 = project / "tests" / "lifecycle.py"
    ev2 = project / "tests" / "hash.txt"
    ev1.write_text("tests", encoding="utf-8")
    ev2.write_text("hash", encoding="utf-8")
    closed = plane.close_mission(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        evidence={"dc-1": {"path": str(ev1)}, "dc-2": {"path": str(ev2)}},
        outcome_summary="all criteria verified",
        closure="completed",
    )
    assert closed["status"] == "completed"


def test_existing_owner_gates_regression(plane: ControlPlane, project: Path) -> None:
    denied = plane.accept_next_step(str(project), owner_ack=False, reason="nope")
    assert denied["error"] == "owner_gate_required"
    plane.propose_next_step(str(project), "# step\n")
    ok = plane.accept_next_step(str(project), owner_ack=True, reason="approved")
    assert ok["ok"] is True


def test_public_tool_registration() -> None:
    import importlib

    import hai_mcp.server as server

    importlib.reload(server)

    core_tools = {
        "hai_open_mission",
        "hai_authorize_session",
        "hai_get_contract",
        "hai_check_activity",
        "hai_park_item",
        "hai_recontract",
        "hai_close_mission",
    }
    assert hasattr(server.mcp, "_tool_manager")
    registered = {tool.name for tool in server.mcp._tool_manager.list_tools()}
    assert core_tools.issubset(registered)


def test_server_import_does_not_touch_live_hai_home(tmp_path: Path) -> None:
    import importlib
    import os

    live_home = Path.home() / ".hai"
    marker = live_home / "ACTIVE_CONTEXT.json"
    before_mtime = marker.stat().st_mtime if marker.is_file() else None

    isolated = tmp_path / "import-isolation-home"
    os.environ["HAI_HOME"] = str(isolated)
    import hai_mcp.server as server

    importlib.reload(server)
    server.get_control_plane()

    assert not isolated.exists() or isolated.resolve() != live_home.resolve()
    if before_mtime is not None:
        assert marker.stat().st_mtime == before_mtime


def test_malformed_mission_id_rejected(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    # A syntactically invalid id must fail closed, never be masked as not-found (None).
    with pytest.raises(IdentifierError) as exc:
        plane.mission.load_mission_meta("../../escape")
    assert exc.value.code == "invalid_args"
    assert exc.value.field == "mission_id"
    r = plane.authorize_session(
        mission_id="../../escape",
        contract_version=1,
        agent_identity="a",
        role="builder",
        contribution="x",
        expected_result="y",
        duration_minutes=10,
        criterion_ids=["dc-1"],
    )
    assert r["ok"] is False
    assert r["error"] == "invalid_args"
    r2 = plane.close_mission(
        mission_id="../missions/other",
        contract_version=opened["contract_version"],
        evidence={},
        outcome_summary="x",
        closure="abandoned",
        owner_ack=True,
    )
    assert r2["ok"] is False
    assert r2["error"] == "invalid_args"


def test_malformed_session_id_rejected(plane: ControlPlane, project: Path) -> None:
    _open(plane, project)
    got = plane.get_contract("../../sessions/evil")
    assert got["ok"] is False
    assert got["error"] == "invalid_args"


def test_duplicate_done_criteria_rejected(plane: ControlPlane, project: Path) -> None:
    r = plane.open_mission(
        objective="Test duplicate criteria",
        artifact="tests/file.py",
        done_criteria=[
            {"id": "dc-1", "description": "first"},
            {"id": "dc-1", "description": "duplicate"},
        ],
        non_goals=[],
        constraints={"project_path": str(project)},
        owner="samuel",
    )
    assert r["ok"] is False
    assert r["error"] == "review_required"
    assert r["status"] == "review_required"
    assert any("duplicate" in i for i in r["issues"])


def test_authorize_requires_identity_fields(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    base = dict(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        contribution="work",
        duration_minutes=30,
        criterion_ids=["dc-1"],
    )
    for missing in ("agent_identity", "role", "expected_result"):
        kwargs = {
            "agent_identity": "composer",
            "role": "builder",
            "expected_result": "done",
            **base,
        }
        kwargs[missing] = ""
        r = plane.authorize_session(**kwargs)
        assert r["ok"] is False
        assert r["error"] == "invalid_args"


def test_authorize_invalid_duration_type(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    r = plane.authorize_session(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        agent_identity="composer",
        role="builder",
        contribution="work",
        expected_result="done",
        duration_minutes="not-a-number",  # type: ignore[arg-type]
        criterion_ids=["dc-1"],
    )
    assert r["ok"] is False
    assert r["error"] == "invalid_args"


def test_check_activity_blocker_does_not_hide_drift(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    auth = plane.authorize_session(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        agent_identity="composer",
        role="builder",
        contribution="tests",
        expected_result="green",
        duration_minutes=30,
        criterion_ids=["dc-1"],
    )
    r = plane.check_activity(
        session_id=auth["session_id"],
        activity_step="blocked but outside path",
        criterion_id="dc-1",
        affected_paths=[str(project / "outside" / "secret.txt")],
        declares_blocker=True,
    )
    assert r["classification"] == "drift"
    assert r["required_action"] == "stop"


def test_stale_lease_after_recontract(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    auth = plane.authorize_session(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        agent_identity="composer",
        role="builder",
        contribution="tests",
        expected_result="green",
        duration_minutes=30,
        criterion_ids=["dc-1"],
    )
    new_constraints = dict(opened["contract"]["constraints"])
    new_constraints["allowed_paths"] = ["src/", "tests/", "docs/"]
    plane.recontract(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        reason="expand paths",
        changes={"constraints": new_constraints},
        owner_ack=True,
    )
    got = plane.get_contract(auth["session_id"])
    assert got["ok"] is False
    assert got["error"] == "contract_version_mismatch"


def test_recontract_rejects_system_managed_field(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    r = plane.recontract(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        reason="sneaky",
        changes={"mission_id": "M-HIDDEN"},
        owner_ack=False,
    )
    assert r["ok"] is False
    assert r["error"] == "invalid_args"
    assert "system-managed" in r["message"]


def test_recontract_rejects_empty_diff(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    r = plane.recontract(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        reason="noop",
        changes={},
        owner_ack=False,
    )
    assert r["ok"] is False
    assert "empty diff" in r["message"]


def test_recontract_rejects_invalid_mode(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    r = plane.recontract(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        reason="bad mode",
        changes={"objective": "new objective text"},
        owner_ack=False,
        mode="turbo",
    )
    assert r["ok"] is False
    assert r["error"] == "invalid_args"


def test_recontract_preserves_owner(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    new_constraints = dict(opened["contract"]["constraints"])
    new_constraints["owner"] = "impostor"
    new_constraints["allowed_paths"] = ["src/", "tests/", "docs/"]
    r = plane.recontract(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        reason="try owner swap while expanding paths",
        changes={"constraints": new_constraints},
        owner_ack=True,
    )
    assert r["ok"] is True
    assert r["contract"]["constraints"]["owner"] == "samuel"
    assert "docs/" in r["contract"]["constraints"]["allowed_paths"]


def test_close_requires_outcome_summary(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    ev1 = project / "tests" / "proof1.txt"
    ev2 = project / "tests" / "proof2.txt"
    ev1.write_text("one", encoding="utf-8")
    ev2.write_text("two", encoding="utf-8")
    r = plane.close_mission(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        evidence={"dc-1": {"path": str(ev1)}, "dc-2": {"path": str(ev2)}},
        outcome_summary="",
        closure="completed",
    )
    assert r["ok"] is False
    assert r["error"] == "invalid_args"


def test_close_rejects_directory_evidence(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    ev_dir = project / "tests" / "proofdir"
    ev_dir.mkdir(parents=True, exist_ok=True)
    ev2 = project / "tests" / "proof2.txt"
    ev2.write_text("two", encoding="utf-8")
    r = plane.close_mission(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        evidence={"dc-1": {"path": str(ev_dir)}, "dc-2": {"path": str(ev2)}},
        outcome_summary="done",
        closure="completed",
    )
    assert r["status"] == "incomplete"
    assert any("directory" in str(item.get("message", "")) for item in r["invalid_evidence"])


def test_park_item_requires_trigger_and_valid_origin(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    denied = plane.park_item(
        idea="later idea",
        origin_session_id="S-00000000T000000-00000000",
        trigger_event="",
        mission_id=opened["mission_id"],
        rationale="out of scope",
    )
    assert denied["ok"] is False
    assert "trigger_event" in denied["message"]

    denied2 = plane.park_item(
        idea="later idea",
        origin_session_id="S-00000000T000000-00000000",
        trigger_event="idea surfaced",
        mission_id=opened["mission_id"],
        rationale="out of scope",
    )
    assert denied2["ok"] is False
    assert "origin_session_id" in denied2["message"]


def test_lease_expiry_audited(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    auth = plane.authorize_session(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        agent_identity="composer",
        role="builder",
        contribution="x",
        expected_result="y",
        duration_minutes=1,
        criterion_ids=["dc-1"],
    )
    session = plane.mission.get_session(auth["session_id"])
    assert session is not None
    session["expires_at"] = "2000-01-01T00:00:00Z"
    plane.mission.save_session(session)
    before = set(plane.mission.list_audit_ids())
    plane.get_contract(auth["session_id"])
    after = set(plane.mission.list_audit_ids())
    new_events = after - before
    assert new_events
    latest = sorted(new_events)[-1]
    payload = json.loads((plane.mission.audit_dir / f"{latest}.json").read_text(encoding="utf-8"))
    assert payload["event_type"] == "lease_expired"


def _open_without_project(plane: ControlPlane) -> dict:
    return plane.open_mission(
        objective="Harden without a bound project root",
        artifact="notes.md",
        done_criteria=[
            {"id": "dc-1", "description": "first"},
            {"id": "dc-2", "description": "second"},
        ],
        non_goals=[],
        constraints={},  # deliberately no project_path
        owner="samuel",
    )


@pytest.mark.parametrize("expected_prefix", ["M", "S", "I"])
@pytest.mark.parametrize(
    "bad",
    [
        "X-20260722T010203-abcdef12",  # wrong prefix
        "M-20260722T010203-abcdef12/evil",  # separator
        "M-20260722T010203-abcdef12\\evil",  # backslash
        "../../escape",  # traversal
        "/abs/path",  # absolute
        "M-20260722T010203-abcdef12\x00",  # null byte
        " M-20260722T010203-abcdef12 ",  # whitespace padded
        "M-20260722T010203-abcdef12\n",  # trailing newline (Python $ bypass)
        "M-2026-abcdef12",  # wrong length/shape
        "M-20260722T010203-ABCDEF12",  # uppercase hex not allowed
        12345,  # not a string
    ],
)
def test_generated_id_validator_rejects_malformed(expected_prefix: str, bad: object) -> None:
    from hai_mcp.ids import require_generated_id, validate_generated_id

    ok, _ = validate_generated_id(bad, expected_prefix=expected_prefix)
    assert ok is False
    with pytest.raises(IdentifierError) as exc:
        require_generated_id(bad, expected_prefix=expected_prefix)
    assert exc.value.code == "invalid_args"


def test_intake_id_prefix_supported() -> None:
    from hai_mcp.ids import validate_intake_id

    assert validate_intake_id("I-20260722T010203-abcdef12")[0] is True
    assert validate_intake_id("M-20260722T010203-abcdef12")[0] is False


@pytest.mark.parametrize("ack", [False, None, 0, 1, "true"])
def test_recontract_requires_literal_owner_ack_true(plane: ControlPlane, project: Path, ack: object) -> None:
    opened = _open(plane, project)
    new_constraints = dict(opened["contract"]["constraints"])
    new_constraints["allowed_paths"] = ["src/", "tests/", "docs/"]
    r = plane.recontract(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        reason="expand",
        changes={"constraints": new_constraints},
        owner_ack=ack,  # type: ignore[arg-type]
    )
    assert r["ok"] is False
    assert r["error"] == "owner_gate_required"
    assert plane.mission.load_contract(opened["mission_id"], 2) is None
    assert plane.mission.current_contract(opened["mission_id"])["contract_version"] == 1


def test_recontract_cannot_change_mission_id_even_with_owner_ack(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    r = plane.recontract(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        reason="sneaky id swap",
        changes={"mission_id": "M-20000101T000000-deadbeef"},
        owner_ack=True,
    )
    assert r["ok"] is False
    assert r["error"] == "invalid_args"
    assert plane.mission.load_contract(opened["mission_id"], 2) is None
    assert plane.mission.current_contract(opened["mission_id"])["mission_id"] == opened["mission_id"]


@pytest.mark.parametrize("bad_prefix", ["../", "/abs/root", "src/../.."])
def test_open_mission_rejects_unsafe_allowed_paths(plane: ControlPlane, project: Path, bad_prefix: str) -> None:
    r = plane.open_mission(
        objective="test unsafe allowed paths",
        artifact="tests/file.py",
        done_criteria=[{"id": "dc-1", "description": "x"}],
        non_goals=[],
        constraints={"project_path": str(project), "allowed_paths": [bad_prefix]},
        owner="samuel",
    )
    assert r["ok"] is False
    assert r["error"] == "review_required"
    assert r["status"] == "review_required"
    assert any("allowed_paths" in i for i in r["issues"])


def test_check_activity_rejects_path_when_no_project_root(plane: ControlPlane) -> None:
    opened = _open_without_project(plane)
    auth = plane.authorize_session(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        agent_identity="composer",
        role="builder",
        contribution="x",
        expected_result="y",
        duration_minutes=30,
        criterion_ids=["dc-1"],
    )
    r = plane.check_activity(
        session_id=auth["session_id"],
        activity_step="read secrets",
        criterion_id="dc-1",
        affected_paths=["/etc/passwd"],
    )
    # without a bound project root, an unvalidatable path must be refused, not allowed
    assert r["classification"] == "drift"
    assert r["required_action"] == "stop"


def test_check_activity_null_byte_path_is_structured_not_raised(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    auth = plane.authorize_session(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        agent_identity="composer",
        role="builder",
        contribution="x",
        expected_result="y",
        duration_minutes=30,
        criterion_ids=["dc-1"],
    )
    r = plane.check_activity(
        session_id=auth["session_id"],
        activity_step="edit",
        criterion_id="dc-1",
        affected_paths=["src/foo\x00.py"],
    )
    assert r["classification"] == "drift"
    assert r["required_action"] == "stop"


def test_close_mission_rejects_evidence_without_project_root(plane: ControlPlane, tmp_path: Path) -> None:
    opened = _open_without_project(plane)
    real_file = tmp_path / "pyproject.toml"
    real_file.write_text("[tool]\n", encoding="utf-8")
    r = plane.close_mission(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        evidence={"dc-1": {"path": str(real_file)}, "dc-2": {"path": str(real_file)}},
        outcome_summary="claiming done with a foreign file",
        closure="completed",
    )
    assert r["status"] == "incomplete"
    assert r["invalid_evidence"]
    assert all(item.get("error") == "invalid_args" for item in r["invalid_evidence"])
    # the foreign file must NOT have been hashed/accepted as proof
    assert not r.get("verified_criteria")
    assert all("sha256" not in item for item in r["invalid_evidence"])
    assert plane.mission.load_mission_meta(opened["mission_id"])["status"] == "active"


def test_contract_and_sessions_symlink_escape_rejected(plane: ControlPlane, project: Path, tmp_path: Path) -> None:
    opened = _open(plane, project)
    mid = opened["mission_id"]
    mdir = plane.mission.missions_dir / mid
    outside = tmp_path / "escape-store"
    outside.mkdir()
    sentinel = outside / "stolen.json"
    sentinel.write_text('{"secret": true}', encoding="utf-8")
    sentinel_before = sentinel.read_bytes()

    # replace sessions/ with a symlink pointing outside HAI_HOME
    import shutil

    shutil.rmtree(mdir / "sessions")
    (mdir / "sessions").symlink_to(outside)

    from hai_mcp.paths import PathError

    with pytest.raises(PathError) as exc:
        plane.mission.sessions_dir(mid)
    assert exc.value.code == "path_outside_root"
    # get_session must not follow the escape either
    assert plane.mission.get_session("S-20000101T000000-abcdef12") is None
    # and the public surface must fail closed with an exact code, not leak
    got = plane.get_contract("S-20000101T000000-abcdef12")
    assert got["ok"] is False
    assert got["error"] == "invalid_args"
    assert sentinel.read_bytes() == sentinel_before


def test_get_session_binds_stored_ids(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    auth = plane.authorize_session(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        agent_identity="composer",
        role="builder",
        contribution="x",
        expected_result="y",
        duration_minutes=30,
        criterion_ids=["dc-1"],
    )
    sid = auth["session_id"]
    sdir = plane.mission.sessions_dir(opened["mission_id"])
    session_file = sdir / f"{sid}.json"

    # (a) tamper the stored mission_id so it no longer matches its directory
    session = plane.mission.get_session(sid)
    assert session is not None
    tampered = dict(session)
    tampered["mission_id"] = "M-20000101T000000-deadbeef"
    session_file.write_text(json.dumps(tampered), encoding="utf-8")
    before_bytes = session_file.read_bytes()
    assert plane.mission.get_session(sid) is None  # mismatched mission_id rejected
    assert session_file.read_bytes() == before_bytes  # read path did not mutate the record

    # (b) tamper the stored session_id so it no longer matches the requested id
    tampered2 = dict(session)
    tampered2["session_id"] = "S-20000101T000000-deadbeef"
    session_file.write_text(json.dumps(tampered2), encoding="utf-8")
    assert plane.mission.get_session(sid) is None  # mismatched session_id rejected


def test_concurrent_open_mission_only_one_active(plane: ControlPlane, project: Path) -> None:
    import threading

    c = _valid_contract(project)
    results: list[dict] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        barrier.wait()
        results.append(
            plane.open_mission(
                objective=c["objective"],
                artifact=c["artifact"],
                done_criteria=c["done_criteria"],
                non_goals=c["non_goals"],
                constraints=c["constraints"],
                owner=c["owner"],
            )
        )

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    active_count = sum(1 for r in results if r.get("status") == "active")
    denied_count = sum(1 for r in results if r.get("error") == "active_mission_exists")
    assert active_count == 1
    assert denied_count == 1

