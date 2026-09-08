"""The owner is a separate principal: the agent cannot pass an owner gate alone.

Every test here runs the DEFAULT gate (nonce). The helper ``_owner_relays`` plays the
human: it reads the code from the owner channel — something the agent-side plane never
sees in plaintext — and hands it back.
"""

from __future__ import annotations

import json
import re
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from hai_mcp import owner_gate as og
from hai_mcp.config import Config
from hai_mcp.state import ControlPlane

CODE_RE = re.compile(r"HAI owner code: ([2-9A-HJKMNP-Z]{4}-[2-9A-HJKMNP-Z]{4})")


@pytest.fixture
def plane(tmp_path: Path) -> ControlPlane:
    # owner_home lives NEXT TO hai_home — never inside it.
    return ControlPlane(Config(hai_home=tmp_path / "hai_home", owner_home=tmp_path / "owner_home"))


@pytest.fixture
def project(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    (p / "Projek-Managment").mkdir(parents=True)
    (p / "src").mkdir()
    (p / "tests").mkdir()
    (p / "tests" / "out.md").write_text("done\n", encoding="utf-8")
    return p


def _owner_relays(plane: ControlPlane, pending: dict[str, Any]) -> str:
    path = plane.cfg.resolved_owner_home() / f"{pending['challenge_id']}.txt"
    match = CODE_RE.search(path.read_text(encoding="utf-8"))
    assert match, "owner file must carry the code"
    return match.group(1)


def _scan_for(root: Path, needle: str) -> list[Path]:
    hits: list[Path] = []
    for path in root.rglob("*"):
        if path.is_file() and needle in path.read_text(encoding="utf-8", errors="ignore"):
            hits.append(path)
    return hits


def _propose(plane: ControlPlane, project: Path, body: str = "# do the thing\n") -> None:
    assert plane.propose_next_step(str(project), body)["ok"] is True


def _open(plane: ControlPlane, project: Path) -> dict[str, Any]:
    opened = plane.open_mission(
        objective="Implement drift classifier tests",
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
    assert opened["ok"] is True, opened
    return opened


# --- accept_next_step ---------------------------------------------------------


def test_owner_ack_alone_is_not_enough(plane: ControlPlane, project: Path) -> None:
    _propose(plane, project)
    res = plane.accept_next_step(str(project), owner_ack=True, reason="I asked, honest")
    assert res["ok"] is False
    assert res["error"] == "owner_gate_required"
    assert res["status"] == "pending_owner_code"
    assert res["gate"] == "nonce"
    assert not (project / "Projek-Managment" / "NEXT_STEP.md").exists()


def test_code_goes_to_owner_home_and_only_a_hash_stays_in_hai_home(plane: ControlPlane, project: Path) -> None:
    _propose(plane, project)
    pending = plane.accept_next_step(str(project))
    code = _owner_relays(plane, pending)
    assert _scan_for(plane.cfg.hai_home, code) == []  # plaintext never lands where the agent reads
    record = json.loads((plane.cfg.hai_home / "owner" / "challenges" / f"{pending['challenge_id']}.json").read_text())
    assert record["status"] == "pending" and "code_hash" in record and code not in json.dumps(record)
    owner_file = plane.cfg.resolved_owner_home() / f"{pending['challenge_id']}.txt"
    assert owner_file.stat().st_mode & 0o777 == 0o600
    text = owner_file.read_text(encoding="utf-8")
    assert "do the thing" in text  # the owner sees WHAT is being approved, not just a captcha


def test_correct_code_promotes_and_is_single_use(plane: ControlPlane, project: Path) -> None:
    _propose(plane, project)
    pending = plane.accept_next_step(str(project))
    code = _owner_relays(plane, pending)
    ok = plane.accept_next_step(str(project), owner_code=code)
    assert ok["ok"] is True and ok["canonical"] is True
    assert ok["owner_gate"]["challenge_id"] == pending["challenge_id"]
    canonical = project / "Projek-Managment" / "NEXT_STEP.md"
    assert canonical.is_file() and "gate: nonce" in canonical.read_text(encoding="utf-8")
    # same body again, same code: consumed, so no live challenge for it
    _propose(plane, project)
    again = plane.accept_next_step(str(project), owner_code=code)
    assert again["ok"] is False and again["detail"] == "no_pending_challenge"


def test_lowercase_and_unformatted_codes_are_accepted(plane: ControlPlane, project: Path) -> None:
    _propose(plane, project)
    code = _owner_relays(plane, plane.accept_next_step(str(project)))
    sloppy = code.replace("-", " ").lower()
    assert plane.accept_next_step(str(project), owner_code=sloppy)["ok"] is True


def test_three_wrong_codes_cancel_the_challenge(plane: ControlPlane, project: Path) -> None:
    _propose(plane, project)
    pending = plane.accept_next_step(str(project))
    code = _owner_relays(plane, pending)
    wrong = "2222-3333" if code != "2222-3333" else "4444-5555"
    for remaining in (2, 1):
        res = plane.accept_next_step(str(project), owner_code=wrong)
        assert res["detail"] == "invalid_owner_code" and res["attempts_remaining"] == remaining
    res = plane.accept_next_step(str(project), owner_code=wrong)
    assert res["detail"] == "invalid_owner_code" and res["status"] == "cancelled"
    # the real code no longer works …
    assert plane.accept_next_step(str(project), owner_code=code)["detail"] == "no_pending_challenge"
    # … and the next ask produces a fresh challenge (new id, new push to the owner)
    fresh = plane.accept_next_step(str(project))
    assert fresh["status"] == "pending_owner_code" and fresh["challenge_id"] != pending["challenge_id"]
    assert not (project / "Projek-Managment" / "NEXT_STEP.md").exists()


def test_code_is_bound_to_the_exact_content(plane: ControlPlane, project: Path) -> None:
    _propose(plane, project, "# version A\n")
    code = _owner_relays(plane, plane.accept_next_step(str(project)))
    # the agent swaps the content after the owner approved A
    swapped = plane.accept_next_step(str(project), owner_code=code, content="# version B — rm -rf\n")
    assert swapped["ok"] is False and swapped["detail"] == "no_pending_challenge"
    assert not (project / "Projek-Managment" / "NEXT_STEP.md").exists()
    # the approved content still goes through
    assert plane.accept_next_step(str(project), owner_code=code)["ok"] is True
    assert "version A" in (project / "Projek-Managment" / "NEXT_STEP.md").read_text(encoding="utf-8")


def test_repeated_asks_reuse_the_pending_challenge(plane: ControlPlane, project: Path) -> None:
    _propose(plane, project)
    first = plane.accept_next_step(str(project))
    second = plane.accept_next_step(str(project))
    assert first["challenge_id"] == second["challenge_id"]
    assert len(list(plane.cfg.resolved_owner_home().glob("C-*.txt"))) == 1  # no push-spam loop
    assert plane.status()["pending_owner_challenges"] == 1


def test_expired_code_is_rejected(plane: ControlPlane, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _propose(plane, project)
    pending = plane.accept_next_step(str(project))
    code = _owner_relays(plane, pending)
    real_time = og.time.time
    monkeypatch.setattr(og.time, "time", lambda: real_time() + plane.cfg.owner_code_ttl_seconds + 1)
    res = plane.accept_next_step(str(project), owner_code=code)
    assert res["ok"] is False and res["detail"] == "no_pending_challenge"
    record = json.loads((plane.cfg.hai_home / "owner" / "challenges" / f"{pending['challenge_id']}.json").read_text())
    assert record["status"] == "expired"


def test_malformed_code_is_rejected_without_issuing_or_counting(plane: ControlPlane, project: Path) -> None:
    _propose(plane, project)
    res = plane.accept_next_step(str(project), owner_code="abc")
    assert res["ok"] is False and res["detail"] == "malformed_owner_code"
    assert plane.status()["pending_owner_challenges"] == 0
    assert not (plane.cfg.hai_home / "owner" / "challenges").exists()


def test_owner_home_inside_hai_home_is_refused(tmp_path: Path, project: Path) -> None:
    home = tmp_path / "hai_home"
    plane = ControlPlane(Config(hai_home=home, owner_home=home / "owner-codes"))
    _propose(plane, project)
    res = plane.accept_next_step(str(project))
    assert res["ok"] is False and res["error"] == "owner_channel_unavailable"
    assert "inside HAI_HOME" in res["message"]
    assert not (home / "owner-codes").exists()
    assert not (project / "Projek-Managment" / "NEXT_STEP.md").exists()


def test_one_code_passes_exactly_once_under_concurrency(plane: ControlPlane, project: Path) -> None:
    _propose(plane, project)
    code = _owner_relays(plane, plane.accept_next_step(str(project)))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: plane.accept_next_step(str(project), owner_code=code), range(4)))
    assert sum(1 for r in results if r["ok"] is True) == 1


# --- recontract / abandon ------------------------------------------------------


def test_recontract_shows_diff_then_requires_code(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    args = dict(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        reason="narrow scope",
        changes={"objective": "Only the classifier tests"},
    )
    pending = plane.recontract(**args, owner_ack=True)
    assert pending["ok"] is False and pending["status"] == "pending_owner_code"
    assert pending["diff"] and pending["diff"][0]["field"] == "objective"
    owner_text = (plane.cfg.resolved_owner_home() / f"{pending['challenge_id']}.txt").read_text(encoding="utf-8")
    assert "Only the classifier tests" in owner_text  # the owner sees the diff
    applied = plane.recontract(**args, owner_code=_owner_relays(plane, pending))
    assert applied["ok"] is True and applied["contract_version"] == 2
    assert applied["owner_gate"]["challenge_id"] == pending["challenge_id"]


def test_recontract_code_is_bound_to_the_diff(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    base = dict(mission_id=opened["mission_id"], contract_version=opened["contract_version"], reason="narrow")
    pending = plane.recontract(**base, changes={"objective": "Only tests"})
    code = _owner_relays(plane, pending)
    swapped = plane.recontract(**base, changes={"non_goals": []}, owner_code=code)
    assert swapped["ok"] is False and swapped["detail"] == "no_pending_challenge"
    assert plane.mission.load_mission_meta(opened["mission_id"])["current_version"] == 1


def test_abandon_requires_code(plane: ControlPlane, project: Path) -> None:
    opened = _open(plane, project)
    args = dict(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        evidence=None,
        outcome_summary="scope no longer needed",
        closure="abandoned",
    )
    pending = plane.close_mission(**args, owner_ack=True)
    assert pending["ok"] is False and pending["status"] == "pending_owner_code"
    assert plane.mission.load_mission_meta(opened["mission_id"])["status"] == "active"
    done = plane.close_mission(**args, owner_code=_owner_relays(plane, pending))
    assert done["ok"] is True and done["status"] == "abandoned"
    assert done["owner_gate"]["challenge_id"] == pending["challenge_id"]


# --- channels, health, audit ----------------------------------------------------


def test_ntfy_channel_posts_the_code_and_fails_closed(tmp_path: Path, project: Path) -> None:
    captured: list[Any] = []

    class _Resp:
        status = 200

        def __enter__(self) -> "_Resp":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    def fake_urlopen(request: Any, timeout: float) -> _Resp:
        captured.append(request)
        return _Resp()

    cfg = Config(hai_home=tmp_path / "hai_home", owner_channel="ntfy", owner_ntfy_topic="hai-x9q2", owner_ntfy_token="tok")
    plane = ControlPlane(cfg)
    plane.mission.owner_gate.opener = fake_urlopen
    _propose(plane, project)
    pending = plane.accept_next_step(str(project))
    assert pending["channel"] == "ntfy"
    req = captured[0]
    assert req.full_url == "https://ntfy.sh/hai-x9q2" and req.get_method() == "POST"
    assert req.get_header("Authorization") == "Bearer tok"
    body = req.data.decode("utf-8")
    code = CODE_RE.search(body).group(1)
    assert _scan_for(plane.cfg.hai_home, code) == []
    assert plane.accept_next_step(str(project), owner_code=code)["ok"] is True

    # delivery failure → no live challenge, gate stays closed
    def broken_urlopen(request: Any, timeout: float) -> _Resp:
        raise urllib.error.URLError("no network")

    plane.mission.owner_gate.opener = broken_urlopen
    _propose(plane, project, "# second\n")
    res = plane.accept_next_step(str(project))
    assert res["ok"] is False and res["error"] == "owner_channel_unavailable"
    assert plane.status()["pending_owner_challenges"] == 0


def test_ntfy_without_topic_fails_closed(tmp_path: Path, project: Path) -> None:
    plane = ControlPlane(Config(hai_home=tmp_path / "hai_home", owner_channel="ntfy"))
    _propose(plane, project)
    res = plane.accept_next_step(str(project))
    assert res["error"] == "owner_channel_unavailable" and "HAI_OWNER_NTFY_TOPIC" in res["message"]


def test_health_reports_the_gate_and_never_touches_owner_home_by_itself(plane: ControlPlane) -> None:
    health = plane.health()
    assert health["owner_gate"]["mode"] == "nonce"
    assert health["owner_gate"]["channel"] == "file"
    assert health["owner_gate"]["owner_home"] == str(plane.cfg.resolved_owner_home())
    assert not plane.cfg.resolved_owner_home().exists()  # created only on first delivery


def test_legacy_mode_is_explicit_and_flagged(tmp_path: Path, project: Path) -> None:
    plane = ControlPlane(Config(hai_home=tmp_path / "hai_home", owner_gate="ack_legacy"))
    assert "honor system" in plane.health()["owner_gate"]["warning"]
    _propose(plane, project)
    assert plane.accept_next_step(str(project), owner_ack=True, reason="legacy")["ok"] is True


def test_from_env_unknown_gate_falls_back_to_nonce(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HAI_HOME", str(tmp_path / "h"))
    monkeypatch.setenv("HAI_OWNER_GATE", "trust-me")
    cfg = Config.from_env()
    assert cfg.owner_gate == "nonce" and any("HAI_OWNER_GATE" in w for w in cfg.warnings)
    monkeypatch.setenv("HAI_OWNER_GATE", "ack")
    assert Config.from_env().owner_gate == "ack_legacy"


def test_audit_trail_records_the_gate_but_never_the_code(plane: ControlPlane, project: Path) -> None:
    _propose(plane, project)
    pending = plane.accept_next_step(str(project))
    code = _owner_relays(plane, pending)
    assert plane.accept_next_step(str(project), owner_code=code)["ok"] is True
    events = [
        json.loads(p.read_text(encoding="utf-8"))["event_type"]
        for p in sorted(plane.mission.audit_dir.glob("A-*.json"))
    ]
    assert "owner_challenge_issued" in events and "owner_gate_passed" in events
    assert _scan_for(plane.mission.audit_dir, code) == []
