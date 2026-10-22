"""Touch ID owner channel: the MCP client cannot approve a gated action alone.

The biometric helper is mocked. These tests never call LocalAuthentication — a real
prompt cannot run in CI. On the owner's Mac, run ``scripts/manual_touchid.py``.
"""

from __future__ import annotations

import hashlib
import json
import stat
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from hai_mcp import owner_gate as og
from hai_mcp.config import OWNER_CHANNELS, Config
from hai_mcp.paths import require_project_path
from hai_mcp.state import ControlPlane

BODY = "# do the thing\n"


def _ok(payload: dict[str, Any], *, method: str = "touchid") -> dict[str, Any]:
    return {
        "ok": True,
        "fingerprint": payload["fingerprint"],
        "reason": payload["reason"],
        "method": method,
    }


def _denied(payload: dict[str, Any], error: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": error,
        "fingerprint": payload["fingerprint"],
        "reason": payload["reason"],
    }


def _plane(
    tmp_path: Path,
    evaluate: Any = None,
    *,
    fallback: str | None = None,
    helper: Path | None = None,
    timeout: int = 60,
    ttl: int = 600,
) -> ControlPlane:
    cfg = Config(
        hai_home=tmp_path / "hai_home",
        owner_home=tmp_path / "owner_home",
        owner_channel="touchid",
        owner_touchid_fallback=fallback,
        owner_touchid_helper=helper,
        owner_touchid_timeout_seconds=timeout,
        owner_code_ttl_seconds=ttl,
    )
    plane = ControlPlane(cfg)
    plane.mission.owner_gate.touchid_evaluate = evaluate
    return plane


def _canonical(project: Path) -> Path:
    return project / "Projek-Managment" / "NEXT_STEP.md"


def _accept(plane: ControlPlane, project: Path, content: str = BODY, **kwargs: Any) -> dict[str, Any]:
    return plane.accept_next_step(str(project), content=content, **kwargs)


def _record(plane: ControlPlane, challenge_id: str) -> dict[str, Any]:
    path = plane.cfg.hai_home / "owner" / "challenges" / f"{challenge_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _tree(root: Path) -> str:
    if not root.exists():
        return ""
    parts: list[str] = []
    for path in root.rglob("*"):
        if path.is_file():
            parts.append(path.read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(parts)


def _assert_no_code_material(plane: ControlPlane, *payloads: Any) -> None:
    blob = json.dumps(payloads, ensure_ascii=False)
    assert "HAI owner code:" not in blob
    assert "code_hash" not in blob
    stored = _tree(plane.cfg.hai_home)
    assert "HAI owner code:" not in stored
    assert "code_hash" not in stored
    assert "salt" not in stored


@pytest.fixture
def project(tmp_path: Path) -> Path:
    path = tmp_path / "proj"
    (path / "Projek-Managment").mkdir(parents=True)
    (path / "src").mkdir()
    (path / "tests").mkdir()
    (path / "tests" / "out.md").write_text("done\n", encoding="utf-8")
    return path


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


def test_channels_include_touchid() -> None:
    assert OWNER_CHANNELS == frozenset({"file", "ntfy", "touchid"})


def test_success_records_approval_then_follow_up_applies_once(
    tmp_path: Path, project: Path
) -> None:
    calls: list[dict[str, Any]] = []

    def evaluate(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload)
        assert set(payload) == {"reason", "fingerprint", "timeout_seconds"}
        assert "code" not in payload
        return _ok(payload)

    plane = _plane(tmp_path, evaluate)
    pending = _accept(plane, project, owner_ack=True, reason="please")
    assert pending["ok"] is False
    assert pending["error"] == "owner_gate_required"
    assert pending["status"] == "owner_approved"
    assert pending["channel"] == "touchid"
    assert not _canonical(project).exists()
    assert plane.status()["pending_owner_challenges"] == 1
    assert len(calls) == 1
    reason = calls[0]["reason"]
    assert "accept_next_step" in reason
    assert project.name in reason
    assert "do the thing" in reason
    assert calls[0]["fingerprint"].removeprefix("sha256:")[:12] in reason
    assert len(reason) <= 120
    record = _record(plane, pending["challenge_id"])
    assert record["status"] == "approved" and record["approval"] == "touchid"
    assert "code_hash" not in record and "salt" not in record

    applied = _accept(plane, project, owner_code="2222-3333")
    assert applied["ok"] is True
    assert applied["owner_gate"]["channel"] == "touchid"
    assert applied["owner_gate"]["challenge_id"] == pending["challenge_id"]
    assert len(calls) == 1  # the follow-up does not prompt again
    text = _canonical(project).read_text(encoding="utf-8")
    assert "do the thing" in text and "Touch ID" in text and "gate: nonce" in text
    assert plane.status()["pending_owner_challenges"] == 0
    _assert_no_code_material(plane, pending, applied)

    events = [
        json.loads(path.read_text(encoding="utf-8"))["event_type"]
        for path in plane.mission.audit_dir.glob("A-*.json")
    ]
    assert "owner_challenge_issued" in events and "owner_gate_passed" in events
    assert events.count("owner_gate_passed") == 1


def test_replay_needs_a_new_biometric(tmp_path: Path, project: Path) -> None:
    calls = {"n": 0}

    def evaluate(payload: dict[str, Any]) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 1:
            return _ok(payload)
        return _denied(payload, "user_cancel")

    plane = _plane(tmp_path, evaluate)
    assert _accept(plane, project)["status"] == "owner_approved"
    assert _accept(plane, project)["ok"] is True
    assert calls["n"] == 1

    # Forging the consumed record back to approved must not redeem it.
    record_path = next((plane.cfg.hai_home / "owner" / "challenges").glob("C-*.json"))
    forged = json.loads(record_path.read_text(encoding="utf-8"))
    forged["status"] = "approved"
    record_path.write_text(json.dumps(forged), encoding="utf-8")

    again = _accept(plane, project)
    assert again["ok"] is False and again["error"] == "owner_denied"
    assert calls["n"] == 2
    passed = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in plane.mission.audit_dir.glob("A-*.json")
        if json.loads(path.read_text(encoding="utf-8"))["event_type"] == "owner_gate_passed"
    ]
    assert len(passed) == 1


def test_disk_approval_from_another_process_does_not_pass(tmp_path: Path, project: Path) -> None:
    plane = _plane(tmp_path, _ok)
    pending = _accept(plane, project)
    assert pending["status"] == "owner_approved"

    calls = {"n": 0}

    def cancel(payload: dict[str, Any]) -> dict[str, Any]:
        calls["n"] += 1
        return _denied(payload, "user_cancel")

    other = ControlPlane(plane.cfg)
    other.mission.owner_gate.touchid_evaluate = cancel
    replay = _accept(other, project)
    assert calls["n"] == 1  # disk flag was not a credential; the helper ran again
    assert replay["ok"] is False and replay["error"] == "owner_denied"
    assert not _canonical(project).exists()


def test_changed_body_needs_a_new_approval(tmp_path: Path, project: Path) -> None:
    plane = _plane(tmp_path, _ok)
    first = _accept(plane, project, "# version A\n")
    assert first["status"] == "owner_approved"
    swapped = _accept(plane, project, "# version B — rm -rf\n")
    assert swapped["status"] == "owner_approved"
    assert swapped["challenge_id"] != first["challenge_id"]
    assert swapped["fingerprint"] != first["fingerprint"]
    assert not _canonical(project).exists()

    applied = _accept(plane, project, "# version A\n")
    assert applied["ok"] is True
    assert "version A" in _canonical(project).read_text(encoding="utf-8")
    assert "rm -rf" not in _canonical(project).read_text(encoding="utf-8")


def test_fingerprint_mismatch_fails_closed(tmp_path: Path, project: Path) -> None:
    def evaluate(payload: dict[str, Any]) -> dict[str, Any]:
        lied = _ok(payload)
        lied["fingerprint"] = "sha256:" + ("ab" * 32)
        return lied

    plane = _plane(tmp_path, evaluate)
    denied = _accept(plane, project)
    assert denied["ok"] is False
    assert denied["error"] == "owner_denied" and denied["detail"] == "fingerprint_mismatch"
    assert not _canonical(project).exists()
    assert plane.status()["pending_owner_challenges"] == 0
    # The mismatch did not leave an approval the next call can spend.
    again = _accept(plane, project)
    assert again["ok"] is False and not _canonical(project).exists()
    _assert_no_code_material(plane, denied, again)


def test_reason_mismatch_fails_closed(tmp_path: Path, project: Path) -> None:
    def evaluate(payload: dict[str, Any]) -> dict[str, Any]:
        lied = _ok(payload)
        lied["reason"] = "approve something else"
        return lied

    plane = _plane(tmp_path, evaluate)
    denied = _accept(plane, project)
    assert denied["error"] == "owner_denied" and denied["detail"] == "reason_mismatch"
    assert not _canonical(project).exists()


def test_cancel_keeps_the_gate_closed(tmp_path: Path, project: Path) -> None:
    plane = _plane(tmp_path, lambda payload: _denied(payload, "user_cancel"), fallback="file")
    denied = _accept(plane, project, owner_ack=True, owner_code="K7F3-M9QW")
    assert denied["error"] == "owner_denied" and denied["detail"] == "user_cancel"
    assert denied["channel"] == "touchid"
    assert not _canonical(project).exists()
    assert plane.status()["pending_owner_challenges"] == 0
    assert list(plane.cfg.resolved_owner_home().glob("C-*.txt")) == []
    _assert_no_code_material(plane, denied)


def test_timeout_does_not_fall_back_to_file(tmp_path: Path, project: Path) -> None:
    plane = _plane(tmp_path, lambda payload: _denied(payload, "timeout"), fallback="file")
    denied = _accept(plane, project)
    assert denied["error"] == "owner_denied" and denied["detail"] == "timeout"
    assert not _canonical(project).exists()
    assert list(plane.cfg.resolved_owner_home().glob("C-*.txt")) == []
    assert plane.status()["pending_owner_challenges"] == 0


def test_unavailable_without_fallback_stays_closed(tmp_path: Path, project: Path) -> None:
    plane = _plane(tmp_path, lambda payload: _denied(payload, "unavailable"))
    denied = _accept(plane, project)
    assert denied["error"] == "owner_channel_unavailable"
    assert denied["detail"] == "biometry_unavailable"
    assert not _canonical(project).exists()
    assert plane.status()["pending_owner_challenges"] == 0
    assert list(plane.cfg.resolved_owner_home().glob("C-*.txt")) == []


def test_unavailable_falls_back_to_file_only_when_configured(tmp_path: Path, project: Path) -> None:
    plane = _plane(tmp_path, lambda payload: _denied(payload, "unavailable"), fallback="file")
    pending = _accept(plane, project)
    assert pending["ok"] is False
    assert pending["status"] == "pending_owner_code"
    assert pending["channel"] == "file" and pending["fallback_from"] == "touchid"
    owner_file = plane.cfg.resolved_owner_home() / f"{pending['challenge_id']}.txt"
    text = owner_file.read_text(encoding="utf-8")
    assert owner_file.stat().st_mode & 0o777 == 0o600
    code = text.split("HAI owner code:", 1)[1].splitlines()[0].strip()
    assert code not in _tree(plane.cfg.hai_home)
    assert "code_hash" not in _tree(plane.mission.audit_dir)
    assert "do the thing" in text

    applied = _accept(plane, project, owner_code=code.lower().replace("-", " "))
    assert applied["ok"] is True
    assert "do the thing" in _canonical(project).read_text(encoding="utf-8")


def test_expired_approval_is_not_redeemed(tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def evaluate(payload: dict[str, Any]) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 1:
            return _ok(payload)
        return _denied(payload, "user_cancel")

    plane = _plane(tmp_path, evaluate, ttl=60)
    pending = _accept(plane, project)
    assert pending["status"] == "owner_approved"
    record = _record(plane, pending["challenge_id"])
    real = og.time.time
    monkeypatch.setattr(og.time, "time", lambda: real() + plane.cfg.owner_code_ttl_seconds + 5)
    denied = _accept(plane, project)
    assert denied["ok"] is False and denied["error"] == "owner_denied"
    assert not _canonical(project).exists()
    assert _record(plane, pending["challenge_id"])["status"] == "expired" or record["challenge_id"] != denied.get(
        "challenge_id"
    )
    expired = json.loads(
        (plane.cfg.hai_home / "owner" / "challenges" / f"{pending['challenge_id']}.json").read_text(encoding="utf-8")
    )
    assert expired["status"] == "expired"


def test_recontract_and_abandon_are_bound_to_the_change(tmp_path: Path, project: Path) -> None:
    seen: list[str] = []

    def evaluate(payload: dict[str, Any]) -> dict[str, Any]:
        seen.append(payload["reason"])
        return _ok(payload)

    plane = _plane(tmp_path, evaluate)
    opened = _open(plane, project)
    base = dict(
        mission_id=opened["mission_id"],
        contract_version=opened["contract_version"],
        reason="narrow scope",
    )
    first = plane.recontract(**base, changes={"objective": "Only the classifier tests"})
    assert first["status"] == "owner_approved" and first["channel"] == "touchid"
    assert any(item["field"] == "objective" for item in first["diff"])
    assert plane.mission.load_mission_meta(opened["mission_id"])["current_version"] == 1
    assert "recontract" in seen[-1] and "diff:" in seen[-1] and "fp " in seen[-1]

    swapped = plane.recontract(**base, changes={"objective": "Wipe the archive"})
    assert swapped["status"] == "owner_approved"
    assert swapped["challenge_id"] != first["challenge_id"]
    assert plane.mission.load_mission_meta(opened["mission_id"])["current_version"] == 1

    applied = plane.recontract(**base, changes={"objective": "Only the classifier tests"})
    assert applied["ok"] is True and applied["contract_version"] == 2
    assert applied["owner_gate"]["channel"] == "touchid"
    assert plane.mission.load_contract(opened["mission_id"], 2)["objective"] == "Only the classifier tests"

    abandon_args = dict(
        mission_id=opened["mission_id"],
        contract_version=2,
        evidence=None,
        outcome_summary="scope no longer needed",
        closure="abandoned",
    )
    parked = plane.close_mission(**abandon_args, owner_ack=True)
    assert parked["status"] == "owner_approved"
    assert "abandon_mission" in seen[-1] and "scope no longer needed" in seen[-1]
    assert plane.mission.load_mission_meta(opened["mission_id"])["status"] == "active"
    done = plane.close_mission(**abandon_args)
    assert done["ok"] is True and done["status"] == "abandoned"
    assert done["owner_gate"]["channel"] == "touchid"
    _assert_no_code_material(plane, first, swapped, applied, parked, done)


def test_helper_method_is_sanitized_before_audit(tmp_path: Path, project: Path) -> None:
    def evaluate(payload: dict[str, Any]) -> dict[str, Any]:
        return _ok(payload, method="touchid; rm -rf /")

    plane = _plane(tmp_path, evaluate)
    pending = _accept(plane, project)
    record = _record(plane, pending["challenge_id"])
    assert record["delivery"]["method"] == "device_owner"
    audit = _tree(plane.mission.audit_dir)
    assert "rm -rf" not in audit
    assert "touchid_method" in audit and "device_owner" in audit


def test_health_reports_touchid_without_prompting(tmp_path: Path) -> None:
    def evaluate(payload: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError(f"health must not prompt: {payload}")

    plane = _plane(tmp_path, evaluate)
    health = plane.health()
    info = health["owner_gate"]
    assert info["channel"] == "touchid"
    assert info["touchid_timeout_seconds"] == 60
    assert info["touchid_fallback"] is None
    assert info["touchid_platform_ok"] is og._touchid_platform_ok()
    assert str(info["touchid_helper"]).endswith("touchid_prompt.swift")
    assert plane.status()["pending_owner_challenges"] == 0


def test_not_macos_fails_closed_unless_file_fallback_is_configured(
    tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(og, "_touchid_platform_ok", lambda: False)
    plane = _plane(tmp_path, evaluate=None)
    denied = _accept(plane, project)
    assert denied["error"] == "owner_channel_unavailable" and denied["detail"] == "not_macos"
    assert not _canonical(project).exists()

    fallback = _plane(tmp_path / "fb", evaluate=None, fallback="file")
    # _plane uses tmp_path / "hai_home"; give the second plane its own root via a nested dir.
    # The factory always uses the path it is given as the parent of hai_home.
    pending = _accept(fallback, project)
    assert pending["status"] == "pending_owner_code" and pending["fallback_from"] == "touchid"
    assert not _canonical(project).exists()


def test_missing_untrusted_and_swiftless_helper_fail_closed(
    tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(og, "_touchid_platform_ok", lambda: True)

    missing = _plane(tmp_path / "missing", helper=tmp_path / "no-such-helper")
    denied = _accept(missing, project)
    assert denied["error"] == "owner_channel_unavailable" and denied["detail"] == "helper_missing"
    assert not _canonical(project).exists()

    home = tmp_path / "inside" / "hai_home"
    helper = home / "evil-touchid"
    helper.parent.mkdir(parents=True)
    helper.write_text("#!/bin/sh\necho ran > \"$(dirname \"$0\")/did-run\"\n", encoding="utf-8")
    helper.chmod(0o755)
    untrusted = ControlPlane(
        Config(hai_home=home, owner_home=tmp_path / "inside_owner", owner_channel="touchid", owner_touchid_helper=helper)
    )
    denied = _accept(untrusted, project)
    assert denied["error"] == "owner_channel_unavailable" and denied["detail"] == "helper_untrusted"
    assert not (home / "did-run").exists()

    monkeypatch.setattr(og.shutil, "which", lambda _cmd: None)
    swiftless = _plane(tmp_path / "swiftless")
    denied = _accept(swiftless, project)
    assert denied["detail"] == "helper_missing"
    assert "swift" in denied["message"]


def _write_fake_helper(path: Path) -> None:
    path.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json, os, sys, time
            req = json.load(sys.stdin)
            mode = os.environ.get("FAKE_TOUCHID_MODE", "ok")
            log = os.environ.get("FAKE_TOUCHID_LOG")
            if log:
                with open(log, "a", encoding="utf-8") as handle:
                    handle.write(mode + "\\n")
            fp = req.get("fingerprint", "")
            reason = req.get("reason", "")
            if mode == "mismatch":
                body, code = {"ok": True, "fingerprint": "sha256:" + ("ab" * 32), "reason": reason, "method": "touchid"}, 0
            elif mode == "cancel":
                body, code = {"ok": False, "error": "user_cancel", "fingerprint": fp, "reason": reason}, 1
            elif mode == "unavailable":
                body, code = {"ok": False, "error": "unavailable", "fingerprint": fp, "reason": reason}, 3
            elif mode == "timeout":
                time.sleep(30)
                body, code = {"ok": False, "error": "timeout", "fingerprint": fp, "reason": reason}, 2
            elif mode == "lie-ok":
                body, code = {"ok": True, "fingerprint": fp, "reason": reason, "method": "touchid"}, 1
            elif mode == "garbage":
                sys.stdout.write("not-json")
                raise SystemExit(0)
            else:
                body, code = {"ok": True, "fingerprint": fp, "reason": reason, "method": "touchid"}, 0
            json.dump(body, sys.stdout)
            sys.stdout.write("\\n")
            raise SystemExit(code)
            """
        ),
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def test_helper_protocol_subprocess(
    tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The server spawns the helper and trusts only exit 0 plus a matching attestation."""
    monkeypatch.setattr(og, "_touchid_platform_ok", lambda: True)
    helper = tmp_path / "fake-touchid"
    log = tmp_path / "helper.log"
    _write_fake_helper(helper)
    monkeypatch.setenv("FAKE_TOUCHID_LOG", str(log))

    def run(mode: str, *, timeout: int = 60) -> tuple[ControlPlane, dict[str, Any], str]:
        monkeypatch.setenv("FAKE_TOUCHID_MODE", mode)
        root = tmp_path / mode
        plane = _plane(root, evaluate=None, helper=helper, timeout=timeout)
        content = f"# helper-mode {mode}\n"
        return plane, _accept(plane, project, content), content

    plane, pending, content = run("ok")
    assert pending["status"] == "owner_approved"
    assert log.read_text(encoding="utf-8").strip() == "ok"
    applied = _accept(plane, project, content)
    assert applied["ok"] is True
    assert content.strip() in _canonical(project).read_text(encoding="utf-8")
    assert log.read_text(encoding="utf-8").strip() == "ok"  # follow-up did not spawn the helper

    _, denied, content = run("mismatch")
    assert denied["error"] == "owner_denied" and denied["detail"] == "fingerprint_mismatch"
    assert content.strip() not in _canonical(project).read_text(encoding="utf-8")

    _, denied, content = run("cancel")
    assert denied["error"] == "owner_denied" and denied["detail"] == "user_cancel"
    assert content.strip() not in _canonical(project).read_text(encoding="utf-8")

    _, denied, content = run("unavailable")
    assert denied["error"] == "owner_channel_unavailable" and denied["detail"] == "biometry_unavailable"
    assert content.strip() not in _canonical(project).read_text(encoding="utf-8")

    _, denied, content = run("lie-ok")
    assert denied["ok"] is False and denied["error"] == "owner_denied"
    assert denied.get("status") != "owner_approved"
    assert content.strip() not in _canonical(project).read_text(encoding="utf-8")

    _, denied, content = run("garbage")
    assert denied["error"] == "owner_channel_unavailable"
    assert content.strip() not in _canonical(project).read_text(encoding="utf-8")

    _, denied, content = run("timeout", timeout=1)
    assert denied["error"] == "owner_denied" and denied["detail"] == "timeout"
    assert content.strip() not in _canonical(project).read_text(encoding="utf-8")


def test_from_env_touchid_and_bad_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HAI_HOME", str(tmp_path / "h"))
    monkeypatch.setenv("HAI_OWNER_CHANNEL", "touchid")
    monkeypatch.setenv("HAI_OWNER_TOUCHID_TIMEOUT", "45")
    monkeypatch.setenv("HAI_OWNER_TOUCHID_FALLBACK", "file")
    monkeypatch.setenv("HAI_OWNER_TOUCHID_HELPER", str(tmp_path / "hai-touchid"))
    cfg = Config.from_env()
    assert cfg.owner_channel == "touchid"
    assert cfg.owner_touchid_timeout_seconds == 45
    assert cfg.owner_touchid_fallback == "file"
    assert cfg.owner_touchid_helper == (tmp_path / "hai-touchid").resolve()
    if sys.platform != "darwin":
        assert any("macOS" in warning for warning in cfg.warnings)

    monkeypatch.setenv("HAI_OWNER_TOUCHID_FALLBACK", "ntfy")
    cfg = Config.from_env()
    assert cfg.owner_touchid_fallback is None
    assert any("HAI_OWNER_TOUCHID_FALLBACK" in warning for warning in cfg.warnings)

    monkeypatch.setenv("HAI_OWNER_TOUCHID_FALLBACK", "")
    monkeypatch.setenv("HAI_OWNER_TOUCHID_TIMEOUT", "2")
    cfg = Config.from_env()
    assert cfg.owner_touchid_timeout_seconds == 5
    assert any("HAI_OWNER_TOUCHID_TIMEOUT" in warning for warning in cfg.warnings)

    monkeypatch.setenv("HAI_OWNER_CHANNEL", "carrier-pigeon")
    cfg = Config.from_env()
    assert cfg.owner_channel == "file"
    assert any("HAI_OWNER_CHANNEL" in warning for warning in cfg.warnings)


def test_reason_truncates_a_long_diff() -> None:
    preview = {"diff": "x" * 500, "project_path": "/work/proj"}
    reason = og.build_touchid_reason("recontract", "a long summary", preview, "sha256:" + "cd" * 32)
    assert len(reason) <= 120
    assert reason.startswith("recontract · proj")
    assert "fp " + ("cd" * 6) in reason  # first 12 hex chars of the fingerprint
    assert "diff:" in reason


def test_body_sha_binding_uses_the_same_fingerprint_as_the_gate(tmp_path: Path, project: Path) -> None:
    """A hand-edited challenge fingerprint cannot be spent on a different body."""
    plane = _plane(tmp_path, _ok)
    pending = _accept(plane, project, "# version A\n")
    assert pending["status"] == "owner_approved"
    body = "# version B\n"
    project_s = str(require_project_path(str(project)))
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    forged_fp = og.fingerprint({"project_path": project_s, "body_sha256": digest})
    record = _record(plane, pending["challenge_id"])
    record["fingerprint"] = forged_fp
    record["preview"]["next_step"] = body.strip()
    path = plane.cfg.hai_home / "owner" / "challenges" / f"{pending['challenge_id']}.json"
    path.write_text(json.dumps(record), encoding="utf-8")

    calls = {"n": 0}

    def cancel(payload: dict[str, Any]) -> dict[str, Any]:
        calls["n"] += 1
        return _denied(payload, "user_cancel")

    plane.mission.owner_gate.touchid_evaluate = cancel
    denied = _accept(plane, project, body)
    assert denied["ok"] is False and denied["error"] == "owner_denied"
    assert calls["n"] == 1
    assert not _canonical(project).exists()
    retired = _record(plane, pending["challenge_id"])
    assert retired["status"] == "cancelled" and retired["cancel_reason"] == "approval_not_in_process"
