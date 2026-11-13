from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest

from hai_mcp import guard
from hai_mcp.config import Config
from hai_mcp.state import ControlPlane


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "hai_home"


@pytest.fixture
def plane(home: Path, tmp_path: Path) -> ControlPlane:
    return ControlPlane(Config(hai_home=home, owner_gate="ack_legacy", owner_home=tmp_path / "owner_home"))


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "docs").mkdir()
    return root


def _open(plane: ControlPlane, root: Path, *, project_id: str | None = "demo") -> str:
    constraints = {"project_path": str(root), "allowed_paths": ["src/"]}
    if project_id:
        constraints.update({"project_id": project_id, "device_id": "macbook"})
    opened = plane.open_mission(
        objective="guard test",
        artifact="src/x.py",
        done_criteria=[{"id": "dc-1", "description": "src/x.py exists"}],
        non_goals=["nothing else"],
        constraints=constraints,
        owner="samuel",
    )
    assert opened["ok"] is True, opened
    return opened["mission_id"]


def _lease(plane: ControlPlane, mission_id: str) -> dict:
    granted = plane.authorize_session(
        mission_id=mission_id,
        contract_version=1,
        agent_identity="test-agent",
        role="builder",
        contribution="dc-1: write src/x.py",
        criterion_ids=["dc-1"],
        expected_result="file exists",
        duration_minutes=30,
        device_id="macbook",
    )
    assert granted["status"] == "granted", granted
    return granted["session_lease"]


def _abandon(plane: ControlPlane, mission_id: str, device_id: str | None = "macbook") -> None:
    closed = plane.close_mission(
        device_id=device_id,
        mission_id=mission_id,
        contract_version=1,
        evidence=None,
        outcome_summary="dropped for test",
        closure="abandoned",
        owner_ack=True,
    )
    assert closed["ok"] is True, closed


def _tree_digest(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        h.update(str(p.relative_to(root)).encode())
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


def test_path_outside_every_mount_is_allowed(plane: ControlPlane, home: Path, proj: Path, tmp_path: Path) -> None:
    _open(plane, proj)
    result = guard.decide(home, str(tmp_path / "elsewhere" / "notes.md"))
    assert result == {"allow": True, "reason": "ungoverned"}


def test_missing_hai_home_allows_everything(tmp_path: Path) -> None:
    assert guard.decide(tmp_path / "no_such_home", str(tmp_path / "a.py"))["allow"] is True


def test_mounted_project_without_active_mission_blocks(plane: ControlPlane, home: Path, proj: Path) -> None:
    _abandon(plane, _open(plane, proj))  # the mount outlives the mission
    result = guard.decide(home, str(proj / "src" / "x.py"))
    assert (result["allow"], result["reason"]) == (False, "no_active_mission")


def test_active_mission_without_lease_blocks(plane: ControlPlane, home: Path, proj: Path) -> None:
    _open(plane, proj)
    result = guard.decide(home, str(proj / "src" / "x.py"))
    assert (result["allow"], result["reason"]) == (False, "no_valid_lease")


def test_valid_lease_inside_allowed_paths_allows(plane: ControlPlane, home: Path, proj: Path) -> None:
    mission_id = _open(plane, proj)
    _lease(plane, mission_id)
    result = guard.decide(home, str(proj / "src" / "new" / "x.py"))
    assert result == {"allow": True, "reason": "in_contract", "mission_id": mission_id}


def test_valid_lease_outside_allowed_paths_blocks(plane: ControlPlane, home: Path, proj: Path) -> None:
    _lease(plane, _open(plane, proj))
    result = guard.decide(home, str(proj / "docs" / "plan.md"))
    assert (result["allow"], result["reason"]) == (False, "path_outside_contract")


def test_expired_lease_blocks(plane: ControlPlane, home: Path, proj: Path) -> None:
    mission_id = _open(plane, proj)
    lease = _lease(plane, mission_id)
    path = home / "missions" / mission_id / "sessions" / f"{lease['session_id']}.json"
    lease["expires_at"] = "2000-01-01T00:00:00Z"
    path.write_text(json.dumps(lease), encoding="utf-8")
    assert guard.decide(home, str(proj / "src" / "x.py"))["reason"] == "no_valid_lease"


def test_lease_from_old_contract_version_blocks(plane: ControlPlane, home: Path, proj: Path) -> None:
    mission_id = _open(plane, proj)
    _lease(plane, mission_id)
    changed = plane.recontract(
        mission_id=mission_id,
        contract_version=1,
        changes={"objective": "guard test, widened"},
        reason="scope changed",
        owner_ack=True,
    )
    assert changed["ok"] is True, changed
    assert guard.decide(home, str(proj / "src" / "x.py"))["reason"] == "no_valid_lease"


def test_other_mounted_project_blocks_while_mission_runs_elsewhere(
    plane: ControlPlane, home: Path, proj: Path, tmp_path: Path
) -> None:
    other = tmp_path / "other"
    (other / "src").mkdir(parents=True)
    _abandon(plane, _open(plane, other, project_id="other"))
    _lease(plane, _open(plane, proj))
    result = guard.decide(home, str(other / "a.py"))
    assert (result["allow"], result["reason"]) == (False, "other_mission_active")


def test_legacy_project_path_mission_is_governed_only_while_active(
    plane: ControlPlane, home: Path, proj: Path
) -> None:
    mission_id = _open(plane, proj, project_id=None)
    assert guard.decide(home, str(proj / "src" / "x.py"))["reason"] == "no_valid_lease"
    _lease(plane, mission_id)
    assert guard.decide(home, str(proj / "src" / "x.py"))["allow"] is True
    _abandon(plane, mission_id, device_id=None)
    assert guard.decide(home, str(proj / "src" / "x.py")) == {"allow": True, "reason": "ungoverned"}


def test_symlink_into_governed_project_is_still_governed(
    plane: ControlPlane, home: Path, proj: Path, tmp_path: Path
) -> None:
    _open(plane, proj)
    link = tmp_path / "shortcut"
    link.symlink_to(proj / "src")
    assert guard.decide(home, str(link / "x.py"))["reason"] == "no_valid_lease"


def test_corrupt_state_fails_closed(plane: ControlPlane, home: Path, proj: Path) -> None:
    _open(plane, proj)
    (home / "core" / "projects.json").write_text("{not json", encoding="utf-8")
    result = guard.decide(home, str(proj / "src" / "x.py"))
    assert (result["allow"], result["reason"]) == (False, "state_unreadable")


def test_guard_never_writes_to_hai_home(plane: ControlPlane, home: Path, proj: Path) -> None:
    mission_id = _open(plane, proj)
    lease = _lease(plane, mission_id)
    path = home / "missions" / mission_id / "sessions" / f"{lease['session_id']}.json"
    lease["expires_at"] = "2000-01-01T00:00:00Z"
    path.write_text(json.dumps(lease), encoding="utf-8")
    before = _tree_digest(home)
    for target in ("src/x.py", "docs/plan.md", "../outside.txt"):
        guard.decide(home, str(proj / target))
    assert _tree_digest(home) == before


def test_cli_exit_codes_and_hook_payload(
    plane: ControlPlane,
    home: Path,
    proj: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HAI_HOME", str(home))
    _lease(plane, _open(plane, proj))

    assert guard.main(["--path", str(proj / "src" / "x.py")]) == 0
    assert guard.main(["--path", str(proj / "docs" / "plan.md")]) == 2
    assert "path_outside_contract" in capsys.readouterr().err

    payload = {"tool_name": "Write", "tool_input": {"file_path": str(proj / "docs" / "plan.md")}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert guard.main(["--hook-stdin"]) == 2

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}})))
    assert guard.main(["--hook-stdin"]) == 0
