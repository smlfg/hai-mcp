from __future__ import annotations

import json
from pathlib import Path

import pytest

from hai_mcp.config import Config
from hai_mcp.http_transport import http_bind_allowed
from hai_mcp.mission import MissionEngine
from hai_mcp.projects import ProjectStore
from hai_mcp.state import ControlPlane


@pytest.fixture
def plane(tmp_path: Path) -> ControlPlane:
    home = tmp_path / "hai_home"
    return ControlPlane(Config(hai_home=home))


@pytest.fixture
def macbook_root(tmp_path: Path) -> Path:
    root = tmp_path / "macbook_proj"
    (root / "src").mkdir(parents=True)
    return root


@pytest.fixture
def thinkpad_root(tmp_path: Path) -> Path:
    root = tmp_path / "thinkpad_proj"
    (root / "src").mkdir(parents=True)
    return root


def _project_id_contract(macbook_root: Path) -> dict:
    return {
        "objective": "Central core slice acceptance",
        "artifact": "src/x.py",
        "done_criteria": [{"id": "dc-1", "description": "src/x.py exists on device mount"}],
        "non_goals": ["sync HAI_HOME"],
        "constraints": {
            "project_id": "hai-mcp",
            "device_id": "macbook",
            "project_path": str(macbook_root),
            "allowed_paths": ["src/"],
            "capabilities": ["read", "write", "test"],
            "max_parallel_sessions": 1,
        },
        "owner": "samuel",
    }


def _open_project_id(plane: ControlPlane, macbook_root: Path) -> dict:
    c = _project_id_contract(macbook_root)
    return plane.open_mission(
        objective=c["objective"],
        artifact=c["artifact"],
        done_criteria=c["done_criteria"],
        non_goals=c["non_goals"],
        constraints=c["constraints"],
        owner=c["owner"],
    )


def test_open_with_project_id_records_mount_and_null_contract_path(
    plane: ControlPlane,
    macbook_root: Path,
) -> None:
    opened = _open_project_id(plane, macbook_root)
    assert opened["ok"] is True
    contract = opened["contract"]
    assert contract["constraints"]["project_id"] == "hai-mcp"
    assert contract["constraints"]["project_path"] is None

    store = ProjectStore(plane.cfg.hai_home)
    mount = store.get_mount_path("hai-mcp", "macbook")
    assert mount is not None
    assert mount == macbook_root.resolve()


def test_bind_project_owner_gate_then_success(
    plane: ControlPlane,
    macbook_root: Path,
    thinkpad_root: Path,
) -> None:
    _open_project_id(plane, macbook_root)

    denied = plane.bind_project(
        project_id="hai-mcp",
        device_id="thinkpad",
        local_path=str(thinkpad_root),
        owner_ack=False,
        reason="should not write",
    )
    assert denied["ok"] is False
    assert denied["error"] == "owner_gate_required"
    assert ProjectStore(plane.cfg.hai_home).get_mount_path("hai-mcp", "thinkpad") is None

    ok = plane.bind_project(
        project_id="hai-mcp",
        device_id="thinkpad",
        local_path=str(thinkpad_root),
        owner_ack=True,
        reason="bind thinkpad dev tree",
    )
    assert ok["ok"] is True
    mount = ProjectStore(plane.cfg.hai_home).get_mount_path("hai-mcp", "thinkpad")
    assert mount == thinkpad_root.resolve()


def test_authorize_thinkpad_get_contract_shares_identity(
    plane: ControlPlane,
    macbook_root: Path,
    thinkpad_root: Path,
) -> None:
    opened = _open_project_id(plane, macbook_root)
    plane.bind_project(
        project_id="hai-mcp",
        device_id="thinkpad",
        local_path=str(thinkpad_root),
        owner_ack=True,
        reason="bind thinkpad",
    )
    auth = plane.authorize_session(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        agent_identity="thinkpad-agent",
        role="builder",
        contribution="edit src/x.py",
        expected_result="file exists",
        duration_minutes=30,
        criterion_ids=["dc-1"],
        device_id="thinkpad",
        harness_id="cursor",
    )
    assert auth["ok"] is True
    got = plane.get_contract(auth["session_id"])
    assert got["ok"] is True
    assert got["mission_id"] == opened["mission_id"]
    assert got["contract_version"] == opened["contract_version"]
    assert got["contract_hash"] == opened["contract_hash"]
    assert got["project_id"] == "hai-mcp"
    assert got["device_id"] == "thinkpad"
    assert got["mount_path"] == str(thinkpad_root.resolve())


def test_check_activity_resolves_thinkpad_mount_not_macbook(
    plane: ControlPlane,
    macbook_root: Path,
    thinkpad_root: Path,
) -> None:
    opened = _open_project_id(plane, macbook_root)
    plane.bind_project(
        project_id="hai-mcp",
        device_id="thinkpad",
        local_path=str(thinkpad_root),
        owner_ack=True,
        reason="bind thinkpad",
    )
    auth = plane.authorize_session(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        agent_identity="thinkpad-agent",
        role="builder",
        contribution="edit src/x.py",
        expected_result="file exists",
        duration_minutes=30,
        criterion_ids=["dc-1"],
        device_id="thinkpad",
    )
    sid = auth["session_id"]
    thinkpad_file = thinkpad_root / "src" / "x.py"
    thinkpad_file.write_text("ok\n", encoding="utf-8")

    rel = plane.check_activity(
        session_id=sid,
        activity_step="edit src/x.py",
        criterion_id="dc-1",
        affected_paths=["src/x.py"],
    )
    assert rel["classification"] == "in_scope"
    assert rel["required_action"] == "continue"

    abs_ok = plane.check_activity(
        session_id=sid,
        activity_step="edit src/x.py",
        criterion_id="dc-1",
        affected_paths=[str(thinkpad_file)],
    )
    assert abs_ok["classification"] == "in_scope"

    mac_only = macbook_root / "src" / "only-mac.py"
    mac_only.write_text("mac\n", encoding="utf-8")
    drift = plane.check_activity(
        session_id=sid,
        activity_step="touch mac-only file",
        criterion_id="dc-1",
        affected_paths=[str(mac_only)],
    )
    assert drift["classification"] == "drift"
    assert drift["required_action"] == "stop"


def test_close_and_proof_require_device_id_for_project_missions(
    plane: ControlPlane,
    macbook_root: Path,
    thinkpad_root: Path,
) -> None:
    opened = _open_project_id(plane, macbook_root)
    plane.bind_project(
        project_id="hai-mcp",
        device_id="thinkpad",
        local_path=str(thinkpad_root),
        owner_ack=True,
        reason="bind thinkpad",
    )
    evidence_file = thinkpad_root / "src" / "x.py"
    evidence_file.write_text("done\n", encoding="utf-8")
    evidence = {"dc-1": {"path": str(evidence_file)}}

    missing = plane.close_mission(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        evidence=evidence,
        outcome_summary="done on thinkpad",
        closure="completed",
    )
    assert missing["ok"] is False
    assert missing["error"] == "invalid_args"

    completed = plane.close_mission(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        evidence=evidence,
        outcome_summary="done on thinkpad",
        closure="completed",
        device_id="thinkpad",
    )
    assert completed["ok"] is True
    assert completed["status"] == "completed"


def test_unknown_device_authorize_device_mount_required(
    plane: ControlPlane,
    macbook_root: Path,
) -> None:
    opened = _open_project_id(plane, macbook_root)
    r = plane.authorize_session(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        agent_identity="ghost",
        role="builder",
        contribution="x",
        expected_result="y",
        duration_minutes=10,
        criterion_ids=["dc-1"],
        device_id="thinkpad",
    )
    assert r["ok"] is False
    assert r["error"] == "device_mount_required"


@pytest.mark.parametrize(
    "project_id,device_id",
    [
        ("../evil", "macbook"),
        ("/abs", "macbook"),
        ("A", "macbook"),
        ("", "macbook"),
        ("hai-mcp", "../evil"),
        ("hai-mcp", "MacBook"),
    ],
)
def test_malformed_ids_invalid_args_no_write(
    plane: ControlPlane,
    macbook_root: Path,
    project_id: str,
    device_id: str,
) -> None:
    r = plane.open_mission(
        objective="x",
        artifact="y",
        done_criteria=[{"id": "dc-1", "description": "z"}],
        non_goals=[],
        constraints={
            "project_id": project_id,
            "device_id": device_id,
            "project_path": str(macbook_root),
            "allowed_paths": ["src/"],
        },
        owner="samuel",
    )
    assert r["ok"] is False
    assert r.get("error") in {"review_required", "invalid_args"} or r.get("status") == "review_required"
    assert plane.mission.load_active_pointer()["mission_id"] is None
    assert not (plane.cfg.hai_home / "core" / "projects.json").exists() or (
        ProjectStore(plane.cfg.hai_home).get_mount_path("hai-mcp", "macbook") is None
    )


def test_event_chain_links_after_open_bind_authorize(
    plane: ControlPlane,
    macbook_root: Path,
    thinkpad_root: Path,
) -> None:
    opened = _open_project_id(plane, macbook_root)
    plane.bind_project(
        project_id="hai-mcp",
        device_id="thinkpad",
        local_path=str(thinkpad_root),
        owner_ack=True,
        reason="bind thinkpad",
    )
    plane.authorize_session(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        agent_identity="thinkpad-agent",
        role="builder",
        contribution="x",
        expected_result="y",
        duration_minutes=10,
        criterion_ids=["dc-1"],
        device_id="thinkpad",
    )

    engine: MissionEngine = plane.mission
    audit_dir = engine.audit_dir
    events = []
    for path in sorted(audit_dir.glob("A-*.json")):
        events.append(json.loads(path.read_text(encoding="utf-8")))
    events.sort(key=lambda e: int(e["seq"]))

    assert len(events) >= 3
    seqs = [int(e["seq"]) for e in events]
    assert seqs == list(range(1, len(events) + 1))

    for idx in range(1, len(events)):
        prev = events[idx - 1]
        cur = events[idx]
        assert cur["prev_event_id"] == prev["event_id"]
        assert cur["prev_hash"] == prev["event_hash"]

    head = json.loads((audit_dir / "HEAD.json").read_text(encoding="utf-8"))
    last = events[-1]
    assert head["seq"] == last["seq"]
    assert head["event_id"] == last["event_id"]
    assert head["event_hash"] == last["event_hash"]


def test_legacy_project_path_open_still_works(plane: ControlPlane, macbook_root: Path) -> None:
    r = plane.open_mission(
        objective="Legacy mission",
        artifact="src/x.py",
        done_criteria=[{"id": "dc-1", "description": "legacy path works"}],
        non_goals=[],
        constraints={"project_path": str(macbook_root), "allowed_paths": ["src/"]},
        owner="samuel",
    )
    assert r["ok"] is True
    assert r["contract"]["constraints"]["project_path"] == str(macbook_root)
    assert r["contract"]["constraints"].get("project_id") is None


def test_http_bind_allowed_policy() -> None:
    ok, _ = http_bind_allowed("127.0.0.1", None)
    assert ok is True
    ok, _ = http_bind_allowed("localhost", None)
    assert ok is True
    ok, _ = http_bind_allowed("::1", None)
    assert ok is True

    ok, msg = http_bind_allowed("0.0.0.0", None)
    assert ok is False
    assert "HAI_HTTP_TOKEN" in msg

    ok, _ = http_bind_allowed("0.0.0.0", "secret-token")
    assert ok is True


def test_bind_rejects_symlink_escape(tmp_path: Path) -> None:
    home = tmp_path / "hai_home"
    store = ProjectStore(home)
    outside = tmp_path / "outside"
    outside.mkdir()
    link_parent = tmp_path / "links"
    link_parent.mkdir()
    bad_link = link_parent / "proj"
    bad_link.symlink_to(outside)

    data = {"version": 1, "projects": {"hai-mcp": {"project_id": "hai-mcp", "mounts": {}}}}
    store.save(data)

    _, err = store.bind_mount("hai-mcp", "thinkpad", str(bad_link))
    assert err is not None
    assert err["error"] == "path_outside_root"


def test_second_open_still_denied_with_project_id(
    plane: ControlPlane,
    macbook_root: Path,
    thinkpad_root: Path,
) -> None:
    first = _open_project_id(plane, macbook_root)
    second = plane.open_mission(
        objective="second",
        artifact="a",
        done_criteria=[{"id": "dc-1", "description": "d"}],
        non_goals=[],
        constraints={
            "project_id": "hai-mcp",
            "device_id": "thinkpad",
            "project_path": str(thinkpad_root),
            "allowed_paths": ["src/"],
        },
        owner="samuel",
    )
    assert second["ok"] is False
    assert second["error"] == "active_mission_exists"
    assert second["active_mission_id"] == first["mission_id"]


def test_recontract_preserves_project_id(plane: ControlPlane, macbook_root: Path) -> None:
    opened = _open_project_id(plane, macbook_root)
    before_hash = opened["contract_hash"]
    r = plane.recontract(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        reason="tighten scope",
        changes={"objective": "Central core slice acceptance (recontracted)"},
        owner_ack=True,
    )
    assert r["ok"] is True
    assert r["contract"]["constraints"]["project_id"] == "hai-mcp"
    assert r["contract"]["constraints"]["project_path"] is None
    assert r["contract_hash"] != before_hash
