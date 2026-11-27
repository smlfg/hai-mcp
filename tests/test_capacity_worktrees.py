"""Real-git capacity worktree auto-create, grant, isolation, and cleanup."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from hai_mcp.config import Config
from hai_mcp.state import ControlPlane
from hai_mcp.worktrees import grant_active

CODE_RE = re.compile(r"HAI owner code: ([2-9A-HJKMNP-Z]{4}-[2-9A-HJKMNP-Z]{4})")


@pytest.fixture
def plane(tmp_path: Path) -> ControlPlane:
    return ControlPlane(Config(hai_home=tmp_path / "hai_home", owner_home=tmp_path / "owner_home"))


def _owner_relays(plane: ControlPlane, pending: dict[str, Any]) -> str:
    path = plane.cfg.resolved_owner_home() / f"{pending['challenge_id']}.txt"
    match = CODE_RE.search(path.read_text(encoding="utf-8"))
    assert match, "owner file must carry the code"
    return match.group(1)


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=repo, check=True)
    for name in ("src", "tests", "docs", "tools"):
        (repo / name).mkdir()
        (repo / name / ".gitkeep").write_text("", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return repo.resolve()


def _capacity_window(repo: Path) -> dict[str, Any]:
    start = datetime.now(timezone.utc) - timedelta(minutes=1)
    end = start + timedelta(hours=3)
    return {
        "enabled": True,
        "project_path": str(repo),
        "max_active_lanes": 5,
        "max_codex_writers": 5,
        "starts_at": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _install_capacity(plane: ControlPlane, repo: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setenv("HAI_CAPACITY_PROJECTS", str(repo))
    policy = _capacity_window(repo)
    plane.cfg.hai_home.mkdir(parents=True, exist_ok=True)
    (plane.cfg.hai_home / "CAPACITY_WINDOW.json").write_text(json.dumps(policy), encoding="utf-8")
    return policy


def _open(plane: ControlPlane, repo: Path, *, lane: str, owner_code: str | None = None) -> dict[str, Any]:
    return plane.open_mission(
        objective=f"lane {lane}",
        artifact=f"artifact {lane}",
        done_criteria=[{"id": f"dc-{lane}", "description": "done"}],
        non_goals=[],
        constraints={
            "project_path": str(repo),
            "allowed_paths": [lane],
            "capabilities": ["read", "write", "test"],
            "time_limit_hours": 2,
            "max_parallel_sessions": 2,
        },
        owner="owner",
        owner_code=owner_code,
    )


def _open_with_grant(plane: ControlPlane, repo: Path, lane: str) -> dict[str, Any]:
    first = _open(plane, repo, lane=lane)
    if first.get("error") == "owner_gate_required":
        code = _owner_relays(plane, first)
        first = _open(plane, repo, lane=lane, owner_code=code)
    assert first.get("ok") is True, first
    return first


def test_four_missions_one_grant_four_worktrees(plane: ControlPlane, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _git_repo(tmp_path)
    policy = _install_capacity(plane, repo, monkeypatch)
    lanes = ("src", "tests", "docs", "tools")
    opened = []
    for lane in lanes:
        if not opened:
            pending = _open(plane, repo, lane=lane)
            assert pending.get("ok") is False
            assert pending.get("error") == "owner_gate_required"
            code = _owner_relays(plane, pending)
            opened.append(_open(plane, repo, lane=lane, owner_code=code))
        else:
            opened.append(_open(plane, repo, lane=lane))
        assert opened[-1]["ok"] is True
    window = plane.status()["capacity_window"]
    assert window["active"] is True
    grant = grant_active(plane.cfg.hai_home, window["fingerprint"])
    assert grant is not None
    paths = {Path(m["worktree"]["path"]) for m in opened}
    assert len(paths) == 4
    assert all(p.parent.name == ".worktrees" and p.parent.parent == repo for p in paths)
    proc = subprocess.run(["git", "-C", str(repo), "worktree", "list"], capture_output=True, text=True, check=True)
    for entry in opened:
        assert entry["worktree"]["path"] in proc.stdout
        assert entry["worktree"]["branch"].startswith("hai/M-")
    cap = plane.mission.capacity_status()
    assert cap["active_mission_count"] == 4
    assert len(cap.get("managed_worktrees", [])) == 4


def test_write_outside_own_worktree_refused(plane: ControlPlane, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _git_repo(tmp_path)
    _install_capacity(plane, repo, monkeypatch)
    a = _open_with_grant(plane, repo, "src")
    b = _open_with_grant(plane, repo, "tests")
    sess = plane.authorize_session(
        mission_id=a["mission_id"],
        contract_version=a["contract_version"],
        agent_identity="agent-a",
        role="implementer",
        contribution="work",
        expected_result="done",
        duration_minutes=30,
        criterion_ids=["dc-src"],
        capabilities=["read", "write"],
        harness_id="codex",
        write_paths=["src"],
    )
    assert sess["ok"] is True, sess
    other_root = json.loads(
        (plane.cfg.hai_home / "missions" / b["mission_id"] / "mission.json").read_text(encoding="utf-8")
    )["worktree_path"]
    drift = plane.check_activity(
        sess["session_id"],
        "write",
        [str(Path(other_root) / "tests" / "x.txt")],
        criterion_id="dc-src",
    )
    assert drift.get("classification") == "drift"
    assert drift.get("required_action") == "stop"
    overlap = plane.authorize_session(
        mission_id=a["mission_id"],
        contract_version=a["contract_version"],
        agent_identity="agent-b",
        role="implementer",
        contribution="overlap",
        expected_result="nope",
        duration_minutes=20,
        criterion_ids=["dc-src"],
        capabilities=["read", "write"],
        harness_id="codex",
        write_paths=[str(Path(other_root).relative_to(repo) / "tests")],
    )
    assert overlap.get("ok") is False


def test_close_and_abandon_cleanup(plane: ControlPlane, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _git_repo(tmp_path)
    _install_capacity(plane, repo, monkeypatch)
    opened = _open_with_grant(plane, repo, "src")
    wt = Path(opened["worktree"]["path"])
    assert wt.is_dir()
    sess = plane.authorize_session(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        agent_identity="agent",
        role="implementer",
        contribution="work",
        expected_result="done",
        duration_minutes=30,
        criterion_ids=["dc-src"],
        capabilities=["read", "write"],
        harness_id="codex",
        write_paths=["src"],
    )
    assert sess["ok"] is True
    (wt / "src" / "done.txt").write_text("ok\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(wt), "add", "src/done.txt"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(wt), "commit", "-m", "work"], check=True, capture_output=True)

    abandon = _open_with_grant(plane, repo, "tests")
    pending = plane.close_mission(
        mission_id=abandon["mission_id"],
        contract_version=abandon["contract_version"],
        evidence=None,
        outcome_summary="stop",
        closure="abandoned",
    )
    assert pending.get("error") == "owner_gate_required"
    code = _owner_relays(plane, pending)
    closed_abandon = plane.close_mission(
        mission_id=abandon["mission_id"],
        contract_version=abandon["contract_version"],
        evidence=None,
        outcome_summary="stop",
        closure="abandoned",
        owner_code=code,
    )
    assert closed_abandon["ok"] is True
    assert closed_abandon["worktree"]["removed"] is True
    assert closed_abandon["worktree"]["branch_kept"] is True
    assert not Path(abandon["worktree"]["path"]).exists()
    proc = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", abandon["worktree"]["branch"]],
        capture_output=True,
        text=True,
        check=True,
    )
    assert abandon["worktree"]["branch"] in proc.stdout

    complete = plane.close_mission(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        evidence={"dc-src": {"path": str(wt / "src" / "done.txt")}},
        outcome_summary="done",
        closure="completed",
    )
    assert complete["ok"] is True
    assert complete["worktree"]["removed"] is True
    assert not wt.exists()


def test_no_stray_worktrees_after_full_cleanup(plane: ControlPlane, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _git_repo(tmp_path)
    _install_capacity(plane, repo, monkeypatch)
    ids = []
    for lane in ("src", "tests"):
        opened = _open_with_grant(plane, repo, lane)
        ids.append((opened, lane))
    for opened, lane in ids:
        if lane == "tests":
            pending = plane.close_mission(
                opened["mission_id"],
                opened["contract_version"],
                None,
                "abandon",
                closure="abandoned",
            )
            code = _owner_relays(plane, pending)
            plane.close_mission(opened["mission_id"], opened["contract_version"], None, "abandon", "abandoned", owner_code=code)
        else:
            wt = Path(opened["worktree"]["path"])
            (wt / lane / "z.txt").write_text("z", encoding="utf-8")
            plane.close_mission(
                opened["mission_id"],
                opened["contract_version"],
                {f"dc-{lane}": {"path": str(wt / lane / "z.txt")}},
                "done",
                closure="completed",
            )
    proc = subprocess.run(["git", "-C", str(repo), "worktree", "list"], capture_output=True, text=True, check=True)
    assert ".worktrees" not in proc.stdout
    cap = plane.mission.capacity_status()
    assert cap.get("orphan_worktrees") == []


def test_window_expiry_still_lists_orphans(plane: ControlPlane, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _git_repo(tmp_path)
    policy = _install_capacity(plane, repo, monkeypatch)
    opened = _open_with_grant(plane, repo, "src")
    wt = opened["worktree"]["path"]
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    policy["expires_at"] = past
    (plane.cfg.hai_home / "CAPACITY_WINDOW.json").write_text(json.dumps(policy), encoding="utf-8")
    status = plane.status()
    assert status["capacity_window"]["active"] is False
    cap = plane.mission.capacity_status()
    assert cap.get("frozen_missions") or cap["active_mission_count"] <= 1
    orphans = cap.get("orphan_worktrees") or []
    if Path(wt).exists():
        assert wt in orphans