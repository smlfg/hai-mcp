"""Work profile ("Arbeitsprofil"): owner-set capacity bounds for agent sessions.

The owner sets a work profile in a terminal dialog *before* agentic work starts.
Agents then work inside that profile without the owner releasing capacity again.
Without a profile the low defaults always apply; raised values are time-bounded
and fall back automatically (spec §7). Profiles live under ``HAI_HOME/profiles``,
never in the repo.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hai_mcp.config import Config
from hai_mcp.paths import PathError, require_project_path
from hai_mcp.storage import read_json, write_json

if TYPE_CHECKING:
    from hai_mcp.mission import MissionEngine

HOW_TO_CHANGE = (
    "Only the owner can change this: run `hai-mcp` in a terminal in the "
    "project folder (raises need one owner confirmation)."
)

DEFAULT_PROFILE: dict[str, Any] = {
    "max_active_missions": 1,
    "max_writer_sessions": 1,
    "lease_budget": 2,
    "lease_ttl_minutes": 30,
    "expires": "mission_end",
    "mode": "careful",
}

FAST_PRESET: dict[str, Any] = {
    "max_active_missions": 1,
    "max_writer_sessions": 5,
    "lease_budget": 20,
    "lease_ttl_minutes": 120,
    "expires": "mission_end",
    "mode": "fast",
}

WORK_PROFILE_KEYS = frozenset(
    {
        "max_writer_sessions",
        "lease_budget",
        "lease_ttl_minutes",
        "max_active_missions",
        "work_profile",
        "profile_mode",
    }
)

_EXAMPLES: dict[str, int] = {
    "max_writer_sessions": 4,
    "lease_budget": 10,
    "lease_ttl_minutes": 60,
    "max_active_missions": 1,
}

_PROFILE_FIELDS = ("max_active_missions", "max_writer_sessions", "lease_budget", "lease_ttl_minutes")


def _max_active_missions_cap() -> int:
    raw = os.environ.get("HAI_MAX_ACTIVE_MISSIONS_CAP", "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            return 1
        if value >= 1:
            return value
    return 1


def ranges() -> dict[str, tuple[int, int]]:
    return {
        "max_writer_sessions": (1, 10),
        "lease_budget": (1, 50),
        "lease_ttl_minutes": (5, 480),
        "max_active_missions": (1, _max_active_missions_cap()),
    }


RANGES = ranges()


def _day_boundary() -> tuple[str, str | None]:
    raw = os.environ.get("HAI_DAY_BOUNDARY", "").strip()
    if not raw:
        return "00:00", None
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return f"{hh:02d}:{mm:02d}", None
    return "00:00", f"HAI_DAY_BOUNDARY={raw!r} invalid; using '00:00'"


def current_day_boundary() -> str:
    return _day_boundary()[0]


def day_boundary_warning() -> str | None:
    return _day_boundary()[1]


def _aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return dt


def _aware_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    return _aware(now)


def _utc_iso(dt: datetime) -> str:
    return _aware(dt).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _split_boundary(boundary: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", boundary or "")
    if not m:
        return 0, 0
    hh, mm = int(m.group(1)), int(m.group(2))
    if 0 <= hh <= 23 and 0 <= mm <= 59:
        return hh, mm
    return 0, 0


def next_day_boundary(after: datetime, boundary: str) -> datetime:
    """First local day boundary strictly after ``after``."""
    local = _aware(after).astimezone()
    hh, mm = _split_boundary(boundary)
    candidate = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate


def parse_duration_minutes(s: str) -> int | None:
    """Parse ``4h``, ``90m`` or ``1h30m`` into total minutes; ``None`` if invalid."""
    s = str(s).strip()
    if not re.fullmatch(r"(?:\d+\s*[hm]\s*)+", s, re.IGNORECASE):
        return None
    total = 0
    for num, unit in re.findall(r"(\d+)\s*([hm])", s, re.IGNORECASE):
        total += int(num) * (60 if unit.lower() == "h" else 1)
    return total


def compute_expires_at(expires_choice: str, now: datetime, boundary: str) -> datetime:
    """Resolve an ``expires`` choice to a concrete local expiry, never beyond the day boundary."""
    local = _aware(now).astimezone()
    if expires_choice == "today_2359":
        today_end = local.replace(hour=23, minute=59, second=0, microsecond=0)
        return min(today_end, next_day_boundary(local, boundary))
    if str(expires_choice).startswith("duration:"):
        try:
            minutes = int(str(expires_choice).split(":", 1)[1])
        except (ValueError, IndexError):
            minutes = 0
        candidate = local + timedelta(minutes=minutes)
        return min(candidate, next_day_boundary(local, boundary))
    return next_day_boundary(local, boundary)


def parse_int_field(name: str, raw: Any) -> tuple[int | None, str]:
    lo, hi = ranges()[name]  # read env at call time (HAI_MAX_ACTIVE_MISSIONS_CAP)
    example = _EXAMPLES[name]
    err = f"{name} must be an integer {lo}–{hi}, e.g. {example}"
    if raw is None:
        return None, err
    if isinstance(raw, bool):
        return None, err
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str):
        s = raw.strip()
        if s == "":
            return None, err
        try:
            value = int(s)
        except ValueError:
            return None, err
    else:
        return None, err
    if not (lo <= value <= hi):
        return None, err
    return value, ""


def parse_mode(raw: Any) -> tuple[str, str]:
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return DEFAULT_PROFILE["mode"], ""
    s = str(raw).strip().lower()
    if s in ("careful", "fast"):
        return s, ""
    return "", "mode must be careful or fast, e.g. fast"


def parse_expires(raw: Any) -> tuple[str, str]:
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return DEFAULT_PROFILE["expires"], ""
    s = str(raw).strip()
    low = s.lower()
    if low in ("missionsende", "mission_end"):
        return "mission_end", ""
    if low in ("heute 23:59", "today", "heute"):
        return "today_2359", ""
    minutes = parse_duration_minutes(s)
    if minutes and minutes > 0:
        return f"duration:{minutes}", ""
    return "", (
        "expires must be 'missionsende'/'mission_end', 'heute 23:59'/'today', "
        "or a duration like '4h', '90m' or '1h30m', e.g. '4h'"
    )


def is_increase(values: dict[str, Any]) -> bool:
    for key in _PROFILE_FIELDS:
        current = values.get(key)
        if current is None:
            continue
        try:
            if int(current) > int(DEFAULT_PROFILE[key]):
                return True
        except (ValueError, TypeError):
            continue
    return False


def new_profile_id() -> str:
    return f"P-{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"


def _public_values(record: Any) -> dict[str, Any] | None:
    if not isinstance(record, dict):
        return None
    return {
        k: record.get(k)
        for k in ("mode", "max_active_missions", "max_writer_sessions", "lease_budget", "lease_ttl_minutes", "expires")
    }


class WorkProfileStore:
    """Per-project work profiles under HAI_HOME/profiles (never in the repo)."""

    def __init__(self, cfg: Config, engine: "MissionEngine") -> None:
        self.cfg = cfg
        self.engine = engine

    @property
    def profiles_dir(self) -> Path:
        return self.cfg.hai_home / "profiles"

    @property
    def waiting_path(self) -> Path:
        return self.profiles_dir / "waiting.json"

    def _key(self, project_path: str) -> str:
        resolved = require_project_path(project_path)
        return hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:16]

    def _path_for(self, project_path: str) -> Path:
        return self.profiles_dir / f"{self._key(project_path)}.json"

    def load(self, project_path: str) -> dict[str, Any] | None:
        record = read_json(self._path_for(project_path), None)
        return record if isinstance(record, dict) else None

    def save(self, record: dict[str, Any]) -> None:
        path = self._path_for(record["project_path"])
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        old = read_json(path, None)
        self.engine.append_audit(
            "work_profile_set",
            {
                "profile_id": record.get("profile_id"),
                "project_path": record.get("project_path"),
                "mission_id": record.get("mission_id"),
                "old": _public_values(old),
                "new": _public_values(record),
            },
        )
        write_json(path, record)

    def reset(self, project_path: str) -> dict[str, Any] | None:
        path = self._path_for(project_path)
        record = read_json(path, None)
        if not isinstance(record, dict):
            return None
        record["status"] = "reset"
        record["ended_reason"] = "reset"
        record["ended_at"] = _utc_iso(datetime.now(timezone.utc))
        write_json(path, record)
        self.engine.append_audit(
            "work_profile_reset",
            {
                "profile_id": record.get("profile_id"),
                "project_path": record.get("project_path"),
                "mission_id": record.get("mission_id"),
            },
        )
        return record

    def _ended_reason(self, record: dict[str, Any], now: datetime) -> str | None:
        mid = record.get("mission_id")
        if mid:
            try:
                meta = self.engine.load_mission_meta(mid)
            except Exception:  # invalid/unsafe id or unreadable meta: treat as closed (fail-closed)
                meta = None
            if not meta or meta.get("status") != "active":
                return "mission_closed"

        expires_at = record.get("expires_at")
        if expires_at:
            try:
                if now >= _parse_utc(str(expires_at)):
                    return "expired"
            except ValueError:
                pass

        created_at = record.get("created_at")
        if created_at:
            try:
                boundary = next_day_boundary(_parse_utc(str(created_at)), current_day_boundary())
                if now >= boundary:
                    return "day_rollover"
            except ValueError:
                pass
        return None

    def _expire(self, record: dict[str, Any], reason: str, now: datetime, path: Path) -> None:
        record["status"] = "expired"
        record["ended_reason"] = reason
        record["ended_at"] = _utc_iso(now)
        write_json(path, record)
        self.engine.append_audit(
            "work_profile_expired",
            {
                "profile_id": record.get("profile_id"),
                "project_path": record.get("project_path"),
                "mission_id": record.get("mission_id"),
                "ended_reason": reason,
            },
        )

    def active_for_mission(self, mission_id: str, now: datetime | None = None) -> dict[str, Any] | None:
        now = _aware_now(now)
        if not self.profiles_dir.is_dir():
            return None
        for path in sorted(self.profiles_dir.glob("*.json")):
            if path.name == "waiting.json":
                continue
            record = read_json(path, None)
            if not isinstance(record, dict):
                continue
            if record.get("status") != "active":
                continue
            if record.get("mission_id") != mission_id:
                continue
            reason = self._ended_reason(record, now)
            if reason:
                self._expire(record, reason, now, path)
                continue
            return record
        return None

    # ----- waiting sessions -------------------------------------------------

    def record_waiting(
        self,
        mission_id: str,
        agent_identity: str,
        waiting_for: str,
        limit: str,
        message: str,
    ) -> None:
        data = read_json(self.waiting_path, {"waiting": {}})
        waiting = data.setdefault("waiting", {})
        key = f"{mission_id}\x00{agent_identity}"
        existing = waiting.get(key)
        since = existing.get("since") if isinstance(existing, dict) else None
        if not since:
            since = _utc_iso(datetime.now(timezone.utc))
        waiting[key] = {
            "mission_id": mission_id,
            "agent_identity": agent_identity,
            "waiting_for": waiting_for,
            "limit": limit,
            "message": message,
            "since": since,
        }
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        write_json(self.waiting_path, data)

    def clear_waiting(self, mission_id: str, agent_identity: str) -> None:
        data = read_json(self.waiting_path, {"waiting": {}})
        waiting = data.setdefault("waiting", {})
        key = f"{mission_id}\x00{agent_identity}"
        if key in waiting:
            del waiting[key]
            self.profiles_dir.mkdir(parents=True, exist_ok=True)
            write_json(self.waiting_path, data)

    def list_waiting(self, mission_id: str, now: datetime | None = None) -> list[dict[str, Any]]:
        now = _aware_now(now)
        data = read_json(self.waiting_path, {"waiting": {}})
        out: list[dict[str, Any]] = []
        for entry in data.get("waiting", {}).values():
            if not isinstance(entry, dict) or entry.get("mission_id") != mission_id:
                continue
            since = entry.get("since")
            if since:
                try:
                    since_dt = _parse_utc(since)
                except ValueError:
                    since_dt = None
                if since_dt is not None and (now - since_dt) > timedelta(minutes=30):
                    continue
            out.append(entry)
        return out

    # ----- describe ---------------------------------------------------------

    def _count_active_sessions(self, mission_id: str | None) -> int:
        """Count leases that are still valid (not merely stored with status=active)."""
        if not mission_id:
            return 0
        try:
            sessions = self.engine.list_sessions(mission_id)
            contract = self.engine.current_contract(mission_id)
        except Exception:
            return 0
        count = 0
        for s in sessions:
            if s.get("status") != "active":
                continue
            ok, _, _ = self.engine._session_valid(s, contract, audit_expiry=False)
            if ok:
                count += 1
        return count

    def _contract_writer_limit(self, mission_id: str | None) -> int:
        if not mission_id:
            return 1
        try:
            contract = self.engine.current_contract(mission_id) or {}
            return int(contract.get("constraints", {}).get("max_parallel_sessions", 1))
        except Exception:
            return 1

    def _count_profile_sessions(self, mission_id: str | None, profile_id: str | None) -> int:
        if not mission_id or not profile_id:
            return 0
        try:
            sessions = self.engine.list_sessions(mission_id)
        except (ValueError, OSError):
            return 0
        return sum(1 for s in sessions if s.get("profile_id") == profile_id)

    def describe(
        self,
        project_path: str | None = None,
        mission_id: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        now = _aware_now(now)
        record: dict[str, Any] | None = None

        if project_path:
            try:
                path = self._path_for(project_path)
            except (PathError, ValueError):
                path = None
            if path is not None:
                loaded = read_json(path, None)
                if isinstance(loaded, dict) and loaded.get("status") == "active":
                    reason = self._ended_reason(loaded, now)
                    if reason:
                        self._expire(loaded, reason, now, path)
                    else:
                        record = loaded

        if record is None and mission_id:
            record = self.active_for_mission(mission_id, now=now)

        mid = (record or {}).get("mission_id") or mission_id
        if not mid:
            active = self.engine.load_active_pointer()
            if active.get("status") == "active":
                mid = active.get("mission_id")

        if record:
            source = "profile"
            values = {
                k: record.get(k, DEFAULT_PROFILE[k])
                for k in ("mode", "max_active_missions", "max_writer_sessions", "lease_budget", "lease_ttl_minutes", "expires")
            }
            expires_at = record.get("expires_at")
        else:
            source = "defaults"
            values = dict(DEFAULT_PROFILE)
            expires_at = None

        expires_at_local: str | None = None
        if expires_at:
            try:
                expires_at_local = _parse_utc(str(expires_at)).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
            except ValueError:
                expires_at_local = None

        active_writers = self._count_active_sessions(mid)
        # Mirrors authorize_session: the contract limit applies; a profile can only raise it.
        writer_limit = self._contract_writer_limit(mid)
        if record:
            writer_limit = max(writer_limit, int(values["max_writer_sessions"]))
        leases_used = self._count_profile_sessions(mid, (record or {}).get("profile_id")) if record else 0
        waiting = []
        if mid:
            waiting = [
                {
                    "agent_identity": e.get("agent_identity"),
                    "waiting_for": e.get("waiting_for"),
                    "reason": e.get("message"),
                    "since": e.get("since"),
                    "limit": e.get("limit"),
                }
                for e in self.list_waiting(mid, now=now)
            ]

        return {
            "ok": True,
            "source": source,
            "mode": values["mode"],
            "max_active_missions": values["max_active_missions"],
            "max_writer_sessions": values["max_writer_sessions"],
            "lease_budget": values["lease_budget"],
            "lease_ttl_minutes": values["lease_ttl_minutes"],
            "expires": values["expires"],
            "expires_at": expires_at,
            "expires_at_local": expires_at_local,
            "mission_id": mid,
            "active_writer_sessions": active_writers,
            "effective_writer_limit": writer_limit,
            "free_writer_slots": max(0, writer_limit - active_writers),
            "leases_used": leases_used,
            "leases_free": max(0, int(values["lease_budget"]) - leases_used),
            "waiting": waiting,
            "how_to_change": HOW_TO_CHANGE,
        }
