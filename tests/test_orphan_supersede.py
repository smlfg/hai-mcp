"""An orphaned mission (no valid session lease left) must not block new missions forever,
but it is only ever superseded through the owner gate — and a live mission never is."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from hai_mcp.config import Config
from hai_mcp.state import ControlPlane

CODE_RE = re.compile(r"HAI owner code: ([2-9A-HJKMNP-Z]{4}-[2-9A-HJKMNP-Z]{4})")


@pytest.fixture
def plane(tmp_path: Path) -> ControlPlane:
    return ControlPlane(Config(hai_home=tmp_path / "hai_home", owner_home=tmp_path / "owner_home"))


@pytest.fixture
def project(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    (p / "Projek-Managment").mkdir(parents=True)
    (p / "src").mkdir()
    (p / "tests").mkdir()
    return p


def _owner_relays(plane: ControlPlane, pending: dict[str, Any]) -> str:
    path = plane.cfg.resolved_owner_home() / f"{pending['challenge_id']}.txt"
    match = CODE_RE.search(path.read_text(encoding="utf-8"))
    assert match, "owner file must carry the code"
    return match.group(1)


def _payload(project: Path, objective: str) -> dict[str, Any]:
    return dict(
        objective=objective,
        artifact="tests/out.md with green check",
        done_criteria=[{"id": "dc-1", "description": "tests exist and pass"}],
        non_goals=["refactor server"],
        constraints={
            "project_path": str(project),
            "allowed_paths": ["src", "tests"],
            "capabilities": ["read", "write", "test"],
            "time_limit_hours": 2,
        },
        owner="samuel",
    )


def _open(plane: ControlPlane, project: Path, objective: str = "First mission") -> dict[str, Any]:
    opened = plane.open_mission(**_payload(project, objective))
    assert opened["ok"] is True, opened
    return opened


def _lease(plane: ControlPlane, opened: dict[str, Any]) -> dict[str, Any]:
    granted = plane.authorize_session(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        agent_identity="codex",
        role="writer",
        contribution="write tests",
        expected_result="green tests",
        duration_minutes=30,
        criterion_ids=["dc-1"],
    )
    assert granted["ok"] is True, granted
    return granted["session_lease"]


def _expire(plane: ControlPlane, lease: dict[str, Any]) -> None:
    lease = dict(lease, expires_at="2000-01-01T00:00:00Z")
    plane.mission.save_session(lease)


def _status(plane: ControlPlane, mission_id: str) -> str:
    return plane.mission.load_mission_meta(mission_id)["status"]


def test_live_lease_is_never_superseded(plane: ControlPlane, project: Path) -> None:
    first = _open(plane, project)
    _lease(plane, first)
    res = plane.open_mission(
        **_payload(project, "Second"), supersede_orphan=True, supersede_reason="try to steal"
    )
    assert res["ok"] is False and res["error"] == "active_mission_exists"
    assert "orphaned" not in res
    assert _status(plane, first["mission_id"]) == "active"
    challenges = plane.cfg.hai_home / "owner" / "challenges"
    assert not challenges.exists() or not list(challenges.glob("*.json"))  # no owner prompt for a live mission


def test_refusal_names_the_orphan(plane: ControlPlane, project: Path) -> None:
    first = _open(plane, project)
    _expire(plane, _lease(plane, first))
    res = plane.open_mission(**_payload(project, "Second"))
    assert res["ok"] is False and res["error"] == "active_mission_exists"
    assert res["orphaned"] is True
    assert res["orphan"]["mission_id"] == first["mission_id"]
    assert res["orphan"]["session_count"] == 1
    assert res["orphan"]["owner"] == "samuel" and res["orphan"]["last_agent"] == "codex"
    assert res["orphan"]["last_activity_at"] >= "2000-01-01T00:00:00Z"
    assert "supersede_orphan" in res["next_action"]
    assert _status(plane, first["mission_id"]) == "active"  # refusing changes nothing


def test_mission_without_any_session_counts_as_orphan(plane: ControlPlane, project: Path) -> None:
    first = _open(plane, project)
    res = plane.open_mission(**_payload(project, "Second"))
    assert res["orphaned"] is True and res["orphan"]["session_count"] == 0
    assert _status(plane, first["mission_id"]) == "active"


def test_supersede_requires_reason(plane: ControlPlane, project: Path) -> None:
    first = _open(plane, project)
    _expire(plane, _lease(plane, first))
    res = plane.open_mission(**_payload(project, "Second"), supersede_orphan=True)
    assert res["ok"] is False and res["error"] == "invalid_args"
    assert _status(plane, first["mission_id"]) == "active"


def test_supersede_goes_through_the_owner_gate_once(plane: ControlPlane, project: Path) -> None:
    first = _open(plane, project)
    _expire(plane, _lease(plane, first))
    args = dict(**_payload(project, "Second"), supersede_orphan=True, supersede_reason="CB5 done, session gone")

    pending = plane.open_mission(**args)
    assert pending["ok"] is False and pending["status"] == "pending_owner_code"
    assert pending["orphan"]["mission_id"] == first["mission_id"]
    assert _status(plane, first["mission_id"]) == "active"  # agent alone cannot pass

    done = plane.open_mission(**args, owner_code=_owner_relays(plane, pending))
    assert done["ok"] is True and done["status"] == "active", done
    assert done["superseded_mission_id"] == first["mission_id"]
    assert done["mission_id"] != first["mission_id"]
    assert _status(plane, first["mission_id"]) == "abandoned"
    assert plane.mission.load_active_pointer()["mission_id"] == done["mission_id"]

    audit_dir = plane.mission.audit_dir
    abandoned = json.loads((audit_dir / f"{done['superseded_audit_event_id']}.json").read_text())
    assert abandoned["event_type"] == "mission_abandoned"
    assert "orphaned" in abandoned["reason"] and "CB5 done" in abandoned["reason"]
    events = [json.loads(p.read_text()) for p in audit_dir.glob("*.json")]
    superseded = [e for e in events if e.get("event_type") == "mission_superseded"]
    assert len(superseded) == 1
    assert superseded[0]["mission_id"] == done["mission_id"]
    assert superseded[0]["superseded_mission_id"] == first["mission_id"]


def test_day_stop_leaves_an_orphan_that_needs_the_owner(plane: ControlPlane, project: Path) -> None:
    """Revoked leases (e.g. hai_stop) orphan the mission; superseding still needs the owner."""
    first = _open(plane, project)
    _lease(plane, first)
    plane.mission.revoke_all_sessions(first["mission_id"], reason="day_stop")
    res = plane.open_mission(**_payload(project, "Next day"))
    assert res["orphaned"] is True
    pending = plane.open_mission(**_payload(project, "Next day"), supersede_orphan=True, supersede_reason="new day")
    assert pending["status"] == "pending_owner_code"
    assert _status(plane, first["mission_id"]) == "active"


def test_invalid_new_payload_never_abandons_the_orphan(plane: ControlPlane, project: Path) -> None:
    first = _open(plane, project)
    _expire(plane, _lease(plane, first))
    bad = _payload(project, "Second")
    bad["done_criteria"] = []
    res = plane.open_mission(**bad, supersede_orphan=True, supersede_reason="replace")
    assert res["ok"] is False and res["error"] == "review_required"
    assert _status(plane, first["mission_id"]) == "active"
