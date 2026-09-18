"""Acceptance tests for the blocker detector.

Every test builds a real repository in a temp dir and asserts on the real git
state — the detector's whole value is that it reads the world, so testing it
against a mock would repeat the mistake this project already made twice.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hai_mcp.blocker_detector import (
    DUE_THRESHOLD,
    GitProbeError,
    _git,
    detect,
    rank,
    scan_repo,
    verdicts,
)

GIT_IDENTITY = [
    "-c",
    "user.name=Test",
    "-c",
    "user.email=test@example.invalid",
    "-c",
    "commit.gpgsign=false",
]


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *GIT_IDENTITY, "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout


def git_strict(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *GIT_IDENTITY, "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def write(repo: Path, name: str, text: str) -> None:
    (repo / name).write_text(text, encoding="utf-8")


def commit_all(repo: Path, message: str) -> None:
    git_strict(repo, "add", "-A")
    git_strict(repo, "commit", "-m", message)


@pytest.fixture()
def origin(tmp_path: Path) -> Path:
    """A bare repo that acts as the server."""
    bare = tmp_path / "origin.git"
    bare.mkdir()
    git_strict(bare, "init", "--bare", "--initial-branch=main")
    return bare


@pytest.fixture()
def repo(tmp_path: Path, origin: Path) -> Path:
    """A clone with one pushed commit on main, upstream configured."""
    work = tmp_path / "work"
    work.mkdir()
    git_strict(work, "init", "--initial-branch=main")
    git_strict(work, "remote", "add", "origin", str(origin))
    write(work, "README.md", "start\n")
    commit_all(work, "erster Commit")
    git_strict(work, "push", "-u", "origin", "main")
    return work


def kinds(findings) -> set[str]:
    return {f.kind for f in findings}


# --- the read-only guarantee -------------------------------------------------


def test_refuses_mutating_subcommand(repo: Path) -> None:
    with pytest.raises(GitProbeError) as excinfo:
        _git(repo, ["commit", "-m", "should never run"])
    assert excinfo.value.code == "forbidden_subcommand"


def test_refuses_empty_subcommand(repo: Path) -> None:
    with pytest.raises(GitProbeError) as excinfo:
        _git(repo, [])
    assert excinfo.value.code == "invalid_args"


def test_scan_does_not_change_the_repo(repo: Path) -> None:
    before = git_strict(repo, "rev-parse", "HEAD").strip()
    status_before = git(repo, "status", "--porcelain")
    scan_repo(repo)
    assert git_strict(repo, "rev-parse", "HEAD").strip() == before
    assert git(repo, "status", "--porcelain") == status_before


# --- individual signals ------------------------------------------------------


def test_clean_synced_repo_reports_nothing(repo: Path) -> None:
    findings, error = scan_repo(repo)
    assert error is None
    assert findings == []


def test_branch_without_upstream_is_never_pushed(repo: Path) -> None:
    git_strict(repo, "switch", "-c", "feature/learning-budget")
    write(repo, "budget.py", "credits = 0\n")
    commit_all(repo, "Lernbudget")
    findings, error = scan_repo(repo)
    assert error is None
    assert "never_pushed" in kinds(findings)
    finding = next(f for f in findings if f.kind == "never_pushed")
    assert "feature/learning-budget" in finding.evidence
    assert finding.capability == "git-remote"


def test_commits_ahead_of_upstream(repo: Path) -> None:
    write(repo, "README.md", "start\nmehr\n")
    commit_all(repo, "nicht veröffentlicht")
    findings, _ = scan_repo(repo)
    assert "unpushed_commits" in kinds(findings)


def test_dirty_worktree(repo: Path) -> None:
    write(repo, "README.md", "geändert, nicht festgeschrieben\n")
    findings, _ = scan_repo(repo)
    assert "dirty_worktree" in kinds(findings)


def test_detached_head(repo: Path) -> None:
    head = git_strict(repo, "rev-parse", "HEAD").strip()
    git_strict(repo, "checkout", "--detach", head)
    findings, _ = scan_repo(repo)
    assert "detached_head" in kinds(findings)


def test_stash_backlog(repo: Path) -> None:
    write(repo, "README.md", "zwischengelagert\n")
    git_strict(repo, "stash", "push", "-m", "später")
    findings, _ = scan_repo(repo)
    assert "stash_backlog" in kinds(findings)


def test_stuck_cherry_pick_and_conflicts(repo: Path) -> None:
    """The exact situation sitting on the owner's MacBook."""
    git_strict(repo, "switch", "-c", "side")
    write(repo, "README.md", "seite\n")
    commit_all(repo, "Seitenzweig ändert dieselbe Zeile")
    side_head = git_strict(repo, "rev-parse", "HEAD").strip()

    git_strict(repo, "switch", "main")
    write(repo, "README.md", "haupt\n")
    commit_all(repo, "Hauptzweig ändert dieselbe Zeile")

    # Deliberately provoke the conflict; a failing cherry-pick is the state under test.
    git(repo, "cherry-pick", side_head)

    findings, error = scan_repo(repo)
    assert error is None
    found = kinds(findings)
    assert "in_progress_operation" in found
    assert "unmerged_paths" in found

    top = rank(findings)[0]
    assert top.kind == "in_progress_operation", "a half-finished operation outranks everything"


# --- the due rule ------------------------------------------------------------


def test_one_blocked_artifact_is_only_watched(repo: Path) -> None:
    git_strict(repo, "switch", "-c", "solo")
    write(repo, "a.txt", "a\n")
    commit_all(repo, "solo")
    report_capabilities = verdicts(scan_repo(repo)[0])
    remote = next(c for c in report_capabilities if c.capability == "git-remote")
    assert remote.blocked_artifacts == 1
    assert remote.status == "watch"


def test_two_blocked_artifacts_make_the_capability_due(repo: Path) -> None:
    for name in ("worker-capacity", "learning-budget"):
        git_strict(repo, "switch", "-c", name)
        write(repo, f"{name}.txt", name)
        commit_all(repo, name)
    remote = next(c for c in verdicts(scan_repo(repo)[0]) if c.capability == "git-remote")
    assert remote.blocked_artifacts >= DUE_THRESHOLD
    assert remote.status == "due"
    assert remote.practice.startswith("tracks/04-tooling")


def test_two_findings_on_one_branch_count_as_one_artifact(repo: Path) -> None:
    """Severity must not be inflated by counting the same blocked thing twice."""
    git_strict(repo, "switch", "-c", "einzeln")
    write(repo, "x.txt", "x\n")
    commit_all(repo, "einzeln")
    findings, _ = scan_repo(repo)
    branch_findings = [f for f in findings if f.subject == "branch:einzeln"]
    assert len({f.artifact_id for f in branch_findings}) == 1


# --- report shape ------------------------------------------------------------


def test_detect_reports_missing_repo_without_crashing(tmp_path: Path) -> None:
    report = detect([tmp_path / "gibt-es-nicht"])
    assert report["ok"] is True
    assert report["findings"] == []
    assert report["errors"][0]["error"] == "missing_repo"


def test_detect_reports_non_repo_directory(tmp_path: Path) -> None:
    plain = tmp_path / "nur-ein-ordner"
    plain.mkdir()
    report = detect([plain])
    assert report["errors"], "a directory without git must be reported, not silently skipped"


def test_detect_is_json_serialisable(repo: Path) -> None:
    import json

    git_strict(repo, "switch", "-c", "irgendwas")
    write(repo, "y.txt", "y\n")
    commit_all(repo, "y")
    report = detect([repo])
    json.dumps(report)  # must not raise
    assert report["scanned"] == [str(repo)]
    assert report["generated_at"].endswith("Z")
