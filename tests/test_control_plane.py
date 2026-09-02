from __future__ import annotations

import json
from pathlib import Path

import pytest

from hai_mcp.config import Config
from hai_mcp.state import ControlPlane


@pytest.fixture
def plane(tmp_path: Path) -> ControlPlane:
    home = tmp_path / "hai_home"
    # These tests exercise the legacy honor-system gate explicitly; the default gate is
    # 'nonce' and is covered by tests/test_owner_nonce_gate.py.
    return ControlPlane(Config(hai_home=home, owner_gate="ack_legacy"))


@pytest.fixture
def project(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    p.mkdir()
    return p


def test_health(plane: ControlPlane, project: Path) -> None:
    r = plane.health(str(project))
    assert r["ok"] is True
    assert r["model_calls"] is False
    assert r["hai_home_writable"] is True
    assert r["project_path"] == str(project.resolve())


def test_park_does_not_steal_lane(plane: ControlPlane) -> None:
    plane.set_focus("lane-a", label="A")
    before = plane.status()
    r = plane.park("meta idea about better prompts", tags=["meta"])
    assert r["ok"] is True
    assert r["active_unchanged"] is True
    after = plane.status()
    assert after["active_count"] == before["active_count"] == 1
    assert after["inbox_count"] == 1


def test_max_two_active_lanes(plane: ControlPlane, project: Path) -> None:
    assert plane.set_focus("a", project_path=str(project))["ok"] is True
    assert plane.set_focus("b", project_path=str(project))["ok"] is True
    r = plane.set_focus("c", project_path=str(project))
    assert r["ok"] is False
    assert r["error"] == "max_active_lanes"


@pytest.mark.parametrize("ack", [False, None, 0, 1, "true"])
def test_accept_next_step_requires_literal_owner_ack_true(
    plane: ControlPlane, project: Path, ack: object
) -> None:
    plane.propose_next_step(str(project), "# gated step\n")
    r = plane.accept_next_step(str(project), owner_ack=ack, reason="try")  # type: ignore[arg-type]
    assert r["ok"] is False
    assert r["error"] == "owner_gate_required"
    assert not (project / "Projek-Managment" / "NEXT_STEP.md").is_file()


def test_propose_and_accept_gate(plane: ControlPlane, project: Path) -> None:
    content = (
        "# NEXT_STEP\n\n"
        "- goal: write smoke test\n"
        "- allowed: tests only\n"
        "- success: pytest green\n"
    )
    prop = plane.propose_next_step(str(project), content)
    assert prop["ok"] is True
    assert (project / "Projek-Managment" / "NEXT_STEP.proposed.md").is_file()

    denied = plane.accept_next_step(str(project), owner_ack=False, reason="nope")
    assert denied["ok"] is False
    assert denied["error"] == "owner_gate_required"
    assert not (project / "Projek-Managment" / "NEXT_STEP.md").exists()

    denied2 = plane.accept_next_step(str(project), owner_ack=True, reason="")
    assert denied2["ok"] is False

    ok = plane.accept_next_step(str(project), owner_ack=True, reason="owner approved smoke")
    assert ok["ok"] is True
    canonical = project / "Projek-Managment" / "NEXT_STEP.md"
    assert canonical.is_file()
    text = canonical.read_text(encoding="utf-8")
    assert "write smoke test" in text
    assert "owner approved smoke" in text
    assert not (project / "Projek-Managment" / "NEXT_STEP.proposed.md").exists()


def test_get_next_step_and_artifacts(plane: ControlPlane, project: Path) -> None:
    empty = plane.get_next_step(str(project))
    assert empty["ok"] is True
    assert empty["exists"] is False

    plane.propose_next_step(str(project), "# step\n")
    plane.accept_next_step(str(project), owner_ack=True, reason="go")
    got = plane.get_next_step(str(project))
    assert got["exists"] is True
    arts = plane.read_artifacts(str(project))
    assert "NEXT_STEP.md" in arts["present"]


def test_checkpoint_and_recover(plane: ControlPlane, project: Path) -> None:
    plane.set_focus("focus-1", project_path=str(project), label="demo")
    plane.propose_next_step(str(project), "# proposed\n")
    cp = plane.checkpoint(note="before risk", project_path=str(project))
    assert cp["ok"] is True
    rec = plane.recover(cp["checkpoint_id"])
    assert rec["ok"] is True
    assert rec["checkpoint_id"] == cp["checkpoint_id"]
    assert "next_action" in rec


def test_missing_project(plane: ControlPlane, tmp_path: Path) -> None:
    r = plane.get_next_step(str(tmp_path / "nope"))
    assert r["ok"] is False
    assert r["error"] == "missing_project"


def test_symlinked_artifact_dir_rejected(plane: ControlPlane, tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    outside = tmp_path / "outside-artifacts"
    outside.mkdir()
    (outside / "NEXT_STEP.md").write_text("# escaped\n", encoding="utf-8")
    (project / "Projek-Managment").symlink_to(outside)

    r = plane.propose_next_step(str(project), "# sneaky step\n")
    assert r["ok"] is False
    assert r["error"] == "path_outside_root"

    ok_project = tmp_path / "safe-proj"
    ok_project.mkdir()
    good = plane.propose_next_step(str(ok_project), "# safe step\n")
    assert good["ok"] is True


def test_symlinked_artifact_dir_rejected_on_read(plane: ControlPlane, tmp_path: Path) -> None:
    project = tmp_path / "proj-read"
    project.mkdir()
    outside = tmp_path / "outside-read"
    outside.mkdir()
    (outside / "NEXT_STEP.md").write_text("# escaped secret\n", encoding="utf-8")
    (project / "Projek-Managment").symlink_to(outside)

    r = plane.read_artifacts(str(project))
    assert r["ok"] is False
    assert r["error"] == "path_outside_root"

    g = plane.get_next_step(str(project))
    assert g["ok"] is False
    assert g["error"] == "path_outside_root"


def test_symlinked_individual_artifact_file_not_followed(plane: ControlPlane, tmp_path: Path) -> None:
    project = tmp_path / "proj-file"
    project.mkdir()
    ad = project / "Projek-Managment"
    ad.mkdir()
    outside = tmp_path / "outside-file"
    outside.mkdir()
    secret = outside / "secret.md"
    secret.write_text("# leaked next step\n", encoding="utf-8")
    secret_before = secret.read_bytes()
    # NEXT_STEP.md is a symlink pointing outside the artifact dir
    (ad / "NEXT_STEP.md").symlink_to(secret)

    g = plane.get_next_step(str(project))
    assert g["ok"] is False
    assert g["error"] == "path_outside_root"

    arts = plane.read_artifacts(str(project))
    # the escaping file must not be surfaced as an artifact
    assert "NEXT_STEP.md" not in arts.get("present", [])

    # checkpoint must also skip the escaping symlink, not snapshot it
    cp = plane.checkpoint(note="risk", project_path=str(project))
    assert cp["ok"] is True
    assert "NEXT_STEP.md" not in cp["meta"].get("artifacts", {})

    assert secret.read_bytes() == secret_before
