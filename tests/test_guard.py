"""Enforcement layer: hooks must block foreign work before it executes, across harnesses."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hai_mcp.config import Config
from hai_mcp.guard import Guard, GuardEvent, _shell_facts, event_from_hook_payload
from hai_mcp.state import ControlPlane

REPO_SRC = Path(__file__).resolve().parents[1] / "src"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "hai_home"


@pytest.fixture
def plane(home: Path, tmp_path: Path) -> ControlPlane:
    return ControlPlane(Config(hai_home=home, owner_gate="ack_legacy", owner_home=tmp_path / "owner"))


@pytest.fixture
def mission_root(tmp_path: Path) -> Path:
    root = tmp_path / "mission_proj"
    (root / "src").mkdir(parents=True)
    return root


@pytest.fixture
def foreign_root(tmp_path: Path) -> Path:
    root = tmp_path / "new_cli_project"
    root.mkdir()
    return root


def _open(plane: ControlPlane, root: Path, **constraints) -> dict:
    opened = plane.open_mission(
        objective="Ship the guard slice",
        artifact="src/guard.py",
        done_criteria=[{"id": "dc-1", "description": "src/guard.py exists"}],
        non_goals=["new cli"],
        constraints={"project_path": str(root), "allowed_paths": ["src/"], **constraints},
        owner="samuel",
    )
    assert opened["ok"] is True, opened
    return opened


def _tool(cwd: Path, name: str, **tool_input) -> GuardEvent:
    return GuardEvent("tool", "claude-code", str(cwd), tool_name=name, tool_input=tool_input)


def _prompt(cwd: Path, text: str) -> GuardEvent:
    return GuardEvent("prompt", "claude-code", str(cwd), prompt=text)


# -- decision core -------------------------------------------------------------------------


def test_no_mission_allows_prompt_and_reads_but_blocks_acting(plane: ControlPlane, mission_root: Path) -> None:
    g = Guard(plane)
    assert g.evaluate(_prompt(mission_root, "open a mission")).allow
    assert g.evaluate(_tool(mission_root, "Read", file_path="x")).allow
    assert g.evaluate(_tool(mission_root, "mcp__hai__hai_open_mission")).allow
    d = g.evaluate(_tool(mission_root, "Write", file_path="src/a.py"))
    assert not d.allow and d.code == "no_active_mission"


def test_in_scope_write_and_shell_allowed(plane: ControlPlane, mission_root: Path) -> None:
    _open(plane, mission_root)
    g = Guard(plane)
    assert g.evaluate(_prompt(mission_root, "implement the guard")).allow
    assert g.evaluate(_tool(mission_root, "Write", file_path=str(mission_root / "src/a.py"))).allow
    assert g.evaluate(_tool(mission_root / "src", "Edit", file_path="a.py")).allow
    assert g.evaluate(_tool(mission_root, "Bash", command="pytest -q")).allow


def test_write_outside_allowed_prefix_blocked(plane: ControlPlane, mission_root: Path) -> None:
    _open(plane, mission_root)
    d = Guard(plane).evaluate(_tool(mission_root, "Write", file_path="README.md"))
    assert not d.allow and d.code == "outside_mission"


def test_foreign_project_blocked_and_prompt_parked(
    plane: ControlPlane, mission_root: Path, foreign_root: Path
) -> None:
    opened = _open(plane, mission_root)
    g = Guard(plane)
    d = g.evaluate(_prompt(foreign_root, "Baue eine neue CLI"))
    assert not d.allow and d.code == "outside_mission"
    assert d.mission_id == opened["mission_id"]
    intake = json.loads((plane.cfg.hai_home / "intake" / f"{d.intake_id}.json").read_text())
    assert intake["raw"] == "Baue eine neue CLI" and intake["actionable"] is False

    # Prompt typed inside the mission repo, but the write targets the foreign project.
    w = g.evaluate(_tool(mission_root, "Write", file_path=str(foreign_root / "cli.py")))
    assert not w.allow and w.code == "outside_mission"
    b = g.evaluate(_tool(mission_root, "Bash", command=f"cd {foreign_root} && touch x"))
    assert not b.allow and b.code == "outside_mission"
    # Every denial lands in the audit chain.
    events = [json.loads(p.read_text()) for p in (plane.cfg.hai_home / "audit").glob("*.json") if p.name[0] != "."]
    assert sum(1 for e in events if e.get("event_type") == "guard_denied") >= 3


def test_non_goal_prompt_parked(plane: ControlPlane, mission_root: Path) -> None:
    _open(plane, mission_root)
    d = Guard(plane).evaluate(_prompt(mission_root, "let's start a New CLI for this"))
    assert not d.allow and d.code == "non_goal" and d.intake_id


def test_sensitive_shell_needs_capability(plane: ControlPlane, mission_root: Path) -> None:
    _open(plane, mission_root)
    g = Guard(plane)
    d = g.evaluate(_tool(mission_root, "Bash", command="git add . && git push origin main"))
    assert not d.allow and d.code == "capability_missing" and "push" in d.reason
    assert not g.evaluate(_tool(mission_root, "Bash", command="rm -rf src")).allow


def test_capability_granted_allows_commit(plane: ControlPlane, mission_root: Path) -> None:
    _open(plane, mission_root, capabilities=["read", "write", "test", "commit"])
    assert Guard(plane).evaluate(_tool(mission_root, "Bash", command="git commit -m x")).allow


def test_codex_apply_patch_targets_checked(plane: ControlPlane, mission_root: Path, foreign_root: Path) -> None:
    _open(plane, mission_root)
    g = Guard(plane)
    ok_patch = "*** Begin Patch\n*** Add File: src/b.py\n+x\n*** End Patch\n"
    assert g.evaluate(_tool(mission_root, "apply_patch", input=ok_patch)).allow
    bad_patch = f"*** Begin Patch\n*** Update File: {foreign_root}/cli.py\n*** End Patch\n"
    assert not g.evaluate(_tool(mission_root, "apply_patch", input=bad_patch)).allow


def test_unknown_tool_outside_project_blocked(plane: ControlPlane, mission_root: Path, foreign_root: Path) -> None:
    _open(plane, mission_root)
    g = Guard(plane)
    assert not g.evaluate(_tool(foreign_root, "mcp__other__do_thing")).allow
    assert g.evaluate(_tool(mission_root, "mcp__other__do_thing")).allow


def test_project_id_mission_requires_device_mount(plane: ControlPlane, mission_root: Path) -> None:
    plane.open_mission(
        objective="Ship the guard slice",
        artifact="src/guard.py",
        done_criteria=[{"id": "dc-1", "description": "src/guard.py exists"}],
        non_goals=[],
        constraints={"project_id": "hai-mcp", "device_id": "macbook", "project_path": str(mission_root)},
        owner="samuel",
    )
    g = Guard(plane)
    ev = _tool(mission_root, "Write", file_path="src/a.py")
    assert not g.evaluate(ev).allow  # no device id given
    ev.device_id = "thinkpad"
    assert g.evaluate(ev).code == "device_mount_required"
    ev.device_id = "macbook"
    assert g.evaluate(ev).allow


def test_shell_facts() -> None:
    assert _shell_facts("cd /a&&git push -f") == (["/a"], {"force_push"})
    assert _shell_facts("git -C x commit -m 'a; b'") == ([], {"commit"})
    assert _shell_facts("rm file.txt") == ([], {"delete"})


def test_payload_normalization() -> None:
    p = event_from_hook_payload({"hook_event_name": "UserPromptSubmit", "prompt": "hi", "cwd": "/x"}, "codex", None)
    assert p.kind == "prompt" and p.prompt == "hi" and p.harness_id == "codex"
    t = event_from_hook_payload({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": "/x"}, "claude-code", "mac")
    assert t.kind == "tool" and t.tool_input == {"command": "ls"} and t.device_id == "mac"


# -- acceptance: real hook processes, two harnesses, one persistent state ----------------------


def _hook(home: Path, harness: str, payload: dict) -> subprocess.CompletedProcess:
    env = {**os.environ, "HAI_HOME": str(home), "HAI_OWNER_GATE": "ack_legacy", "PYTHONPATH": str(REPO_SRC)}
    return subprocess.run(
        [sys.executable, "-m", "hai_mcp.guard_cli", "--harness", harness],
        input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=30,
    )


def test_acceptance_direction_change_blocked_across_harnesses_and_restarts(
    plane: ControlPlane, home: Path, mission_root: Path, foreign_root: Path
) -> None:
    # 1. An existing mission is active and authorized.
    opened = _open(plane, mission_root)
    session = plane.authorize_session(
        mission_id=opened["mission_id"], contract_version=1, agent_identity="claude-code",
        role="builder", contribution="guard", expected_result="src/guard.py", duration_minutes=60,
        criterion_ids=["dc-1"],
    )
    assert session["ok"] is True, session

    prompt = {"hook_event_name": "UserPromptSubmit", "prompt": "Baue eine neue CLI", "cwd": str(foreign_root)}
    write = {
        "hook_event_name": "PreToolUse", "tool_name": "Write", "cwd": str(foreign_root),
        "tool_input": {"file_path": str(foreign_root / "cli.py"), "content": "x"},
    }
    # 2+3. Claude Code: the foreign task is rejected before the first acting step.
    for payload in (prompt, write):
        r = _hook(home, "claude-code", payload)
        assert r.returncode == 2, r.stderr
        assert opened["mission_id"] in r.stderr
    assert "Parked as intake" in _hook(home, "claude-code", prompt).stderr

    # 4+5. Codex, separate process, same persistent state: same rejection.
    codex_cmd = {
        "hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": str(foreign_root),
        "tool_input": {"command": ["bash", "-lc", "touch cli.py"]},
    }
    for payload in (prompt, codex_cmd):
        r = _hook(home, "codex", payload)
        assert r.returncode == 2, r.stderr

    # 6. "Restart": a brand-new ControlPlane + hook process still sees the lock.
    fresh = ControlPlane(Config(hai_home=home, owner_gate="ack_legacy", owner_home=home.parent / "owner"))
    assert not Guard(fresh).evaluate(_tool(foreign_root, "Write", file_path="cli.py")).allow
    # In-mission work keeps flowing.
    ok = _hook(home, "codex", {**write, "cwd": str(mission_root), "tool_input": {"file_path": str(mission_root / "src/g.py")}})
    assert ok.returncode == 0, ok.stderr


def test_hook_fails_closed_on_garbage(home: Path) -> None:
    env = {**os.environ, "HAI_HOME": str(home), "PYTHONPATH": str(REPO_SRC)}
    r = subprocess.run(
        [sys.executable, "-m", "hai_mcp.guard_cli"], input="not json", capture_output=True, text=True, env=env, timeout=30
    )
    assert r.returncode == 2 and "fail-closed" in r.stderr
