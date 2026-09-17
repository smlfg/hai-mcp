"""Acceptance tests for the practice meter.

The privacy boundary and the agent/human boundary are the two properties worth
guarding, so both get explicit tests rather than being left to the docstring.
"""

from __future__ import annotations

import time
from pathlib import Path

from hai_mcp.practice_meter import (
    CURRENCY,
    combine,
    measure,
    parse_bash,
    parse_fish,
    parse_zsh,
    practice,
    read_events,
)

DAY = 86400.0


def zsh_line(ts: int, command: str) -> str:
    return f": {ts}:0;{command}"


# --- privacy: non-git lines never survive the parser -------------------------


def test_non_git_commands_are_discarded() -> None:
    text = "\n".join(
        [
            zsh_line(1_700_000_000, "export OPENAI_API_KEY=sk-geheim"),
            zsh_line(1_700_000_001, "ssh root@server -p 2222"),
            zsh_line(1_700_000_002, "mysql -u root -phunter2"),
            zsh_line(1_700_000_003, "git status"),
        ]
    )
    events = parse_zsh(text)
    assert len(events) == 1
    assert events[0].subcommand == "status"


def test_secrets_never_reach_the_report(tmp_path: Path) -> None:
    history = tmp_path / ".zsh_history"
    history.write_text(
        "\n".join(
            [
                zsh_line(1_700_000_000, "curl -H 'Authorization: Bearer sk-supergeheim' https://x"),
                zsh_line(1_700_000_001, "git push"),
            ]
        ),
        encoding="utf-8",
    )
    report = measure([history])
    assert "supergeheim" not in repr(report)
    assert report["git_commands_total"] == 1


def test_unknown_git_subcommand_is_ignored() -> None:
    events = parse_zsh(zsh_line(1_700_000_000, "git bisect start"))
    assert events == []


# --- parsing ----------------------------------------------------------------


def test_zsh_extended_and_plain_lines() -> None:
    text = "\n".join([zsh_line(1_700_000_000, "git push"), "git status"])
    events = parse_zsh(text)
    assert [e.subcommand for e in events] == ["push", "status"]
    assert events[0].dated is True
    assert events[1].dated is False


def test_zsh_global_flags_before_subcommand() -> None:
    events = parse_zsh(zsh_line(1_700_000_000, "git -C /tmp/repo --no-pager log --oneline"))
    assert events[0].subcommand == "log"


def test_wrapper_prefix_still_counts() -> None:
    events = parse_zsh(zsh_line(1_700_000_000, "sudo git fetch"))
    assert events[0].capability == "git-remote"


def test_bash_timestamp_pairs() -> None:
    text = "\n".join(["#1700000000", "git commit -m x", "#1700000060", "ls -la"])
    events = parse_bash(text)
    assert len(events) == 1
    assert events[0].subcommand == "commit"
    assert events[0].timestamp == 1_700_000_000


def test_fish_history() -> None:
    text = "\n".join(["- cmd: git merge main", "  when: 1700000000", "- cmd: vim x", "  when: 1"])
    events = parse_fish(text)
    assert [e.subcommand for e in events] == ["merge"]


# --- currency, the aviation rule --------------------------------------------


def test_capability_is_current_when_practised_enough() -> None:
    now = time.time()
    required, _ = CURRENCY["git-remote"]
    text = "\n".join(zsh_line(int(now - i * DAY), "git push") for i in range(required))
    result = {p.capability: p for p in practice(parse_zsh(text), now=now)}
    assert result["git-remote"].status == "current"


def test_capability_lapses_when_practice_is_too_old() -> None:
    now = time.time()
    required, within = CURRENCY["git-remote"]
    old = "\n".join(
        zsh_line(int(now - (within + 10) * DAY), "git push") for _ in range(required + 2)
    )
    result = {p.capability: p for p in practice(parse_zsh(old), now=now)}
    assert result["git-remote"].status == "lapsed"
    assert result["git-remote"].days_since > within


def test_capability_lapses_when_practice_is_too_thin() -> None:
    now = time.time()
    required, _ = CURRENCY["git-remote"]
    text = "\n".join(zsh_line(int(now - DAY), "git push") for _ in range(required - 1))
    result = {p.capability: p for p in practice(parse_zsh(text), now=now)}
    assert result["git-remote"].status == "lapsed"


def test_undated_entries_count_but_do_not_prove_recency() -> None:
    """The owner's real zsh history is mostly undated — it must not read as current."""
    text = "\n".join(["git push"] * 20)
    result = {p.capability: p for p in practice(parse_zsh(text))}
    remote = result["git-remote"]
    assert remote.total == 20
    assert remote.undated == 20
    assert remote.status == "unknown"
    assert remote.days_since is None


def test_missing_timestamps_are_reported_as_a_note(tmp_path: Path) -> None:
    history = tmp_path / ".zsh_history"
    history.write_text("git status\ngit log\n", encoding="utf-8")
    _, notes = read_events([history])
    assert notes and notes[0]["note"] == "no_timestamps"
    assert "EXTENDED_HISTORY" in notes[0]["message"]


def test_partial_timestamps_are_reported(tmp_path: Path) -> None:
    history = tmp_path / ".zsh_history"
    history.write_text("git status\n" + zsh_line(1_700_000_000, "git push") + "\n", encoding="utf-8")
    _, notes = read_events([history])
    assert notes[0]["note"] == "partial_timestamps"


def test_unreadable_and_missing_paths_do_not_crash(tmp_path: Path) -> None:
    report = measure([tmp_path / "gibt-es-nicht"])
    assert report["ok"] is True
    assert report["git_commands_total"] == 0


# --- the join ---------------------------------------------------------------


def test_blocked_and_rusty_is_priority_now() -> None:
    blocker = {"capabilities": [{"capability": "git-remote", "status": "due", "blocked_artifacts": 2, "practice": "tracks/04-tooling"}]}
    meter = {"capabilities": [{"capability": "git-remote", "status": "lapsed", "days_since": 40.0}]}
    row = combine(blocker, meter)["priorities"][0]
    assert row["capability"] == "git-remote"
    assert row["priority"] == "now"
    assert row["practice"] == "tracks/04-tooling"


def test_blocked_but_practised_is_only_soon() -> None:
    blocker = {"capabilities": [{"capability": "git-branch", "status": "due", "blocked_artifacts": 2}]}
    meter = {"capabilities": [{"capability": "git-branch", "status": "current", "days_since": 1.0}]}
    assert combine(blocker, meter)["priorities"][0]["priority"] == "soon"


def test_rusty_but_unblocked_is_only_watched() -> None:
    blocker = {"capabilities": [{"capability": "git-basics", "status": "watch", "blocked_artifacts": 1}]}
    meter = {"capabilities": [{"capability": "git-basics", "status": "lapsed", "days_since": 99.0}]}
    assert combine(blocker, meter)["priorities"][0]["priority"] == "watch"


def test_combine_is_json_serialisable() -> None:
    import json

    json.dumps(combine({"capabilities": []}, measure([])))
