"""Limits logbook: the owner's append-only, self-expiring capacity escape hatch."""

from __future__ import annotations

import json
import time
from pathlib import Path

from hai_mcp.limits_logbook import (
    RESTORED,
    SUSPENDED,
    LimitsSuspension,
    active_suspension,
    logbook_path,
)


def _utc(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _append(hai_home: Path, record: dict) -> None:
    path = logbook_path(hai_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def test_missing_logbook_means_not_suspended(tmp_path: Path) -> None:
    assert active_suspension(tmp_path / "hai-home", now=1000.0) is None


def test_active_suspension_is_reported_with_reason(tmp_path: Path) -> None:
    home = tmp_path / "hai-home"
    _append(
        home,
        {
            "record_type": SUSPENDED,
            "at": _utc(1000.0),
            "reason": "Habe heute Zeit und Lust",
            "until": _utc(5000.0),
        },
    )
    suspension = active_suspension(home, now=2000.0)
    assert isinstance(suspension, LimitsSuspension)
    assert suspension.reason == "Habe heute Zeit und Lust"
    assert suspension.remaining_seconds == 3000


def test_expired_suspension_is_ignored(tmp_path: Path) -> None:
    home = tmp_path / "hai-home"
    _append(
        home,
        {
            "record_type": SUSPENDED,
            "at": _utc(1000.0),
            "reason": "kurz",
            "until": _utc(2000.0),
        },
    )
    assert active_suspension(home, now=3000.0) is None


def test_restored_record_ends_the_suspension(tmp_path: Path) -> None:
    home = tmp_path / "hai-home"
    _append(
        home,
        {
            "record_type": SUSPENDED,
            "at": _utc(1000.0),
            "reason": "x",
            "until": _utc(5000.0),
        },
    )
    _append(home, {"record_type": RESTORED, "at": _utc(2000.0), "reason": "fertig"})
    assert active_suspension(home, now=3000.0) is None


def test_suspension_without_a_reason_is_ignored(tmp_path: Path) -> None:
    home = tmp_path / "hai-home"
    _append(
        home,
        {
            "record_type": SUSPENDED,
            "at": _utc(1000.0),
            "reason": "   ",
            "until": _utc(5000.0),
        },
    )
    assert active_suspension(home, now=2000.0) is None


def test_torn_line_does_not_hide_surrounding_records(tmp_path: Path) -> None:
    home = tmp_path / "hai-home"
    path = logbook_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "{ this is not json }\n"
        + json.dumps(
            {
                "record_type": SUSPENDED,
                "at": _utc(1000.0),
                "reason": "x",
                "until": _utc(5000.0),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    assert active_suspension(home, now=2000.0) is not None


def test_symlink_logbook_is_refused(tmp_path: Path) -> None:
    home = tmp_path / "hai-home"
    real = tmp_path / "real.jsonl"
    real.write_text("", encoding="utf-8")
    logbook = logbook_path(home)
    logbook.parent.mkdir(parents=True, exist_ok=True)
    logbook.symlink_to(real)
    assert active_suspension(home, now=2000.0) is None
