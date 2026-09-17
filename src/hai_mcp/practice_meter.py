"""Practice meter: what the owner did with their own hands, measured not asserted.

Why this exists
---------------
A counter that asks the agent how many git commands the owner ran is the
``owner_ack=true`` mistake in a new costume: the party being measured supplies
the measurement. A drifting, helpful or simply forgetful agent inflates it, and
nothing in the record says so.

So this module never asks. It reads a trace the agent does not write: the
interactive shell history. A command a human types at a prompt is appended to
``HISTFILE``; a command an agent runs through ``subprocess`` is executed by a
non-interactive shell, which appends nothing. The line between the owner's hand
and the agent's is therefore not a claim anyone makes — it falls out of how
shells work.

The aviation model, made literal
--------------------------------
Airline pilots do not stay current on everything. They stay current on takeoffs,
landings and failure of the automation, and currency *lapses* on a clock. That is
the model here: a capability is current when it was exercised by hand often
enough, recently enough (:data:`CURRENCY`). Nothing punishes a lapse — the record
simply stops saying you are current, the same way a logbook stops saying it.

Privacy
-------
Only lines whose command is ``git`` are retained. Everything else — and shell
history holds passwords, tokens and private paths — is dropped inside the parser
and never stored, returned or printed.

What this does NOT do
---------------------
It counts keystrokes, not understanding: a typed command proves the owner acted,
never that they knew why. It is not tamper-proof, since a history file can be
edited by anyone who can write it — but unlike an agent's self-report, altering
it takes a deliberate act rather than an omission. And it can only measure what
the shell dates: with ``EXTENDED_HISTORY`` off, zsh stores commands without a
timestamp, and those are counted but cannot be placed in time. The report says so
rather than quietly treating undated entries as recent.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

#: capability id -> git subcommands that exercise it (mirrors blocker_detector).
CAPABILITY_BY_SUBCOMMAND: dict[str, str] = {
    # publishing and fetching
    "push": "git-remote",
    "pull": "git-remote",
    "fetch": "git-remote",
    "clone": "git-remote",
    "remote": "git-remote",
    # branches and integration
    "branch": "git-branch",
    "switch": "git-branch",
    "checkout": "git-branch",
    "merge": "git-branch",
    "rebase": "git-branch",
    "cherry-pick": "git-branch",
    "revert": "git-branch",
    # reading state and committing
    "status": "git-basics",
    "add": "git-basics",
    "commit": "git-basics",
    "log": "git-basics",
    "diff": "git-basics",
    "show": "git-basics",
    "stash": "git-basics",
    "restore": "git-basics",
    "reset": "git-basics",
}

#: capability -> (hand executions required, within this many days). Aviation currency.
CURRENCY: dict[str, tuple[int, int]] = {
    "git-remote": (3, 30),
    "git-branch": (3, 30),
    "git-basics": (5, 30),
}

DEFAULT_WINDOWS = (7, 30)

_ZSH_EXTENDED = re.compile(r"^:\s*(\d+):\d+;(.*)$")
_BASH_STAMP = re.compile(r"^#(\d{9,})$")
_FISH_CMD = re.compile(r"^- cmd:\s*(.*)$")
_FISH_WHEN = re.compile(r"^\s+when:\s*(\d+)")

#: A leading `sudo`/`env`-style wrapper still means the human typed a git command.
_WRAPPERS = frozenset({"sudo", "command", "nohup", "time"})

#: Global git options that consume the following token, which is therefore not the
#: subcommand — ``git -C /tmp/repo log`` would otherwise be read as ``git /tmp/repo``.
_VALUE_FLAGS = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--super-prefix"}
)


@dataclass(frozen=True)
class GitEvent:
    """One git command a human typed. Never carries a non-git command line."""

    subcommand: str
    capability: str
    timestamp: int | None
    source: str

    @property
    def dated(self) -> bool:
        return self.timestamp is not None


@dataclass
class CapabilityPractice:
    capability: str
    total: int
    undated: int
    counts_by_window: dict[str, int]
    last_practised: str | None
    days_since: float | None
    required: int
    within_days: int
    status: str


def _utc_iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts if ts is not None else time.time()))


def _classify(command: str) -> tuple[str, str] | None:
    """Return ``(subcommand, capability)`` for a git command line, else ``None``.

    Everything that is not a git invocation returns ``None`` and is discarded by
    the caller — this is the privacy boundary, and it is deliberately the first
    thing that happens to a history line.
    """
    tokens = command.strip().split()
    while tokens and tokens[0] in _WRAPPERS:
        tokens = tokens[1:]
    if not tokens or tokens[0] != "git":
        return None
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in _VALUE_FLAGS:
            index += 2  # skip the option and the value it consumes
            continue
        if token.startswith("-"):
            index += 1  # a flag carrying its own value, or a bare one such as --no-pager
            continue
        capability = CAPABILITY_BY_SUBCOMMAND.get(token)
        if capability is None:
            return None
        return token, capability
    return None


def _emit(command: str, timestamp: int | None, source: str) -> GitEvent | None:
    hit = _classify(command)
    if hit is None:
        return None
    subcommand, capability = hit
    return GitEvent(subcommand=subcommand, capability=capability, timestamp=timestamp, source=source)


def parse_zsh(text: str, source: str = "zsh") -> list[GitEvent]:
    """Handle both the extended format (``: <epoch>:<elapsed>;cmd``) and plain lines."""
    events: list[GitEvent] = []
    for raw in text.splitlines():
        line = raw.rstrip("\\")
        match = _ZSH_EXTENDED.match(line)
        if match:
            stamp: int | None = int(match.group(1))
            command = match.group(2)
        else:
            stamp, command = None, line
        event = _emit(command, stamp, source)
        if event is not None:
            events.append(event)
    return events


def parse_bash(text: str, source: str = "bash") -> list[GitEvent]:
    """``HISTTIMEFORMAT`` writes a ``#<epoch>`` line before each command."""
    events: list[GitEvent] = []
    pending: int | None = None
    for raw in text.splitlines():
        line = raw.strip()
        stamp_match = _BASH_STAMP.match(line)
        if stamp_match:
            pending = int(stamp_match.group(1))
            continue
        event = _emit(line, pending, source)
        if event is not None:
            events.append(event)
        pending = None
    return events


def parse_fish(text: str, source: str = "fish") -> list[GitEvent]:
    events: list[GitEvent] = []
    command: str | None = None
    for raw in text.splitlines():
        cmd_match = _FISH_CMD.match(raw)
        if cmd_match:
            command = cmd_match.group(1)
            continue
        when_match = _FISH_WHEN.match(raw)
        if when_match and command is not None:
            event = _emit(command, int(when_match.group(1)), source)
            if event is not None:
                events.append(event)
            command = None
    return events


_PARSERS = {
    ".zsh_history": parse_zsh,
    ".bash_history": parse_bash,
    "fish_history": parse_fish,
}


def default_history_paths(home: Path | None = None) -> list[Path]:
    base = home or Path.home()
    return [
        base / ".zsh_history",
        base / ".bash_history",
        base / ".local/share/fish/fish_history",
    ]


def read_events(paths: Iterable[Path]) -> tuple[list[GitEvent], list[dict[str, Any]]]:
    """Parse every readable history file. Returns ``(events, notes)``."""
    events: list[GitEvent] = []
    notes: list[dict[str, Any]] = []
    for path in paths:
        if not path.is_file():
            continue
        parser = _PARSERS.get(path.name, parse_zsh)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            notes.append({"path": str(path), "note": "unreadable", "message": str(exc)})
            continue
        found = parser(text, source=path.name)
        events += found
        undated = sum(1 for e in found if not e.dated)
        if found and undated == len(found):
            notes.append(
                {
                    "path": str(path),
                    "note": "no_timestamps",
                    "message": "Keine Einträge mit Zeitstempel. In zsh: 'setopt EXTENDED_HISTORY' "
                    "in ~/.zshrc — ohne das lässt sich Übung nicht in der Zeit verorten.",
                }
            )
        elif undated:
            notes.append(
                {
                    "path": str(path),
                    "note": "partial_timestamps",
                    "message": f"{undated} von {len(found)} git-Einträgen ohne Zeitstempel; "
                    "sie zählen mit, lassen sich aber nicht datieren.",
                }
            )
    return events, notes


def practice(
    events: list[GitEvent],
    *,
    now: float | None = None,
    windows: tuple[int, ...] = DEFAULT_WINDOWS,
) -> list[CapabilityPractice]:
    """Per capability: how much hand practice, how recent, and is it still current."""
    reference = now if now is not None else time.time()
    by_capability: dict[str, list[GitEvent]] = {}
    for event in events:
        by_capability.setdefault(event.capability, []).append(event)

    out: list[CapabilityPractice] = []
    for capability, required_pair in CURRENCY.items():
        required, within_days = required_pair
        group = by_capability.get(capability, [])
        dated = [e for e in group if e.timestamp is not None]

        counts = {}
        for window in windows:
            cutoff = reference - window * 86400
            counts[f"{window}d"] = sum(1 for e in dated if (e.timestamp or 0) >= cutoff)

        newest = max((e.timestamp or 0 for e in dated), default=0)
        cutoff = reference - within_days * 86400
        in_window = sum(1 for e in dated if (e.timestamp or 0) >= cutoff)

        if not dated:
            status = "unknown"
        elif in_window >= required:
            status = "current"
        else:
            status = "lapsed"

        out.append(
            CapabilityPractice(
                capability=capability,
                total=len(group),
                undated=len(group) - len(dated),
                counts_by_window=counts,
                last_practised=_utc_iso(newest) if newest else None,
                days_since=round((reference - newest) / 86400.0, 1) if newest else None,
                required=required,
                within_days=within_days,
                status=status,
            )
        )
    out.sort(key=lambda p: ({"lapsed": 0, "unknown": 1, "current": 2}[p.status], p.capability))
    return out


def measure(paths: Iterable[Path] | None = None, *, now: float | None = None) -> dict[str, Any]:
    history_paths = list(paths) if paths is not None else default_history_paths()
    events, notes = read_events(history_paths)
    by_subcommand: dict[str, int] = {}
    for event in events:
        by_subcommand[event.subcommand] = by_subcommand.get(event.subcommand, 0) + 1
    return {
        "ok": True,
        "generated_at": _utc_iso(now),
        "sources": [str(p) for p in history_paths if p.is_file()],
        "git_commands_total": len(events),
        "by_subcommand": dict(sorted(by_subcommand.items())),
        "capabilities": [asdict(p) for p in practice(events, now=now)],
        "notes": notes,
    }


def combine(blocker_report: dict[str, Any], practice_report: dict[str, Any]) -> dict[str, Any]:
    """Join both signals. Blocked *and* out of practice is what needs doing first.

    This is the whole point of having two instruments: one says what is stopping
    the owner, the other says whether they still have the hands for it.
    """
    blocked = {c["capability"]: c for c in blocker_report.get("capabilities", [])}
    practised = {c["capability"]: c for c in practice_report.get("capabilities", [])}

    rows: list[dict[str, Any]] = []
    for capability in sorted(set(blocked) | set(practised)):
        block = blocked.get(capability, {})
        prac = practised.get(capability, {})
        blocking = block.get("status") == "due"
        rusty = prac.get("status") in {"lapsed", "unknown"}

        if blocking and rusty:
            priority, reason = "now", "blockiert dich und wurde zuletzt nicht geübt"
        elif blocking:
            priority, reason = "soon", "blockiert dich, Übung ist aber aktuell"
        elif rusty:
            priority, reason = "watch", "keine frische Übung, blockiert aber gerade nichts"
        else:
            priority, reason = "ok", "aktuell und nicht blockierend"

        rows.append(
            {
                "capability": capability,
                "priority": priority,
                "reason": reason,
                "blocked_artifacts": block.get("blocked_artifacts", 0),
                "practice_status": prac.get("status", "unknown"),
                "days_since_practice": prac.get("days_since"),
                "practice": block.get("practice") or "",
            }
        )
    order = {"now": 0, "soon": 1, "watch": 2, "ok": 3}
    rows.sort(key=lambda r: (order[r["priority"]], -r["blocked_artifacts"]))
    return {"ok": True, "generated_at": _utc_iso(), "priorities": rows}


def render_text(report: dict[str, Any]) -> str:
    lines = [f"EIGENE GIT-BEFEHLE: {report['git_commands_total']} insgesamt", ""]
    for cap in report["capabilities"]:
        mark = {"current": "aktuell   ", "lapsed": "VERFALLEN ", "unknown": "unbekannt "}[
            cap["status"]
        ]
        windows = " · ".join(f"{k}: {v}" for k, v in cap["counts_by_window"].items())
        lines.append(f"  {mark} {cap['capability']:<12} {windows}")
        if cap["days_since"] is not None:
            lines.append(
                f"               zuletzt vor {cap['days_since']:.0f} Tagen · "
                f"verlangt {cap['required']}× in {cap['within_days']} Tagen"
            )
        if cap["undated"]:
            lines.append(f"               {cap['undated']} Einträge ohne Zeitstempel")
    if report["by_subcommand"]:
        lines += ["", "NACH BEFEHL", ""]
        for name, count in report["by_subcommand"].items():
            lines.append(f"  {count:>4} × git {name}")
    for note in report.get("notes", []):
        lines += ["", f"  ! {note['message']}"]
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hai-practice-meter",
        description="Zählt die git-Befehle, die der Owner selbst getippt hat. Rein lesend; "
        "nur git-Zeilen werden ausgewertet, alles andere sofort verworfen.",
    )
    parser.add_argument(
        "--history",
        action="append",
        default=None,
        help="Pfad zu einer History-Datei; mehrfach angebbar (Vorgabe: zsh, bash, fish)",
    )
    parser.add_argument("--json", action="store_true", help="Bericht als JSON ausgeben")
    args = parser.parse_args(argv)

    paths = [Path(p).expanduser() for p in args.history] if args.history else None
    report = measure(paths)

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        print(render_text(report), end="")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
