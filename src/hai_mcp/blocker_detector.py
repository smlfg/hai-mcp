"""Blocker detector: which capability is stopping the owner right now.

Why this exists
---------------
An agent opens loops faster than a human closes them. A pull request is opened in
four minutes; closing it needs a human to verify and decide, and verification is
the serial, un-delegatable part. The ratio of opened to closed diverges on its
own, and the repository's state is the arithmetic of that — not a character flaw.

So the owner does not need to be asked what they cannot do. A blockage leaves
traces, and this module reads them: work that was started and never reached a
terminal state. Each trace maps to one capability, and a capability that blocks
two distinct artifacts is *due* — the rest stays delegable, forever.

What it is not
--------------
Not an assessment of the owner, not a curriculum, and not a gate. It reports.
The decision to collect a learning debt belongs to the mission lifecycle, at the
closing ritual — never mid-flight, because a system that interrupts work while it
is being done gets switched off on exactly the evening it was needed most.

Safety
------
Strictly read-only: every git invocation goes through :func:`_git`, whose first
argument must be in :data:`READ_ONLY_SUBCOMMANDS`. Nothing is written, fetched or
sent. ``status`` runs under ``--no-optional-locks`` so not even the index cache
is touched.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

# Only these git subcommands may run. A mutating one is a bug, not a policy choice.
READ_ONLY_SUBCOMMANDS = frozenset(
    {"rev-parse", "for-each-ref", "diff", "status", "symbolic-ref", "log"}
)

_GIT_TIMEOUT_SECONDS = 15

#: Two distinct blocked artifacts make a capability due. One is noise; two is a pattern.
DUE_THRESHOLD = 2

#: Markers git leaves in the git dir while an operation is half-finished.
_IN_PROGRESS_MARKERS: tuple[tuple[str, str], ...] = (
    ("CHERRY_PICK_HEAD", "cherry-pick"),
    ("MERGE_HEAD", "merge"),
    ("REVERT_HEAD", "revert"),
    ("rebase-merge", "rebase"),
    ("rebase-apply", "rebase"),
    ("BISECT_LOG", "bisect"),
)

#: capability id -> what it is, and where the owner practises it.
CAPABILITIES: dict[str, dict[str, str]] = {
    "git-basics": {
        "title": "Git: Stand lesen, festschreiben",
        "practice": "tracks/04-tooling/01-git-basics",
        "why": "Ohne einen festgeschriebenen Stand ist Arbeit jederzeit verlierbar.",
    },
    "git-branch": {
        "title": "Git: Zweige und Zusammenführen",
        "practice": "tracks/04-tooling/02-git-branch",
        "why": "Eine halb gelaufene Operation blockiert jede weitere Arbeit im Repo.",
    },
    "git-remote": {
        "title": "Git: Veröffentlichen und Holen",
        "practice": "tracks/04-tooling",
        "why": "Was nicht veröffentlicht ist, existiert für niemanden sonst — auch nicht "
        "für die eigenen Agenten auf einem anderen Rechner.",
    },
}

#: finding kind -> (capability, severity, one-line title)
_KINDS: dict[str, tuple[str, int, str]] = {
    "in_progress_operation": ("git-branch", 100, "Halb gelaufene Operation"),
    "unmerged_paths": ("git-branch", 90, "Ungelöste Konfliktdateien"),
    "detached_head": ("git-basics", 60, "Losgelöster HEAD"),
    "never_pushed": ("git-remote", 50, "Zweig nie veröffentlicht"),
    "unpushed_commits": ("git-remote", 45, "Unveröffentlichte Commits"),
    "dirty_worktree": ("git-basics", 30, "Nicht festgeschriebene Änderungen"),
    "stash_backlog": ("git-basics", 20, "Liegengebliebene Stashes"),
}


class GitProbeError(RuntimeError):
    """A git invocation could not be completed. Carries a structured code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Finding:
    """One open loop: work that was started and never reached a terminal state."""

    kind: str
    repo: str
    subject: str
    title: str
    evidence: str
    capability: str
    severity: int
    age_days: float | None = None

    @property
    def artifact_id(self) -> str:
        """Identity of the blocked thing, so two findings on one branch count once."""
        return f"{self.repo}::{self.subject}"


@dataclass
class CapabilityVerdict:
    capability: str
    title: str
    practice: str
    why: str
    status: str
    blocked_artifacts: int
    findings: list[str] = field(default_factory=list)


def _utc_iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts if ts is not None else time.time()))


def _git(repo: Path, args: list[str]) -> str:
    """Run one read-only git subcommand and return stdout.

    Refuses any subcommand outside the allowlist — a mutating call must fail loudly
    rather than quietly change the owner's repository while it is being inspected.
    """
    if not args:
        raise GitProbeError("invalid_args", "git needs a subcommand")
    if args[0] not in READ_ONLY_SUBCOMMANDS:
        raise GitProbeError(
            "forbidden_subcommand",
            f"refusing to run 'git {args[0]}': detector is read-only",
        )
    cmd = ["git", "--no-optional-locks", "-C", str(repo), *args]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:  # pragma: no cover - environment without git
        raise GitProbeError("git_missing", "git executable not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitProbeError("git_timeout", f"git {args[0]} timed out") from exc
    if proc.returncode != 0:
        raise GitProbeError(
            "git_failed",
            f"git {args[0]} failed ({proc.returncode}): {proc.stderr.strip()[:200]}",
        )
    return proc.stdout


def _git_ok(repo: Path, args: list[str]) -> tuple[bool, str]:
    """Like :func:`_git` but a non-zero exit is an answer, not an error."""
    try:
        return True, _git(repo, args)
    except GitProbeError as exc:
        if exc.code == "git_failed":
            return False, ""
        raise


def _age_days(ts: float | None) -> float | None:
    if ts is None:
        return None
    return round(max(time.time() - ts, 0.0) / 86400.0, 1)


def _newest_mtime(paths: Iterable[Path]) -> float | None:
    stamps = []
    for path in paths:
        try:
            stamps.append(path.stat().st_mtime)
        except OSError:
            continue
    return max(stamps) if stamps else None


def _oldest_mtime(paths: Iterable[Path]) -> float | None:
    stamps = []
    for path in paths:
        try:
            stamps.append(path.stat().st_mtime)
        except OSError:
            continue
    return min(stamps) if stamps else None


def _make(kind: str, repo: str, subject: str, evidence: str, age_days: float | None) -> Finding:
    capability, severity, title = _KINDS[kind]
    return Finding(
        kind=kind,
        repo=repo,
        subject=subject,
        title=title,
        evidence=evidence,
        capability=capability,
        severity=severity,
        age_days=age_days,
    )


# --- probes -----------------------------------------------------------------


def _probe_in_progress(repo: Path, git_dir: Path, label: str) -> list[Finding]:
    out: list[Finding] = []
    for marker, operation in _IN_PROGRESS_MARKERS:
        path = git_dir / marker
        if not path.exists():
            continue
        out.append(
            _make(
                "in_progress_operation",
                label,
                f"operation:{operation}",
                f"Ein {operation} läuft seit dem Start und wurde nie beendet "
                f"({marker} liegt im Git-Verzeichnis).",
                _age_days(_newest_mtime([path])),
            )
        )
    return out


def _probe_unmerged(repo: Path, label: str) -> list[Finding]:
    ok, raw = _git_ok(repo, ["diff", "--name-only", "--diff-filter=U"])
    if not ok:
        return []
    files = [line.strip() for line in raw.splitlines() if line.strip()]
    if not files:
        return []
    shown = ", ".join(files[:3]) + (f" … (+{len(files) - 3})" if len(files) > 3 else "")
    return [
        _make(
            "unmerged_paths",
            label,
            "conflicts",
            f"{len(files)} Datei(en) mit ungelösten Konflikten: {shown}",
            _age_days(_oldest_mtime(repo / f for f in files)),
        )
    ]


def _probe_detached_head(repo: Path, label: str) -> list[Finding]:
    ok, _ = _git_ok(repo, ["symbolic-ref", "-q", "--short", "HEAD"])
    if ok:
        return []
    return [
        _make(
            "detached_head",
            label,
            "HEAD",
            "HEAD zeigt auf keinen Zweig. Neue Commits hier gehören zu nichts und "
            "sind beim nächsten Wechsel schwer wiederzufinden.",
            None,
        )
    ]


def _probe_branches(repo: Path, label: str) -> list[Finding]:
    fmt = "%(refname:short)%09%(upstream:short)%09%(upstream:track)%09%(committerdate:unix)"
    ok, raw = _git_ok(repo, ["for-each-ref", f"--format={fmt}", "refs/heads"])
    if not ok:
        return []
    out: list[Finding] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        name, upstream, track, stamp = parts[0], parts[1], parts[2], parts[3]
        try:
            age = _age_days(float(stamp))
        except ValueError:
            age = None
        if not upstream:
            out.append(
                _make(
                    "never_pushed",
                    label,
                    f"branch:{name}",
                    f"Zweig '{name}' hat kein Gegenstück auf einem Server. Alles darauf "
                    f"existiert nur auf diesem Rechner.",
                    age,
                )
            )
            continue
        if "ahead" in track:
            out.append(
                _make(
                    "unpushed_commits",
                    label,
                    f"branch:{name}",
                    f"Zweig '{name}' hat Commits, die '{upstream}' nicht hat ({track.strip('[]')}).",
                    age,
                )
            )
    return out


def _probe_dirty(repo: Path, label: str) -> list[Finding]:
    ok, raw = _git_ok(repo, ["status", "--porcelain"])
    if not ok:
        return []
    changed: list[str] = []
    for line in raw.splitlines():
        if len(line) < 4:
            continue
        if line[:2] == "UU":  # already reported as a conflict
            continue
        changed.append(line[3:].strip().strip('"'))
    if not changed:
        return []
    shown = ", ".join(changed[:3]) + (f" … (+{len(changed) - 3})" if len(changed) > 3 else "")
    return [
        _make(
            "dirty_worktree",
            label,
            "worktree",
            f"{len(changed)} nicht festgeschriebene Änderung(en): {shown}",
            _age_days(_oldest_mtime(repo / c for c in changed)),
        )
    ]


def _probe_stashes(repo: Path, label: str) -> list[Finding]:
    ok, raw = _git_ok(repo, ["for-each-ref", "--format=%(committerdate:unix)", "refs/stash"])
    if not ok:
        return []
    stamps = [line.strip() for line in raw.splitlines() if line.strip()]
    if not stamps:
        return []
    try:
        oldest = min(float(s) for s in stamps)
    except ValueError:
        oldest = None
    return [
        _make(
            "stash_backlog",
            label,
            "stash",
            f"{len(stamps)} Stash-Eintrag/Einträge liegen unangetastet.",
            _age_days(oldest),
        )
    ]


# --- scanning ---------------------------------------------------------------


def scan_repo(repo_path: str | Path) -> tuple[list[Finding], dict[str, Any] | None]:
    """Collect findings for one repository. Returns ``(findings, error)``."""
    repo = Path(repo_path).expanduser()
    if not repo.is_dir():
        return [], {"repo": str(repo), "error": "missing_repo", "message": "kein Verzeichnis"}
    try:
        top = _git(repo, ["rev-parse", "--show-toplevel"]).strip()
        git_dir_raw = _git(repo, ["rev-parse", "--absolute-git-dir"]).strip()
    except GitProbeError as exc:
        return [], {"repo": str(repo), "error": exc.code, "message": exc.message}

    root = Path(top)
    git_dir = Path(git_dir_raw)
    label = root.name or str(root)

    findings: list[Finding] = []
    try:
        findings += _probe_in_progress(root, git_dir, label)
        findings += _probe_unmerged(root, label)
        findings += _probe_detached_head(root, label)
        findings += _probe_branches(root, label)
        findings += _probe_dirty(root, label)
        findings += _probe_stashes(root, label)
    except GitProbeError as exc:
        return findings, {"repo": str(root), "error": exc.code, "message": exc.message}
    return findings, None


def rank(findings: list[Finding]) -> list[Finding]:
    """Most blocking first, then oldest. Age None sorts last within a severity."""
    return sorted(findings, key=lambda f: (-f.severity, -(f.age_days or 0.0), f.subject))


def verdicts(findings: list[Finding]) -> list[CapabilityVerdict]:
    """A capability blocking DUE_THRESHOLD distinct artifacts is due; one is watched."""
    by_capability: dict[str, list[Finding]] = {}
    for finding in findings:
        by_capability.setdefault(finding.capability, []).append(finding)

    out: list[CapabilityVerdict] = []
    for capability, group in by_capability.items():
        meta = CAPABILITIES.get(capability, {})
        artifacts = {f.artifact_id for f in group}
        out.append(
            CapabilityVerdict(
                capability=capability,
                title=meta.get("title", capability),
                practice=meta.get("practice", ""),
                why=meta.get("why", ""),
                status="due" if len(artifacts) >= DUE_THRESHOLD else "watch",
                blocked_artifacts=len(artifacts),
                findings=[f.kind for f in rank(group)],
            )
        )
    out.sort(key=lambda v: (v.status != "due", -v.blocked_artifacts, v.capability))
    return out


def detect(repos: list[str | Path]) -> dict[str, Any]:
    """Scan every repo and return the full report as a plain dict."""
    all_findings: list[Finding] = []
    errors: list[dict[str, Any]] = []
    scanned: list[str] = []
    for repo in repos:
        found, error = scan_repo(repo)
        all_findings += found
        if error:
            errors.append(error)
        else:
            scanned.append(str(Path(repo).expanduser()))

    ranked = rank(all_findings)
    return {
        "ok": True,
        "generated_at": _utc_iso(),
        "scanned": scanned,
        "findings": [asdict(f) for f in ranked],
        "capabilities": [asdict(v) for v in verdicts(all_findings)],
        "errors": errors,
    }


# --- presentation -----------------------------------------------------------


def render_text(report: dict[str, Any]) -> str:
    lines: list[str] = []
    findings = report.get("findings", [])
    capabilities = report.get("capabilities", [])

    if not findings:
        lines.append("Keine offenen Schleifen gefunden. Nichts hält dich gerade auf.")
    else:
        lines.append(f"OFFENE SCHLEIFEN ({len(findings)})")
        lines.append("")
        for f in findings:
            age = f"{f['age_days']:.0f} Tage" if f.get("age_days") is not None else "Alter unbekannt"
            lines.append(f"  [{f['repo']}] {f['title']} — {age}")
            lines.append(f"      {f['evidence']}")
        lines.append("")

    if capabilities:
        lines.append("FÄHIGKEITEN")
        lines.append("")
        for c in capabilities:
            mark = "FÄLLIG " if c["status"] == "due" else "beobachtet"
            lines.append(
                f"  {mark}  {c['title']}  "
                f"({c['blocked_artifacts']} blockierte(s) Artefakt(e))"
            )
            if c["status"] == "due":
                lines.append(f"      {c['why']}")
                if c["practice"]:
                    lines.append(f"      Üben: {c['practice']}")
        lines.append("")

    for e in report.get("errors", []):
        lines.append(f"  ! {e['repo']}: {e['error']} — {e['message']}")

    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hai-blocker-detector",
        description="Meldet offene Schleifen und die Fähigkeit, die sie verursacht. Rein lesend.",
    )
    parser.add_argument(
        "--repo",
        action="append",
        default=None,
        help="Repository-Pfad; mehrfach angebbar (Vorgabe: aktuelles Verzeichnis)",
    )
    parser.add_argument("--json", action="store_true", help="Bericht als JSON ausgeben")
    args = parser.parse_args(argv)

    repos: list[str | Path] = list(args.repo) if args.repo else [Path.cwd()]
    report = detect(repos)

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        print(render_text(report), end="")

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
