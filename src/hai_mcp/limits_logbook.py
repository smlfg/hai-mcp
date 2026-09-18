"""Read the owner's append-only limit-suspension logbook.

The owner can lift the earned worker capacity to the cap for a bounded time, at the cost of
writing down why. The record is produced by the HAI Worker Panel
(`HAI_UIV1/Sources/HAIClient/HAILimitsLogbook.swift`); this module is the reading half, so
the button actually moves something instead of repeating the mistake of
`OWNER_CONTRACT.json` — a file that was written but never read.

Format is one JSON object per line under `$HAI_HOME/logbook/limits.jsonl`. The current state
is *derived* from the last record rather than stored as a mutable flag, which is the same
rule the mission ledger follows and the reason two concurrent sessions cannot clobber each
other's reasons.

Nothing here raises. A missing, unreadable or malformed logbook simply means "not
suspended": the bonus may only ever widen capacity, never narrow it below the floor.
"""

from __future__ import annotations

import calendar
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SUSPENDED = "limits_suspended"
RESTORED = "limits_restored"


@dataclass(frozen=True)
class LimitsSuspension:
    reason: str
    started_at: str
    expires_at: str
    remaining_seconds: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "started_at": self.started_at,
            "expires_at": self.expires_at,
            "remaining_seconds": self.remaining_seconds,
        }


def logbook_path(hai_home: Path) -> Path:
    return hai_home / "logbook" / "limits.jsonl"


def _parse_utc(raw: Any) -> int | None:
    try:
        return calendar.timegm(time.strptime(str(raw), "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None


def active_suspension(hai_home: Path, now: float | None = None) -> LimitsSuspension | None:
    """The suspension in force, or None. Never raises."""
    path = logbook_path(hai_home)
    try:
        if path.is_symlink() or not path.is_file():
            return None
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None

    last: dict[str, Any] | None = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue  # a torn line must not hide the records around it
        if isinstance(record, dict) and record.get("record_type") in (SUSPENDED, RESTORED):
            last = record

    if last is None or last.get("record_type") != SUSPENDED:
        return None

    expires = _parse_utc(last.get("until"))
    started = last.get("at")
    if expires is None or not isinstance(started, str):
        return None

    moment = int(now if now is not None else time.time())
    if moment >= expires:
        return None  # ends by itself; nobody has to remember to revoke it

    reason = str(last.get("reason") or "").strip()
    if not reason:
        return None  # a suspension without a written reason is the silent bypass it replaces

    return LimitsSuspension(
        reason=reason,
        started_at=started,
        expires_at=str(last.get("until")),
        remaining_seconds=expires - moment,
    )
