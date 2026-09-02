from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest

from hai_mcp.config import Config
from hai_mcp.state import ControlPlane


@pytest.fixture
def plane(tmp_path: Path) -> ControlPlane:
    # These tests exercise the legacy honor-system gate explicitly; the default gate is
    # 'nonce' and is covered by tests/test_owner_nonce_gate.py.
    return ControlPlane(Config(hai_home=tmp_path / "hai_home", owner_gate="ack_legacy"))


@pytest.fixture
def project(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    (p / "tests").mkdir(parents=True)
    return p


def _criteria() -> list[dict]:
    return [
        {"id": "dc-1", "description": "first"},
        {"id": "dc-2", "description": "second"},
    ]


# --- intake ---

def test_intake_captures_non_actionable(plane: ControlPlane) -> None:
    r = plane.intake("Maybe rebuild HAI, move to Mac, deploy router")
    assert r["ok"] is True
    assert r["actionable"] is False
    assert r["intake_id"].startswith("I-")
    stored = plane.intake_dir / f"{r['intake_id']}.json"
    assert stored.is_file()


def test_intake_rejects_empty(plane: ControlPlane) -> None:
    r = plane.intake("   ")
    assert r["ok"] is False
    assert r["error"] == "invalid_args"


def test_intake_rejects_store_symlink_outside_root_without_writing(plane: ControlPlane, tmp_path: Path) -> None:
    outside = tmp_path / "outside-intake"
    outside.mkdir()
    sentinel = outside / "sentinel.json"
    original = b"do not overwrite"
    sentinel.write_bytes(original)
    plane.intake_dir.symlink_to(outside, target_is_directory=True)

    r = plane.intake("must stay inside HAI_HOME")

    assert r["ok"] is False
    assert r["error"] == "path_outside_root"
    assert sentinel.read_bytes() == original


# --- distill: 1/1/park cardinality ---

def test_distill_enforces_single_decision_and_parks_rest(plane: ControlPlane) -> None:
    cap = plane.intake("many ideas at once")
    before_inbox = len(list(plane.inbox_dir.glob("*.md"))) if plane.inbox_dir.is_dir() else 0
    r = plane.distill(
        intake_id=cap["intake_id"],
        decision="Harden the MCP core",
        next_step="Write the failing path-traversal test",
        parklist=["idea A", "idea B", "idea C"],
    )
    assert r["ok"] is True
    assert r["decision"] == "Harden the MCP core"
    assert r["parked_count"] == 3
    after_inbox = len(list(plane.inbox_dir.glob("*.md")))
    assert after_inbox == before_inbox + 3


def test_distill_rejects_missing_or_bundled_fields(plane: ControlPlane) -> None:
    cap = plane.intake("x")
    assert plane.distill(cap["intake_id"], "", "step")["error"] == "invalid_args"
    assert plane.distill(cap["intake_id"], "decision", "")["error"] == "invalid_args"
    bundled = plane.distill(cap["intake_id"], "do A\ndo B\ndo C", "one step")
    assert bundled["ok"] is False
    assert bundled["error"] == "invalid_args"


def test_distill_rejects_unknown_and_malformed_intake(plane: ControlPlane) -> None:
    assert plane.distill("../../escape", "d", "s")["error"] == "invalid_args"
    assert plane.distill("I-20000101T000000-abcdef12", "d", "s")["error"] == "invalid_args"


def test_distill_rejects_store_symlink_outside_root_without_writing(plane: ControlPlane, tmp_path: Path) -> None:
    cap = plane.intake("distill this safely")
    outside = tmp_path / "outside-distillations"
    outside.mkdir()
    sentinel = outside / "sentinel.json"
    original = b"do not overwrite"
    sentinel.write_bytes(original)
    plane.distill_dir.symlink_to(outside, target_is_directory=True)

    r = plane.distill(cap["intake_id"], "one decision", "one next step")

    assert r["ok"] is False
    assert r["error"] == "path_outside_root"
    assert sentinel.read_bytes() == original


# --- mission_start + drift_check: the REAL server wrappers, isolated HAI_HOME ---

def test_mission_start_wrapper_opens_canonical_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    project = tmp_path / "proj"
    (project / "tests").mkdir(parents=True)
    monkeypatch.setenv("HAI_HOME", str(tmp_path / "ms-home"))
    import hai_mcp.server as server

    importlib.reload(server)
    out = json.loads(
        server.hai_mission_start(
            problem="Ship the hardening slice",
            artifact="tests/green.py",
            done_criteria=_criteria(),
            owner="samuel",
            time_limit_hours=2,
            constraints={"project_path": str(project), "allowed_paths": ["tests/"]},
        )
    )
    assert out["ok"] is True
    assert out["status"] == "active"
    assert out["contract"]["objective"] == "Ship the hardening slice"
    assert out["contract"]["constraints"]["time_limit_hours"] == 2


def test_drift_check_wrapper_flags_out_of_scope_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    project = tmp_path / "proj2"
    (project / "tests").mkdir(parents=True)
    monkeypatch.setenv("HAI_HOME", str(tmp_path / "dc-home"))
    import hai_mcp.server as server

    importlib.reload(server)
    opened = json.loads(
        server.hai_mission_start(
            problem="drift wrapper",
            artifact="tests/x.py",
            done_criteria=_criteria(),
            owner="samuel",
            constraints={"project_path": str(project), "allowed_paths": ["tests/"]},
        )
    )
    auth = json.loads(
        server.hai_authorize_session(
            mission_id=opened["mission_id"],
            contract_version=opened["contract_version"],
            agent_identity="composer",
            role="builder",
            contribution="x",
            expected_result="y",
            duration_minutes=30,
            criterion_ids=["dc-1"],
        )
    )
    r = json.loads(
        server.hai_drift_check(
            session_id=auth["session_id"],
            activity_step="touch outside",
            criterion_id="dc-1",
            affected_paths=[str(project / "outside" / "secret.txt")],
        )
    )
    assert r["classification"] == "drift"
    assert r["required_action"] == "stop"


# --- proof (thin wrapper over close_mission=completed) ---

def test_proof_completes_with_verified_evidence(plane: ControlPlane, project: Path) -> None:
    opened = plane.open_mission(
        objective="Prove completion",
        artifact="tests/out.py",
        done_criteria=_criteria(),
        non_goals=[],
        constraints={"project_path": str(project), "allowed_paths": ["tests/"]},
        owner="samuel",
    )
    ev1 = project / "tests" / "p1.txt"
    ev2 = project / "tests" / "p2.txt"
    ev1.write_text("a", encoding="utf-8")
    ev2.write_text("b", encoding="utf-8")
    r = plane.close_mission(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        evidence={"dc-1": {"path": str(ev1)}, "dc-2": {"path": str(ev2)}},
        outcome_summary="done",
        closure="completed",
    )
    assert r["status"] == "completed"


# --- stop (day terminal) ---

def test_stop_seals_day_and_revokes_leases_without_closing_mission(plane: ControlPlane, project: Path) -> None:
    opened = plane.open_mission(
        objective="Work then stop",
        artifact="tests/out.py",
        done_criteria=_criteria(),
        non_goals=[],
        constraints={"project_path": str(project), "allowed_paths": ["tests/"]},
        owner="samuel",
    )
    auth = plane.authorize_session(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        agent_identity="composer",
        role="builder",
        contribution="work",
        expected_result="done",
        duration_minutes=30,
        criterion_ids=["dc-1"],
    )
    r = plane.stop_day(day="2026-07-22", loop_closed=True, clearer="yes", agency_gained="high")
    assert r["ok"] is True
    assert r["status"] == "day_stopped"
    assert r["next_mission"] is None
    assert auth["session_id"] in r["revoked_sessions"]
    # mission is paused (leases gone) but NOT closed
    assert plane.mission.load_mission_meta(opened["mission_id"])["status"] == "active"
    # the revoked lease can no longer act
    got = plane.get_contract(auth["session_id"])
    assert got["ok"] is False


def test_stop_requires_day(plane: ControlPlane) -> None:
    assert plane.stop_day(day="", loop_closed=False, clearer="", agency_gained="")["error"] == "invalid_args"


def test_stop_rejects_store_symlink_outside_root_without_writing(plane: ControlPlane, tmp_path: Path) -> None:
    outside = tmp_path / "outside-stops"
    outside.mkdir()
    sentinel = outside / "sentinel.json"
    original = b"do not overwrite"
    sentinel.write_bytes(original)
    plane.stop_dir.symlink_to(outside, target_is_directory=True)

    r = plane.stop_day(day="2026-07-22", loop_closed=True, clearer="yes", agency_gained="high")

    assert r["ok"] is False
    assert r["error"] == "path_outside_root"
    assert sentinel.read_bytes() == original


# --- Item 4: full public surface is registered / discoverable (isolated HAI_HOME) ---

def test_additional_seven_tools_registered(tmp_path: Path) -> None:
    os.environ["HAI_HOME"] = str(tmp_path / "discovery-home")
    import hai_mcp.server as server

    importlib.reload(server)
    registered = {tool.name for tool in server.mcp._tool_manager.list_tools()}

    additional_seven = {
        "hai_intake",
        "hai_distill",
        "hai_mission_start",
        "hai_park",
        "hai_drift_check",
        "hai_proof",
        "hai_stop",
    }
    core_seven = {
        "hai_open_mission",
        "hai_authorize_session",
        "hai_get_contract",
        "hai_check_activity",
        "hai_park_item",
        "hai_recontract",
        "hai_close_mission",
    }
    assert additional_seven.issubset(registered)
    assert core_seven.issubset(registered)
