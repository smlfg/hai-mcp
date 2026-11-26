"""Which HAI-MCP code is actually running.

The owner should never have to wonder whether the live server is the version they
develop. This module answers that from the checkout the server was started from:
commit, branch, uncommitted changes, and distance to ``origin/main``.

Boundaries (kept on purpose):
- read-only ``git`` invocations with an argument list, never a shell;
- no network: ``origin/main`` is compared as of the last local fetch;
- fail-soft: any problem becomes ``source="unknown"`` plus an error string, never an
  exception, so ``hai_health`` stays reachable.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

GIT_TIMEOUT_SECONDS = 2.0
REFERENCE_REF = "origin/main"

# A runner takes git arguments and returns stripped stdout, or raises on failure.
Runner = Callable[[Sequence[str]], str]


def code_root() -> Path:
    """Directory the running package was loaded from (``src/hai_mcp`` -> repo root)."""
    return Path(__file__).resolve().parents[2]


def _git_runner(root: Path) -> Runner:
    def run(args: Sequence[str]) -> str:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=True,
        )
        return completed.stdout.strip()

    return run


def describe_build(root: Path | None = None, runner: Runner | None = None) -> dict[str, Any]:
    """Describe the code the server runs from. Never raises."""
    if root is None:
        root = code_root()
        # Installed copy (site-packages), not a source checkout: a git repo that merely
        # contains the virtualenv must not be reported as the running code.
        if not (root / "src" / "hai_mcp").is_dir():
            return {"source": "package", "code_root": str(root), "warnings": []}
    run = runner or _git_runner(root)
    try:
        inside = run(["rev-parse", "--is-inside-work-tree"])
    except FileNotFoundError:
        return {"source": "unknown", "error": "git_not_found", "code_root": str(root)}
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return {"source": "package", "code_root": str(root), "warnings": []}
    if inside != "true":
        return {"source": "package", "code_root": str(root), "warnings": []}

    try:
        commit = run(["rev-parse", "--short=7", "HEAD"])
        branch_name = run(["rev-parse", "--abbrev-ref", "HEAD"])
        porcelain = run(["status", "--porcelain"])
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        return {"source": "unknown", "error": f"git_failed: {type(exc).__name__}", "code_root": str(root)}

    detached = branch_name == "HEAD"
    dirty_files = len([line for line in porcelain.splitlines() if line.strip()])
    info: dict[str, Any] = {
        "source": "git",
        "code_root": str(root),
        "commit": commit,
        "branch": None if detached else branch_name,
        "detached": detached,
        "dirty": dirty_files > 0,
        "dirty_files": dirty_files,
        "reference": REFERENCE_REF,
        "ahead": None,
        "behind": None,
    }

    try:
        counts = run(["rev-list", "--left-right", "--count", f"HEAD...{REFERENCE_REF}"])
        ahead_str, behind_str = counts.split()
        info["ahead"], info["behind"] = int(ahead_str), int(behind_str)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, ValueError):
        info["reference_missing"] = True

    info["warnings"] = build_warnings(info)
    return info


def build_warnings(info: dict[str, Any]) -> list[str]:
    """Plain-language warnings an agent should pass on to the owner verbatim."""
    if info.get("source") != "git":
        return []
    warnings: list[str] = []
    where = f"HAI-MCP runs from {info['commit']}"
    if info["detached"]:
        warnings.append(f"{where} on a detached HEAD (no branch).")
    elif info["branch"] != "main":
        warnings.append(f"{where} on branch '{info['branch']}', not main.")
    if info["dirty"]:
        warnings.append(f"{where} with {info['dirty_files']} uncommitted change(s).")
    if info.get("behind"):
        warnings.append(f"{where}, {info['behind']} commit(s) behind {REFERENCE_REF} (as of last fetch).")
    if info.get("ahead"):
        warnings.append(f"{where}, {info['ahead']} commit(s) not in {REFERENCE_REF}.")
    if info.get("reference_missing"):
        warnings.append(f"{REFERENCE_REF} is not known locally; cannot compare versions.")
    return warnings
