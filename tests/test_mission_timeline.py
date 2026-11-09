from __future__ import annotations

import importlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hai_mcp.config import Config
from hai_mcp.state import ControlPlane
from hai_mcp.timeline import build_timeline, list_missions, mission_timeline, render_mermaid


class Clock:
    """Deterministic UTC clock injected into the engine so audit `at` values are exact."""

    def __init__(self, start: str) -> None:
        self.now = start

    def set(self, value: str) -> None:
        self.now = value

    def __call__(self) -> str:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock("2026-09-14T10:00:00Z")
    monkeypatch.setattr("hai_mcp.mission._utc_now", c)
    return c


@pytest.fixture
def plane(tmp_path: Path, clock: Clock) -> ControlPlane:
    return ControlPlane(Config(hai_home=tmp_path / "hai_home", owner_gate="ack_legacy"))


@pytest.fixture
def project(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    (p / "src").mkdir(parents=True)
    return p


def _open(plane: ControlPlane, project: Path, objective: str = "Implement the timeline renderer") -> dict:
    return plane.open_mission(
        objective=objective,
        artifact="src/timeline.py with green pytest",
        done_criteria=[{"id": "dc-1", "description": "timeline renders"}],
        non_goals=["rewrite the engine"],
        constraints={"project_path": str(project), "allowed_paths": ["src/"], "capabilities": ["read", "write"]},
        owner="samuel",
    )


def _authorize(plane: ControlPlane, opened: dict, version: int) -> dict:
    r = plane.authorize_session(
        mission_id=opened["mission_id"],
        contract_version=version,
        agent_identity="composer",
        role="builder",
        contribution="build",
        expected_result="green",
        duration_minutes=30,
        criterion_ids=["dc-1"],
    )
    assert r["ok"] is True
    return r


def _abandon(plane: ControlPlane, opened: dict, version: int, reason: str = "no longer needed") -> dict:
    r = plane.close_mission(
        mission_id=opened["mission_id"],
        contract_version=version,
        evidence={},
        outcome_summary=reason,
        closure="abandoned",
        owner_ack=True,
    )
    assert r["ok"] is True
    return r


@pytest.fixture
def lived_mission(plane: ControlPlane, project: Path, clock: Clock) -> dict:
    """open 10:00 → work 10:01 → blocked 10:30..10:45 → work 10:46 → recontract 10:50
    → work 10:51 → abandoned 11:10 (a realistic waterfall with every span kind)."""
    opened = _open(plane, project)
    clock.set("2026-09-14T10:01:00Z")
    _authorize(plane, opened, 1)
    clock.set("2026-09-14T10:30:00Z")
    plane.mission.append_audit(
        "mission_blocked", {"mission_id": opened["mission_id"], "reason": "needs: SECRET-REASON #1"}
    )
    plane.mission.revoke_all_sessions(opened["mission_id"], reason="blocked")
    clock.set("2026-09-14T10:45:00Z")
    plane.mission.append_audit("mission_resumed", {"mission_id": opened["mission_id"], "from_status": "blocked"})
    clock.set("2026-09-14T10:46:00Z")
    _authorize(plane, opened, 1)
    clock.set("2026-09-14T10:50:00Z")
    constraints = {**opened["contract"]["constraints"], "allowed_paths": ["src/", "docs/"]}
    r = plane.recontract(
        mission_id=opened["mission_id"],
        contract_version=1,
        reason="need docs",
        changes={"constraints": constraints},
        owner_ack=True,
    )
    assert r["ok"] is True
    clock.set("2026-09-14T10:51:00Z")
    _authorize(plane, opened, 2)
    clock.set("2026-09-14T11:10:00Z")
    _abandon(plane, opened, 2)
    return opened


def test_timeline_spans_milestones_and_stats(plane: ControlPlane, lived_mission: dict) -> None:
    t = build_timeline(plane.mission, lived_mission["mission_id"])
    assert t["ok"] is True
    assert t["status"] == "abandoned"
    assert t["open"] is False
    assert t["contract_versions"] == 2

    spans = [(s["kind"], s["version"], s["start"][11:16], s["end"][11:16], s["seconds"]) for s in t["segments"]]
    assert spans == [
        ("work", 1, "10:01", "10:30", 29 * 60),
        ("blocked", 1, "10:30", "10:45", 15 * 60),
        ("work", 1, "10:46", "10:50", 4 * 60),
        ("work", 2, "10:51", "11:10", 19 * 60),
    ]
    assert [(m["kind"], m["version"]) for m in t["milestones"]] == [
        ("opened", 1),
        ("recontract", 2),
        ("abandoned", 2),
    ]
    stats = t["stats"]
    assert stats["total_seconds"] == 70 * 60
    assert stats["work_seconds"] == (29 + 4 + 19) * 60
    assert stats["blocked_seconds"] == 15 * 60
    assert stats["idle_seconds"] == 3 * 60
    assert (stats["sessions"], stats["blockers"], stats["recontracts"]) == (3, 1, 1)


def test_mermaid_is_structural_by_default(plane: ControlPlane, lived_mission: dict) -> None:
    out = mission_timeline(plane.mission, lived_mission["mission_id"])
    mermaid = out["mermaid"]
    assert mermaid.startswith("gantt\n")
    assert "section v1" in mermaid and "section v2" in mermaid
    assert "BLOCKER :crit" in mermaid
    assert "Recontract v2 :milestone" in mermaid
    assert "Mission abandoned :milestone" in mermaid
    assert "axisFormat %H:%M" in mermaid
    # free text stays out unless asked for
    assert "SECRET-REASON" not in mermaid
    assert "timeline renderer" not in mermaid
    assert out["markdown"] == f"```mermaid\n{mermaid}```"


def test_include_text_is_sanitised(plane: ControlPlane, lived_mission: dict) -> None:
    out = mission_timeline(plane.mission, lived_mission["mission_id"], include_text=True)
    mermaid = out["mermaid"]
    assert "SECRET-REASON 1" in mermaid  # ':' and '#' stripped so the diagram stays parseable
    assert "needs:" not in mermaid and "#1" not in mermaid
    assert "timeline renderer" in mermaid
    assert out["objective"].startswith("Implement the timeline renderer")


def test_open_mission_ends_at_injected_now(plane: ControlPlane, project: Path, clock: Clock) -> None:
    opened = _open(plane, project)
    clock.set("2026-09-14T10:05:00Z")
    _authorize(plane, opened, 1)
    now = datetime(2026, 9, 14, 10, 20, tzinfo=timezone.utc)
    t = build_timeline(plane.mission, opened["mission_id"], now=now)
    assert t["open"] is True
    assert t["ended_at"] == "2026-09-14T10:20:00Z"
    assert [(s["kind"], s["seconds"]) for s in t["segments"]] == [("work", 15 * 60)]


def test_open_blocker_runs_to_now(plane: ControlPlane, project: Path, clock: Clock) -> None:
    opened = _open(plane, project)
    clock.set("2026-09-14T10:05:00Z")
    _authorize(plane, opened, 1)
    clock.set("2026-09-14T10:10:00Z")
    plane.mission.append_audit("mission_blocked", {"mission_id": opened["mission_id"], "reason": "x"})
    now = datetime(2026, 9, 14, 10, 40, tzinfo=timezone.utc)
    t = build_timeline(plane.mission, opened["mission_id"], now=now)
    assert [(s["kind"], s["seconds"]) for s in t["segments"]] == [("work", 5 * 60), ("blocked", 30 * 60)]


def test_mission_without_session_only_has_opened_milestone(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    t = build_timeline(plane.mission, opened["mission_id"], now=datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc))
    assert t["segments"] == []
    assert [m["kind"] for m in t["milestones"]] == ["opened"]
    assert "Mission opened :milestone" in render_mermaid(t)


def test_markers_for_drift_and_rejected_completion(plane: ControlPlane, project: Path, clock: Clock) -> None:
    opened = _open(plane, project)
    mid = opened["mission_id"]
    clock.set("2026-09-14T10:05:00Z")
    plane.mission.append_audit("activity_checked", {"mission_id": mid, "classification": "in_scope"})
    plane.mission.append_audit("activity_checked", {"mission_id": mid, "classification": "drift"})
    plane.mission.append_audit("completion_rejected", {"mission_id": mid, "claimed_outcome": "done"})
    t = build_timeline(plane.mission, mid, now=datetime(2026, 9, 14, 10, 6, tzinfo=timezone.utc))
    assert [m["kind"] for m in t["milestones"]] == ["opened", "drift", "completion_rejected"]
    assert t["stats"]["activity_checks"] == 2
    assert t["stats"]["drift_events"] == 1
    assert t["stats"]["rejected_completions"] == 1
    mermaid = render_mermaid(t)
    assert "Drift detected :crit, milestone" in mermaid
    assert "Completion rejected :crit, milestone" in mermaid


def test_superseded_mission_ends_at_supersede_event(plane: ControlPlane, project: Path, clock: Clock) -> None:
    old = _open(plane, project)
    clock.set("2026-09-14T10:10:00Z")
    _authorize(plane, old, 1)
    clock.set("2026-09-14T10:30:00Z")
    new_id = "M-20260914T103000-deadbeef"
    plane.mission.append_audit(
        "mission_superseded",
        {"mission_id": new_id, "superseded_mission_id": old["mission_id"], "reason": "orphaned"},
    )
    t = build_timeline(plane.mission, old["mission_id"], now=datetime(2026, 9, 15, tzinfo=timezone.utc))
    assert t["milestones"][-1]["kind"] == "superseded"
    assert t["open"] is False
    assert [s["seconds"] for s in t["segments"]] == [20 * 60]


def test_multi_day_mission_uses_date_axis(plane: ControlPlane, project: Path, clock: Clock) -> None:
    opened = _open(plane, project)
    clock.set("2026-09-16T10:00:00Z")
    _abandon(plane, opened, 1)
    out = mission_timeline(plane.mission, opened["mission_id"])
    assert "axisFormat %m-%d %H:%M" in out["mermaid"]


def test_unknown_and_invalid_mission_fail_closed(plane: ControlPlane) -> None:
    assert build_timeline(plane.mission, "M-20260101T000000-00000000")["error"] == "mission_not_found"
    for bad in ("../etc/passwd", "M-1", "", "S-20260101T000000-00000000"):
        r = build_timeline(plane.mission, bad)
        assert r["ok"] is False and r["error"] == "invalid_args"


def test_list_missions_filters_orders_and_limits(plane: ControlPlane, project: Path, clock: Clock) -> None:
    clock.set("2026-09-13T09:00:00Z")
    first = _open(plane, project, objective="Day one mission")
    clock.set("2026-09-13T09:30:00Z")
    _abandon(plane, first, 1)
    clock.set("2026-09-14T09:00:00Z")
    second = _open(plane, project, objective="Day two mission")

    everything = list_missions(plane.mission)
    assert everything["ok"] is True
    assert everything["timezone"] == "UTC"
    assert [m["mission_id"] for m in everything["missions"]] == [second["mission_id"], first["mission_id"]]

    yesterday = list_missions(plane.mission, since="2026-09-13T00:00:00Z", until="2026-09-13T23:59:59Z")
    assert [m["mission_id"] for m in yesterday["missions"]] == [first["mission_id"]]
    assert yesterday["missions"][0]["status"] == "abandoned"
    assert yesterday["missions"][0]["objective"] == "Day one mission"

    assert [m["mission_id"] for m in list_missions(plane.mission, status="active")["missions"]] == [
        second["mission_id"]
    ]
    limited = list_missions(plane.mission, limit=1)
    assert limited["returned"] == 1 and limited["total_matching"] == 2


@pytest.mark.parametrize(
    "kwargs",
    [{"since": "yesterday"}, {"until": "nope"}, {"limit": "5"}, {"limit": True}, {"limit": 0}, {"status": ""}],
)
def test_list_missions_rejects_bad_args(plane: ControlPlane, kwargs: dict) -> None:
    r = list_missions(plane.mission, **kwargs)
    assert r["ok"] is False and r["error"] == "invalid_args"


def test_timeline_is_read_only(plane: ControlPlane, lived_mission: dict) -> None:
    def snapshot() -> dict[str, str]:
        home = plane.cfg.hai_home
        return {str(p.relative_to(home)): p.read_text() for p in home.rglob("*.json")}

    before = snapshot()
    build_timeline(plane.mission, lived_mission["mission_id"], include_text=True)
    list_missions(plane.mission)
    assert snapshot() == before


def test_control_plane_rejects_non_boolean_include_text(plane: ControlPlane, lived_mission: dict) -> None:
    r = plane.mission_timeline(lived_mission["mission_id"], include_text="true")
    assert r["ok"] is False and r["error"] == "invalid_args"


def test_tools_registered_and_callable(tmp_path: Path) -> None:
    os.environ["HAI_HOME"] = str(tmp_path / "discovery-home")
    import hai_mcp.server as server

    importlib.reload(server)
    registered = {tool.name for tool in server.mcp._tool_manager.list_tools()}
    assert {"hai_list_missions", "hai_mission_timeline"} <= registered

    listed = json.loads(server.hai_list_missions())
    assert listed["ok"] is True and listed["missions"] == []
    missing = json.loads(server.hai_mission_timeline("M-20260101T000000-00000000"))
    assert missing["error"] == "mission_not_found"
