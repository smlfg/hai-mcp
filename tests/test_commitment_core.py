from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from hai_mcp.config import Config
from hai_mcp.state import ControlPlane
from hai_mcp.transport import parse_runtime


@pytest.fixture
def plane(tmp_path: Path) -> ControlPlane:
    return ControlPlane(Config(hai_home=tmp_path / "hai_home"))


def _criteria() -> list[dict]:
    return [{"id": "dc-1", "description": "evidence file exists", "verifiable": True}]


def _register_both_devices(plane: ControlPlane, mac_root: Path, tp_root: Path) -> None:
    for device_id, root in (("macbook", mac_root), ("thinkpad", tp_root)):
        r = plane.register_mount(
            project_id="hai-mcp",
            device_id=device_id,
            root_path=str(root),
            owner_ack=True,
            reason=f"test mount {device_id}",
        )
        assert r["ok"] is True


def _read_jsonl(audit_dir: Path) -> list[dict]:
    path = audit_dir / "events.jsonl"
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def test_two_device_happy_path(plane: ControlPlane, tmp_path: Path) -> None:
    mac_root = tmp_path / "macbook-root"
    tp_root = tmp_path / "thinkpad-root"
    (mac_root / "src").mkdir(parents=True)
    (tp_root / "src").mkdir(parents=True)

    _register_both_devices(plane, mac_root, tp_root)

    opened = plane.open_mission(
        objective="Ship central commitment core",
        artifact="src/done.txt",
        done_criteria=_criteria(),
        non_goals=[],
        constraints={
            "project_id": "hai-mcp",
            "allowed_paths": ["src/"],
            "capabilities": ["read", "write"],
            "max_parallel_sessions": 2,
        },
        owner="samuel",
    )
    assert opened["ok"] is True
    contract = opened["contract"]
    assert contract["constraints"]["project_id"] == "hai-mcp"
    assert contract["constraints"]["project_path"] is None

    mac_auth = plane.authorize_session(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        agent_identity="mac-agent",
        role="builder",
        contribution="mac work",
        expected_result="done",
        duration_minutes=30,
        criterion_ids=["dc-1"],
        device_id="macbook",
        harness_id="cursor-mac",
    )
    assert mac_auth["ok"] is True

    tp_auth = plane.authorize_session(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        agent_identity="tp-agent",
        role="builder",
        contribution="tp work",
        expected_result="done",
        duration_minutes=30,
        criterion_ids=["dc-1"],
        device_id="thinkpad",
        harness_id="cursor-tp",
    )
    assert tp_auth["ok"] is True

    ev_rel = Path("src") / "done.txt"
    ev_file = tp_root / ev_rel
    ev_file.write_text("thinkpad evidence\n", encoding="utf-8")

    drift = plane.check_activity(
        session_id=tp_auth["session_id"],
        activity_step="touch mac absolute path",
        criterion_id="dc-1",
        affected_paths=[str(mac_root / "src" / "secret.txt")],
    )
    assert drift["classification"] == "drift"
    assert drift["required_action"] == "stop"

    closed = plane.close_mission(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        evidence={"dc-1": {"path": str(ev_rel)}},
        outcome_summary="completed on thinkpad",
        closure="completed",
        device_id="thinkpad",
    )
    assert closed["status"] == "completed"

    events = _read_jsonl(plane.mission.audit_dir)
    event_types = [e["event_type"] for e in events]
    assert "mount_registered" in event_types
    assert "mission_opened" in event_types
    assert "session_authorized" in event_types
    assert event_types.count("session_authorized") >= 2
    assert "mission_completed" in event_types


@pytest.mark.parametrize("ack", [False, 1, "true"])
def test_register_mount_owner_gate(plane: ControlPlane, tmp_path: Path, ack: object) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    r = plane.register_mount("hai-mcp", "macbook", str(root), owner_ack=ack, reason="test")
    assert r["ok"] is False
    assert r["error"] == "owner_gate_required"
    assert not (plane.cfg.hai_home / "projects.json").exists()


def test_register_mount_missing_dir(plane: ControlPlane, tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    r = plane.register_mount("hai-mcp", "macbook", str(missing), owner_ack=True, reason="test")
    assert r["ok"] is False
    assert r["error"] in ("missing_project", "invalid_args")


@pytest.mark.parametrize(
    "project_id",
    ["HAI-MCP", "hai_mcp", "hai/mcp", "../escape", "hai..mcp"],
)
def test_register_mount_invalid_slug(plane: ControlPlane, tmp_path: Path, project_id: str) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    r = plane.register_mount(project_id, "macbook", str(root), owner_ack=True, reason="test")
    assert r["ok"] is False
    assert r["error"] == "invalid_args"


def test_authorize_without_device_when_project_id(plane: ControlPlane, tmp_path: Path) -> None:
    root = tmp_path / "mac"
    (root / "src").mkdir(parents=True)
    plane.register_mount("hai-mcp", "macbook", str(root), owner_ack=True, reason="m")
    opened = plane.open_mission(
        objective="Need device",
        artifact="src/x",
        done_criteria=_criteria(),
        non_goals=[],
        constraints={"project_id": "hai-mcp", "allowed_paths": ["src/"]},
        owner="samuel",
    )
    auth = plane.authorize_session(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        agent_identity="a",
        role="r",
        contribution="c",
        expected_result="e",
        duration_minutes=10,
        criterion_ids=["dc-1"],
    )
    assert auth["ok"] is False
    assert auth["status"] == "denied"
    assert auth["error"] == "invalid_args"


def test_authorize_device_without_mount(plane: ControlPlane, tmp_path: Path) -> None:
    root = tmp_path / "mac"
    (root / "src").mkdir(parents=True)
    plane.register_mount("hai-mcp", "macbook", str(root), owner_ack=True, reason="m")
    opened = plane.open_mission(
        objective="Unknown device",
        artifact="src/x",
        done_criteria=_criteria(),
        non_goals=[],
        constraints={"project_id": "hai-mcp", "allowed_paths": ["src/"]},
        owner="samuel",
    )
    auth = plane.authorize_session(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        agent_identity="a",
        role="r",
        contribution="c",
        expected_result="e",
        duration_minutes=10,
        criterion_ids=["dc-1"],
        device_id="thinkpad",
        harness_id="h1",
    )
    assert auth["ok"] is False
    assert auth["status"] == "denied"


def test_open_mission_project_id_no_mounts(plane: ControlPlane) -> None:
    r = plane.open_mission(
        objective="No mounts yet",
        artifact="x",
        done_criteria=_criteria(),
        non_goals=[],
        constraints={"project_id": "hai-mcp", "allowed_paths": ["."]},
        owner="samuel",
    )
    assert r["ok"] is False
    assert r["error"] == "review_required"
    assert r["status"] == "review_required"


def test_close_mission_requires_device_id(plane: ControlPlane, tmp_path: Path) -> None:
    root = tmp_path / "tp"
    (root / "src").mkdir(parents=True)
    plane.register_mount("hai-mcp", "thinkpad", str(root), owner_ack=True, reason="m")
    opened = plane.open_mission(
        objective="Close needs device",
        artifact="src/x",
        done_criteria=_criteria(),
        non_goals=[],
        constraints={"project_id": "hai-mcp", "allowed_paths": ["src/"]},
        owner="samuel",
    )
    ev = root / "src" / "x"
    ev.write_text("x", encoding="utf-8")
    closed = plane.close_mission(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        evidence={"dc-1": {"path": "src/x"}},
        outcome_summary="try without device",
        closure="completed",
    )
    assert closed["ok"] is False
    assert closed["error"] == "invalid_args"
    assert plane.mission.load_mission_meta(opened["mission_id"])["status"] == "active"


def test_events_jsonl_valid_lines(plane: ControlPlane, tmp_path: Path) -> None:
    root = tmp_path / "mac"
    (root / "src").mkdir(parents=True)
    for i in range(3):
        plane.register_mount("hai-mcp", f"dev{i}", str(root), owner_ack=True, reason=f"r{i}")
    opened = plane.open_mission(
        objective="jsonl integrity",
        artifact="src/a",
        done_criteria=_criteria(),
        non_goals=[],
        constraints={"project_id": "hai-mcp", "allowed_paths": ["src/"]},
        owner="samuel",
    )
    plane.authorize_session(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        agent_identity="a",
        role="r",
        contribution="c",
        expected_result="e",
        duration_minutes=5,
        criterion_ids=["dc-1"],
        device_id="dev0",
        harness_id="h0",
    )
    jsonl = plane.mission.audit_dir / "events.jsonl"
    assert jsonl.is_file()
    for line in jsonl.read_text(encoding="utf-8").splitlines():
        if line.strip():
            json.loads(line)


def test_parse_runtime_default_stdio() -> None:
    r = parse_runtime({})
    assert r.transport == "stdio"
    assert r.host is None
    assert r.port is None


def test_parse_runtime_rejects_non_loopback() -> None:
    with pytest.raises(SystemExit):
        parse_runtime({"HAI_MCP_TRANSPORT": "streamable-http", "HAI_MCP_HTTP_HOST": "0.0.0.0"})


def test_parse_runtime_accepts_loopback_http() -> None:
    r = parse_runtime(
        {
            "HAI_MCP_TRANSPORT": "streamable-http",
            "HAI_MCP_HTTP_HOST": "127.0.0.1",
            "HAI_MCP_HTTP_PORT": "8765",
        }
    )
    assert r.transport == "streamable-http"
    assert r.host == "127.0.0.1"
    assert r.port == 8765


def test_health_kernel_fields(plane: ControlPlane) -> None:
    h = plane.health()
    assert h["kernel"] == "contract_kernel"
    assert h["authenticated_owner"] is False
    assert h["semantic_verification"] is False
    assert "projects" in h


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv required for HTTP smoke")
def test_streamable_http_lists_tools(tmp_path: Path) -> None:
    import asyncio
    import time

    hai_home = tmp_path / "http-home"
    hai_home.mkdir()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    env = {
        **os.environ,
        "HAI_HOME": str(hai_home),
        "HAI_MCP_TRANSPORT": "streamable-http",
        "HAI_MCP_HTTP_HOST": "127.0.0.1",
        "HAI_MCP_HTTP_PORT": str(port),
    }
    repo = Path(__file__).resolve().parents[1]
    proc = subprocess.Popen(
        [shutil.which("uv") or "uv", "run", "--directory", str(repo), "hai-mcp"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async def _list() -> list[str]:
            url = f"http://127.0.0.1:{port}/mcp"
            deadline = time.time() + 15
            last_err: Exception | None = None
            while time.time() < deadline:
                try:
                    async with streamable_http_client(url) as (read, write, _):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            tools = await session.list_tools()
                            return sorted(t.name for t in tools.tools)
                except Exception as exc:  # noqa: BLE001 — retry until server is up
                    last_err = exc
                    await asyncio.sleep(0.25)
            raise last_err or RuntimeError("server did not become ready")

        names = asyncio.run(_list())
        assert "hai_register_mount" in names
        assert "hai_health" in names
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
