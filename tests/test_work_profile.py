"""Work profile ("Arbeitsprofil") slice: setup CLI, store, engine integration, gate.

Covers spec §9 acceptance criteria AC1–AC8 plus the extra invariants
(lowering saves without gate, reset needs no gate, running leases survive expiry).
"""

from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import hai_mcp.work_profile as wp
from hai_mcp.cli import _ask, cli_status, confirm_increase, run_dialog
from hai_mcp.config import Config
from hai_mcp.state import ControlPlane
from hai_mcp.work_profile import WorkProfileStore

REPO = Path(__file__).resolve().parents[1]


# ----- helpers ---------------------------------------------------------------


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _plane(tmp_path: Path, **kw: Any) -> ControlPlane:
    return ControlPlane(Config(hai_home=tmp_path / "hai_home", owner_gate="ack_legacy", **kw))


def _open(plane: ControlPlane, project: Path, **kw: Any) -> dict[str, Any]:
    return plane.open_mission(
        objective="Implement work profile tests",
        artifact="tests/test_work_profile.py green",
        done_criteria=[{"id": "dc-1", "description": "work profile tests pass"}],
        non_goals=[],
        constraints={"project_path": str(project)},
        owner="samuel",
        **kw,
    )


def _auth(plane: ControlPlane, opened: dict[str, Any], agent: str, **kw: Any) -> dict[str, Any]:
    return plane.authorize_session(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        agent_identity=agent,
        role="builder",
        contribution="tests",
        expected_result="green",
        duration_minutes=30,
        criterion_ids=["dc-1"],
        **kw,
    )


def _profile_record(opened: dict[str, Any], project: Path, **overrides: Any) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    record: dict[str, Any] = {
        "profile_id": wp.new_profile_id(),
        "project_path": str(project),
        "mission_id": opened["mission_id"],
        "contract_version": opened["contract_version"],
        "mode": "fast",
        "max_active_missions": 1,
        "max_writer_sessions": 3,
        "lease_budget": 10,
        "lease_ttl_minutes": 120,
        "expires": "mission_end",
        "created_at": _iso(now),
        "expires_at": _iso(now + timedelta(days=1)),
        "day_boundary": "00:00",
        "approved_via": "none",
        "challenge_id": None,
        "status": "active",
        "ended_reason": None,
        "ended_at": None,
    }
    record.update(overrides)
    return record


def _feed(items: list[str]):
    it = iter(items)

    def fn(prompt: str = "") -> str:
        return next(it)

    return fn


def _audit_types(home: Path) -> list[str]:
    audit = home / "audit"
    if not audit.is_dir():
        return []
    return sorted(
        json.loads(p.read_text(encoding="utf-8"))["event_type"] for p in audit.glob("A-*.json")
    )


# ----- AC1: dispatch + stdio -------------------------------------------------


def test_main_dialog_when_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    import hai_mcp.cli as cli
    import hai_mcp.server as server

    calls: list[list[str]] = []

    class _TTY:
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(cli, "run_cli", lambda argv: calls.append(list(argv)) or 0)
    monkeypatch.setattr(server.sys, "stdin", _TTY())
    monkeypatch.setattr(server.sys, "stdout", _TTY())
    server.main([])
    assert calls == [["setup"]]


def test_main_server_when_no_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    import hai_mcp.server as server

    runs: list[dict[str, Any]] = []

    class _NoTTY:
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr(server.mcp, "run", lambda **kw: runs.append(kw))
    monkeypatch.setattr(server.sys, "stdin", _NoTTY())
    monkeypatch.setattr(server.sys, "stdout", _NoTTY())
    server.main([])
    assert runs == [{"transport": "stdio"}]


def test_main_serve_subcommand(monkeypatch: pytest.MonkeyPatch) -> None:
    import hai_mcp.server as server

    runs: list[dict[str, Any]] = []
    monkeypatch.setattr(server.mcp, "run", lambda **kw: runs.append(kw))
    server.main(["serve"])
    assert runs == [{"transport": "stdio"}]


def _spawn(hai_home: Path, owner_home: Path, *extra: str) -> subprocess.Popen:
    uv = shutil.which("uv")
    assert uv, "uv is required to run hai-mcp"
    env = {**os.environ, "HAI_HOME": str(hai_home), "HAI_OWNER_HOME": str(owner_home)}
    return subprocess.Popen(
        [uv, "run", "--directory", str(REPO), "hai-mcp", *extra],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(REPO),
    )


def _readline(proc: subprocess.Popen, timeout: float = 30.0) -> str:
    ready, _, _ = select.select([proc.stdout], [], [], timeout)
    if not ready:
        raise TimeoutError("no response from hai-mcp")
    line = proc.stdout.readline()  # type: ignore[union-attr]
    if not line:
        raise EOFError("hai-mcp closed stdout")
    return line.decode("utf-8").strip()


def _handshake(proc: subprocess.Popen) -> list[str]:
    lines: list[str] = []
    proc.stdin.write(
        (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "tester", "version": "1.0"},
                    },
                }
            )
            + "\n"
        ).encode()
    )
    proc.stdin.flush()
    init = _readline(proc)
    lines.append(init)
    assert json.loads(init)["result"]["serverInfo"]["name"] == "hai-mcp"

    proc.stdin.write(
        (json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}) + "\n").encode()
    )
    proc.stdin.flush()
    proc.stdin.write(
        (json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}) + "\n").encode()
    )
    proc.stdin.flush()
    tools = _readline(proc)
    lines.append(tools)
    names = [t["name"] for t in json.loads(tools)["result"]["tools"]]
    assert "hai_get_profile" in names

    proc.stdin.close()
    rest = proc.stdout.read().decode("utf-8")  # type: ignore[union-attr]
    if rest.strip():
        lines.extend(rest.strip().splitlines())
    proc.wait(timeout=30)
    return lines


def _assert_stdout_only_json_rpc(lines: list[str]) -> None:
    for line in lines:
        assert line.strip(), f"empty stdout line: {line!r}"
        parsed = json.loads(line)
        assert "jsonrpc" in parsed, line
    blob = "\n".join(lines)
    for marker in ("Projekt", "Modus", "Speichern", "Zusammenfassung"):
        assert marker not in blob, blob


@pytest.mark.parametrize("extra", [(), ("serve",)])
def test_stdio_no_dialog_and_profile_tool(tmp_path: Path, extra: tuple[str, ...]) -> None:
    """AC1(b): no-TTY start stays an MCP server; stdout is JSON-RPC only."""
    hai_home = tmp_path / "hai_home"
    owner_home = tmp_path / "owner_home"
    proc = _spawn(hai_home, owner_home, *extra)
    try:
        lines = _handshake(proc)
    finally:
        if proc.poll() is None:
            proc.kill()
    _assert_stdout_only_json_rpc(lines)


# ----- AC2: no profile → default capacity -----------------------------------


def test_no_profile_second_writer_denied(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)
    plane = _plane(tmp_path)
    opened = _open(plane, project)
    a = _auth(plane, opened, "a")
    b = _auth(plane, opened, "b")
    assert a["ok"] is True
    assert b["ok"] is False
    assert b["error"] == "parallel_session_denied"
    assert b["waiting_for"] == "capacity"
    assert b["profile_source"] == "defaults"
    assert b["how_to_change"] == wp.HOW_TO_CHANGE


# ----- AC3: fast preset + one confirmation → 5 writers ----------------------


def test_fast_preset_dialog_one_confirmation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)
    monkeypatch.setenv("HAI_HOME", str(tmp_path / "hai_home"))
    monkeypatch.setenv("HAI_OWNER_HOME", str(tmp_path / "owner_home"))

    calls = {"n": 0}

    def mock_confirm(engine: Any, subject: dict[str, Any], io: tuple) -> tuple[str, str | None]:
        calls["n"] += 1
        return "touchid", None

    out: list[str] = []
    inputs = _feed(["goal", "c1,c2", "fast", "", "", "", "", "", "j"])
    rc = run_dialog(
        str(project),
        input_fn=inputs,
        output_fn=out.append,
        now=datetime.now().astimezone(),
        _confirm_increase=mock_confirm,
    )
    assert rc == 0, "\n".join(out)
    assert calls["n"] == 1

    plane = ControlPlane(Config.from_env())
    store = WorkProfileStore(plane.cfg, plane.mission)
    record = store.load(str(project))
    assert record is not None and record["status"] == "active"
    assert record["mode"] == "fast"
    assert record["max_writer_sessions"] == 5
    assert record["approved_via"] == "touchid"

    opened = {"mission_id": record["mission_id"], "contract_version": record["contract_version"]}
    for i in range(1, 6):
        granted = _auth(plane, opened, f"agent-{i}")
        assert granted["ok"] is True, granted
    sixth = _auth(plane, opened, "agent-6")
    assert sixth["ok"] is False
    assert sixth["error"] == "parallel_session_denied"
    assert sixth["waiting_for"] == "capacity"
    assert sixth["profile_source"] == "profile"
    assert calls["n"] == 1  # no further gate call during authorization


def test_confirm_increase_real_touchid_gate(tmp_path: Path) -> None:
    """The real gate path: Touch ID approval consumed by a second require call."""
    plane = ControlPlane(
        Config(
            hai_home=tmp_path / "hai_home",
            owner_home=tmp_path / "owner_home",
            owner_channel="touchid",
        )
    )
    calls: list[dict[str, Any]] = []

    def evaluate(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload)
        return {"ok": True, "fingerprint": payload["fingerprint"], "reason": payload["reason"], "method": "touchid"}

    plane.mission.owner_gate.touchid_evaluate = evaluate
    subject = {
        "project_path": "/tmp/proj",
        "mission_id": "M-20260101T000000-abcdef12",
        "max_writer_sessions": 5,
        "lease_budget": 20,
    }
    approved_via, challenge_id = confirm_increase(
        plane.mission,
        {"subject": subject, "summary": "Work profile: proj", "preview": subject},
        (_feed([]), lambda text: None),
    )
    assert approved_via == "touchid"
    assert challenge_id is not None
    assert len(calls) == 1  # the follow-up does not re-prompt


def test_confirm_increase_ack_legacy(tmp_path: Path) -> None:
    plane = _plane(tmp_path)
    subject = {"max_writer_sessions": 5}
    approved_via, challenge_id = confirm_increase(
        plane.mission,
        {"subject": subject, "summary": "Work profile", "preview": subject},
        (_feed(["ja"]), lambda text: None),
    )
    assert approved_via == "ack_legacy"
    assert challenge_id is None


def test_confirm_increase_file_channel(tmp_path: Path) -> None:
    plane = ControlPlane(
        Config(hai_home=tmp_path / "hai_home", owner_home=tmp_path / "owner_home", owner_channel="file")
    )
    subject = {"max_writer_sessions": 5}
    out: list[str] = []

    # First call issues a code to the owner home; confirm_increase asks for it.
    # We read the code from the delivered file and feed it back.
    def input_fn(prompt: str) -> str:
        out.append(prompt)
        owner_file = next((tmp_path / "owner_home").glob("C-*.txt"))
        text = owner_file.read_text(encoding="utf-8")
        return text.split("HAI owner code:", 1)[1].splitlines()[0].strip()

    approved_via, challenge_id = confirm_increase(
        plane.mission,
        {"subject": subject, "summary": "Work profile", "preview": subject},
        (input_fn, out.append),
    )
    assert approved_via == "file"
    assert challenge_id is not None


# ----- AC4: close mission → profile expired, defaults afterwards -------------


def test_close_mission_expires_profile(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)
    plane = _plane(tmp_path)
    store = WorkProfileStore(plane.cfg, plane.mission)
    opened = _open(plane, project)
    store.save(_profile_record(opened, project))

    assert _auth(plane, opened, "a")["ok"] is True
    assert _auth(plane, opened, "b")["ok"] is True
    assert _auth(plane, opened, "c")["ok"] is True

    proof = project / "src" / "proof.txt"
    proof.write_text("done", encoding="utf-8")
    closed = plane.close_mission(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        evidence={"dc-1": {"path": str(proof)}},
        outcome_summary="done",
        closure="completed",
    )
    assert closed["ok"] is True

    assert store.active_for_mission(opened["mission_id"]) is None
    assert "work_profile_expired" in _audit_types(tmp_path / "hai_home")

    opened2 = _open(plane, project)
    assert _auth(plane, opened2, "a")["ok"] is True
    second = _auth(plane, opened2, "b")
    assert second["ok"] is False
    assert second["error"] == "parallel_session_denied"
    assert second["profile_source"] == "defaults"


# ----- AC5: day rollover expiry ----------------------------------------------


def test_profile_expired_on_day_rollover(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)
    monkeypatch.setenv("HAI_DAY_BOUNDARY", "04:00")
    plane = _plane(tmp_path)
    store = WorkProfileStore(plane.cfg, plane.mission)
    opened = _open(plane, project)

    yesterday_noon = (datetime.now().astimezone() - timedelta(days=1)).replace(
        hour=12, minute=0, second=0, microsecond=0
    )
    record = _profile_record(
        opened,
        project,
        created_at=_iso(yesterday_noon),
        expires_at=_iso(yesterday_noon + timedelta(days=2)),
    )
    store.save(record)

    today = yesterday_noon + timedelta(days=1)
    at_0359 = today.replace(hour=3, minute=59, second=0, microsecond=0)
    at_0400 = today.replace(hour=4, minute=0, second=0, microsecond=0)

    assert store.active_for_mission(opened["mission_id"], now=at_0359) is not None
    assert store.active_for_mission(opened["mission_id"], now=at_0400) is None
    assert "work_profile_expired" in _audit_types(tmp_path / "hai_home")


# ----- AC6: status output ----------------------------------------------------


def test_status_shows_profile_expiry_capacity_waiting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)
    monkeypatch.setenv("HAI_HOME", str(tmp_path / "hai_home"))
    monkeypatch.setenv("HAI_OWNER_HOME", str(tmp_path / "owner_home"))

    plane = ControlPlane(Config.from_env())
    store = WorkProfileStore(plane.cfg, plane.mission)
    opened = _open(plane, project)
    store.save(_profile_record(opened, project))
    store.record_waiting(
        opened["mission_id"], "agent-x", "capacity", "max_writer_sessions", "max_writer_sessions=3 already reached"
    )

    out: list[str] = []
    cli_status(str(project), output_fn=out.append, now=datetime.now(timezone.utc))
    text = "\n".join(out)
    assert "Profil:" in text
    assert "Modus: fast" in text
    assert "Läuft ab:" in text
    assert "frei" in text
    assert "Wartet:" in text
    assert "agent-x" in text and "capacity" in text and "max_writer_sessions=3 already reached" in text


def test_status_json_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)
    monkeypatch.setenv("HAI_HOME", str(tmp_path / "hai_home"))
    monkeypatch.setenv("HAI_OWNER_HOME", str(tmp_path / "owner_home"))
    out: list[str] = []
    cli_status(str(project), json_out=True, output_fn=out.append)
    data = json.loads(out[0])
    assert data["ok"] is True
    assert data["source"] == "defaults"


# ----- AC7: agent cannot raise the profile -----------------------------------


def test_recontract_profile_keys_denied(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)
    plane = _plane(tmp_path)
    opened = _open(plane, project)
    r = plane.recontract(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        reason="raise lease budget",
        changes={"constraints": {"lease_budget": 50}},
        owner_ack=True,
    )
    assert r["ok"] is False
    assert r["error"] == "work_profile_not_agent_writable"
    assert r["how_to_change"] == wp.HOW_TO_CHANGE
    denied = [
        e
        for e in _audit_types(tmp_path / "hai_home")
        if e == "work_profile_change_denied"
    ]
    assert denied


def test_open_mission_profile_keys_denied(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)
    plane = _plane(tmp_path)
    r = plane.open_mission(
        objective="raise",
        artifact="artifact",
        done_criteria=[{"id": "dc-1", "description": "x"}],
        non_goals=[],
        constraints={"project_path": str(project), "max_writer_sessions": 5},
        owner="samuel",
    )
    assert r["ok"] is False
    assert r["error"] == "work_profile_not_agent_writable"
    assert "work_profile_change_denied" in _audit_types(tmp_path / "hai_home")


def test_only_readonly_profile_tool() -> None:
    import anyio

    from hai_mcp.server import mcp

    async def _run() -> None:
        tools = await mcp.list_tools()
        profile_tools = [t.name for t in tools if "profile" in t.name]
        assert profile_tools == ["hai_get_profile"]

    anyio.run(_run)


# ----- AC8: parse errors + dialog re-ask ------------------------------------


def test_parse_error_messages_name_range_and_example() -> None:
    for name, bad in (
        ("max_writer_sessions", "11"),
        ("max_writer_sessions", "0"),
        ("lease_budget", "0"),
        ("lease_ttl_minutes", "999"),
        ("max_active_missions", "abc"),
    ):
        value, err = wp.parse_int_field(name, bad)
        assert value is None
        assert "e.g." in err, err
        assert "–" in err, err

    _, err = wp.parse_mode("turbo")
    assert "e.g." in err and "fast" in err

    _, err = wp.parse_expires("bogus")
    assert "e.g." in err and "4h" in err


def test_parse_valid_values() -> None:
    assert wp.parse_int_field("max_writer_sessions", "4") == (4, "")
    assert wp.parse_int_field("lease_ttl_minutes", 120) == (120, "")
    assert wp.parse_mode("FAST") == ("fast", "")
    assert wp.parse_expires("mission_end") == ("mission_end", "")
    assert wp.parse_expires("missionsende") == ("mission_end", "")
    assert wp.parse_expires("heute 23:59") == ("today_2359", "")
    assert wp.parse_expires("1h30m") == ("duration:90", "")
    assert wp.parse_expires("") == ("mission_end", "")
    assert wp.is_increase({"lease_budget": 20}) is True
    assert wp.is_increase({"lease_budget": 2, "max_writer_sessions": 1}) is False


def test_dialog_reasks_after_invalid_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)
    monkeypatch.setenv("HAI_HOME", str(tmp_path / "hai_home"))
    monkeypatch.setenv("HAI_OWNER_HOME", str(tmp_path / "owner_home"))

    out: list[str] = []
    inputs = _feed(["goal", "c1", "careful", "999", "1", "", "", "", "", "j"])
    rc = run_dialog(
        str(project),
        input_fn=inputs,
        output_fn=out.append,
        now=datetime.now().astimezone(),
        _confirm_increase=lambda engine, subject, io: ("none", None),
    )
    assert rc == 0, "\n".join(out)
    text = "\n".join(out)
    assert any("e.g." in line for line in out), text


def test_ask_reasks_after_invalid() -> None:
    items = iter(["999", "4"])
    out: list[str] = []
    value = _ask(lambda p: next(items), out.append, "max_writer_sessions", 1, lambda r: wp.parse_int_field("max_writer_sessions", r))
    assert value == 4
    assert any("e.g." in line for line in out)


# ----- extra invariants ------------------------------------------------------


def test_lowering_saves_without_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)
    monkeypatch.setenv("HAI_HOME", str(tmp_path / "hai_home"))
    monkeypatch.setenv("HAI_OWNER_HOME", str(tmp_path / "owner_home"))

    calls = {"n": 0}

    def mock_confirm(engine: Any, subject: dict[str, Any], io: tuple) -> tuple[str, str | None]:
        calls["n"] += 1
        return "touchid", None

    out: list[str] = []
    inputs = _feed(["goal", "c1", "careful", "", "", "", "", "", "j"])
    rc = run_dialog(
        str(project),
        input_fn=inputs,
        output_fn=out.append,
        now=datetime.now().astimezone(),
        _confirm_increase=mock_confirm,
    )
    assert rc == 0
    assert calls["n"] == 0  # defaults are not an increase

    plane = ControlPlane(Config.from_env())
    store = WorkProfileStore(plane.cfg, plane.mission)
    record = store.load(str(project))
    assert record["approved_via"] == "none"


def test_reset_needs_no_gate(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)
    plane = _plane(tmp_path)
    store = WorkProfileStore(plane.cfg, plane.mission)
    opened = _open(plane, project)
    store.save(_profile_record(opened, project))

    reset = store.reset(str(project))
    assert reset is not None and reset["status"] == "reset"
    assert "work_profile_reset" in _audit_types(tmp_path / "hai_home")
    assert store.active_for_mission(opened["mission_id"]) is None


def test_running_lease_survives_profile_expiry(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)
    plane = _plane(tmp_path)
    store = WorkProfileStore(plane.cfg, plane.mission)
    opened = _open(plane, project)
    record = _profile_record(opened, project)
    store.save(record)

    a = _auth(plane, opened, "a")
    assert a["ok"] is True
    assert a["session_lease"]["profile_id"] == record["profile_id"]

    # Expire the profile (rewrite expires_at into the past).
    record["expires_at"] = _iso(datetime.now(timezone.utc) - timedelta(minutes=5))
    store.save(record)

    # The running lease is not revoked.
    assert plane.get_contract(a["session_id"])["ok"] is True
    assert plane.mission.get_session(a["session_id"])["status"] == "active"

    # No new lease above defaults is granted.
    b = _auth(plane, opened, "b")
    assert b["ok"] is False
    assert b["error"] == "parallel_session_denied"
    assert b["profile_source"] == "defaults"


# ----- extra: lease budget, TTL cap, failed gate ------------------------------


def test_profile_lease_budget_and_ttl_cap(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)
    plane = _plane(tmp_path)
    store = WorkProfileStore(plane.cfg, plane.mission)
    opened = _open(plane, project)
    store.save(_profile_record(opened, project, max_writer_sessions=5, lease_budget=2, lease_ttl_minutes=10))

    a = _auth(plane, opened, "a")
    assert a["ok"] is True
    granted = datetime.strptime(a["session_lease"]["granted_at"], "%Y-%m-%dT%H:%M:%SZ")
    expires = datetime.strptime(a["session_lease"]["expires_at"], "%Y-%m-%dT%H:%M:%SZ")
    assert expires - granted <= timedelta(minutes=10, seconds=1)  # requested 30, capped at profile TTL
    assert _auth(plane, opened, "b")["ok"] is True
    third = _auth(plane, opened, "c")
    assert third["ok"] is False
    assert third["error"] == "profile_lease_budget_exhausted"
    assert third["waiting_for"] == "capacity"
    assert third["limit"] == "lease_budget"

    described = store.describe(project_path=str(project))
    assert described["leases_free"] == 0
    assert any(w["agent_identity"] == "c" for w in described["waiting"])


def test_confirm_increase_failed_touchid_aborts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed/cancelled Touch ID must fail closed: no code prompt, RuntimeError (dialog aborts unsaved)."""
    plane = ControlPlane(
        Config(hai_home=tmp_path / "hai_home", owner_home=tmp_path / "owner_home", owner_channel="touchid")
    )

    def evaluate(payload: dict[str, Any]) -> dict[str, Any]:
        return {"ok": False, "error": "user_cancel", "fingerprint": payload["fingerprint"], "reason": payload["reason"]}

    plane.mission.owner_gate.touchid_evaluate = evaluate
    asked: list[str] = []

    def input_fn(prompt: str = "") -> str:
        asked.append(prompt)
        return "AAAA2222"

    subject = {"max_writer_sessions": 5}
    with pytest.raises(RuntimeError):
        confirm_increase(
            plane.mission,
            {"subject": subject, "summary": "Work profile", "preview": subject},
            (input_fn, lambda text: None),
        )
    assert asked == []
