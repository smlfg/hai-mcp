from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from hai_mcp.build_info import describe_build
from hai_mcp.config import Config
from hai_mcp.state import ControlPlane

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "a.txt").write_text("one\n")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-q", "-m", "one")
    _git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    return root


def _commit(repo: Path, name: str) -> None:
    (repo / name).write_text(name)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", name)


@needs_git
def test_clean_main_has_no_warnings(repo: Path) -> None:
    info = describe_build(repo)
    assert info["source"] == "git"
    assert info["branch"] == "main"
    assert info["detached"] is False
    assert info["dirty"] is False
    assert (info["ahead"], info["behind"]) == (0, 0)
    assert info["warnings"] == []


@needs_git
def test_detached_and_dirty_checkout_is_called_out(repo: Path) -> None:
    _git(repo, "checkout", "-q", "--detach")
    (repo / "a.txt").write_text("changed\n")
    info = describe_build(repo)
    assert info["detached"] is True
    assert info["branch"] is None
    assert info["dirty"] is True and info["dirty_files"] == 1
    text = " ".join(info["warnings"])
    assert "detached HEAD" in text
    assert "1 uncommitted change" in text


@needs_git
def test_feature_branch_ahead_and_behind_origin_main(repo: Path) -> None:
    _git(repo, "checkout", "-q", "-b", "feature")
    _commit(repo, "f1")
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "m1")
    _commit(repo, "m2")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "checkout", "-q", "feature")
    info = describe_build(repo)
    assert (info["ahead"], info["behind"]) == (1, 2)
    text = " ".join(info["warnings"])
    assert "branch 'feature', not main" in text
    assert "2 commit(s) behind origin/main" in text
    assert "1 commit(s) not in origin/main" in text


@needs_git
def test_missing_reference_is_reported_not_raised(repo: Path) -> None:
    _git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    info = describe_build(repo)
    assert info["reference_missing"] is True
    assert any("not known locally" in w for w in info["warnings"])


@needs_git
def test_plain_directory_is_an_installed_package(tmp_path: Path) -> None:
    info = describe_build(tmp_path)
    assert info["source"] == "package"
    assert info["warnings"] == []


def test_missing_git_binary_never_raises(tmp_path: Path) -> None:
    def runner(args: Sequence[str]) -> str:
        raise FileNotFoundError("git")

    info = describe_build(tmp_path, runner=runner)
    assert info == {"source": "unknown", "error": "git_not_found", "code_root": str(tmp_path)}


def test_git_timeout_never_raises(tmp_path: Path) -> None:
    calls: list[Sequence[str]] = []

    def runner(args: Sequence[str]) -> str:
        calls.append(args)
        if args[0] == "rev-parse" and args[1] == "--is-inside-work-tree":
            return "true"
        raise subprocess.TimeoutExpired(cmd="git", timeout=2)

    info = describe_build(tmp_path, runner=runner)
    assert info["source"] == "unknown"
    assert "TimeoutExpired" in info["error"]


def test_health_reports_build(tmp_path: Path) -> None:
    plane = ControlPlane(Config.from_env())
    build = plane.health()["build"]
    assert build["source"] in {"git", "package", "unknown"}
    if build["source"] == "git":
        assert isinstance(build["warnings"], list)
