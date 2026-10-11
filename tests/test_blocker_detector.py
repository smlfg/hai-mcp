"""Acceptance tests for the blocker detector.

Every test builds a real repository in a temp dir and asserts on the real git
state — the detector's whole value is that it reads the world, so testing it
against a mock would repeat the mistake this project already made twice.
"""

from __future__ import annotations

import os
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


def test_stash_backlog_counts_the_reflog_not_only_the_tip(repo: Path) -> None:
    def stash_at(text: str, when: str) -> None:
        write(repo, "README.md", text)
        env = os.environ.copy()
        env["GIT_AUTHOR_DATE"] = when
        env["GIT_COMMITTER_DATE"] = when
        subprocess.run(
            ["git", *GIT_IDENTITY, "-C", str(repo), "stash", "push", "-m", when],
            check=True,
            env=env,
            capture_output=True,
            text=True,
        )

    stash_at("alt\n", "2020-01-01T00:00:00Z")
    stash_at("neu\n", "2026-10-01T00:00:00Z")
    findings, error = scan_repo(repo)
    assert error is None
    finding = next(f for f in findings if f.kind == "stash_backlog")
    assert finding.evidence.startswith("2 ")
    assert finding.age_days is not None
    assert finding.age_days > 365


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


def test_same_directory_name_keeps_distinct_artifacts(tmp_path: Path) -> None:
    roots: list[Path] = []
    for parent in ("a", "b"):
        work = tmp_path / parent / "hai-mcp"
        work.mkdir(parents=True)
        git_strict(work, "init", "--initial-branch=main")
        write(work, "README.md", "start\n")
        commit_all(work, "start")
        write(work, "README.md", f"dirty {parent}\n")
        roots.append(work)

    findings = []
    for work in roots:
        found, error = scan_repo(work)
        assert error is None
        findings += found
    dirty = [f for f in findings if f.kind == "dirty_worktree"]
    assert len({f.artifact_id for f in dirty}) == 2
    assert {Path(f.repo) for f in dirty} == set(roots)
    basics = next(c for c in verdicts(findings) if c.capability == "git-basics")
    assert basics.blocked_artifacts == 2
    assert basics.status == "due"


def test_both_added_conflict_is_not_also_dirty_work(repo: Path) -> None:
    git_strict(repo, "switch", "-c", "add-a")
    write(repo, "both.txt", "from a\n")
    commit_all(repo, "add both from a")
    git_strict(repo, "switch", "main")
    git_strict(repo, "switch", "-c", "add-b")
    write(repo, "both.txt", "from b\n")
    commit_all(repo, "add both from b")
    git_strict(repo, "switch", "add-a")
    git(repo, "merge", "--no-commit", "--no-ff", "add-b")

    findings, error = scan_repo(repo)
    assert error is None
    assert "unmerged_paths" in kinds(findings)
    assert "dirty_worktree" not in kinds(findings)

    write(repo, "extra.txt", "wirklich schmutzig\n")
    findings, error = scan_repo(repo)
    assert error is None
    dirty = next(f for f in findings if f.kind == "dirty_worktree")
    assert "extra.txt" in dirty.evidence
    assert "both.txt" not in dirty.evidence


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
