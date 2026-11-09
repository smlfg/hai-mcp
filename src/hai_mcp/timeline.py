"""Read-only mission observability: list missions and turn one into a timeline.

Pure functions over the existing stores (``missions/`` + the audit chain). Nothing here
writes, calls a model, or interprets free text: spans are derived from event types only.
Free text (objective, reasons, outcome summaries) is opt-in via ``include_text`` and is
sanitised and truncated before it reaches the diagram.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from hai_mcp.boundary import strict_int
from hai_mcp.ids import IdentifierError, validate_mission_id
from hai_mcp.mission import MissionEngine
from hai_mcp.paths import PathError
from hai_mcp.storage import read_json

TERMINAL_STATUSES = frozenset({"completed", "abandoned", "parked", "superseded"})
OPEN_STATUSES = frozenset({"active", "blocked"})
LIST_DEFAULT_LIMIT = 20
LIST_MAX_LIMIT = 100
TEXT_MAX_CHARS = 60
OBJECTIVE_LIST_MAX_CHARS = 120

_MERMAID_UNSAFE = re.compile(r"[:;#\r\n\"`<>{}|]")


def _parse_ts(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp; naive values are treated as UTC. Never raises."""
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_text(value: Any, limit: int = TEXT_MAX_CHARS) -> str:
    text = _MERMAID_UNSAFE.sub(" ", str(value or ""))
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _event_sort_key(event: dict[str, Any]) -> tuple[str, int, str]:
    seq = event.get("seq")
    return (str(event.get("at", "")), seq if isinstance(seq, int) else 0, str(event.get("event_id", "")))


def _load_audit_by_mission(engine: MissionEngine) -> dict[str, list[dict[str, Any]]]:
    """One pass over the audit store, grouped by mission. Unreadable files are skipped.

    ``mission_superseded`` is recorded under the *new* mission with ``superseded_mission_id``
    pointing at the old one; the old mission's timeline must end there, so the event is
    also filed under the old mission with a marker.
    """
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for path in engine.audit_dir.glob("A-*.json"):
        try:
            event = read_json(path, {})
        except (OSError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        mission_id = event.get("mission_id")
        old_id = event.get("superseded_mission_id")
        if event.get("event_type") == "mission_superseded" and isinstance(old_id, str):
            grouped[old_id].append({**event, "_ends_superseded": True})
            continue
        if isinstance(mission_id, str):
            grouped[mission_id].append(event)
    for events in grouped.values():
        events.sort(key=_event_sort_key)
    return grouped


def _count(events: list[dict[str, Any]], event_type: str) -> int:
    return sum(1 for e in events if e.get("event_type") == event_type)


def list_missions(
    engine: MissionEngine,
    *,
    since: str | None = None,
    until: str | None = None,
    status: str | None = None,
    limit: Any = LIST_DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Missions created in [since, until] (UTC), newest first. Read-only."""
    parsed_limit, err = strict_int(limit, "limit", min_value=1)
    if err:
        return err
    assert parsed_limit is not None
    parsed_limit = min(parsed_limit, LIST_MAX_LIMIT)

    since_dt = _parse_ts(since) if since is not None else None
    until_dt = _parse_ts(until) if until is not None else None
    if since is not None and since_dt is None:
        return {"ok": False, "error": "invalid_args", "message": "since must be an ISO-8601 timestamp"}
    if until is not None and until_dt is None:
        return {"ok": False, "error": "invalid_args", "message": "until must be an ISO-8601 timestamp"}
    if status is not None and (not isinstance(status, str) or not status.strip()):
        return {"ok": False, "error": "invalid_args", "message": "status must be a non-empty string"}

    audit = _load_audit_by_mission(engine)
    rows: list[dict[str, Any]] = []
    for mdir in engine.missions_dir.glob("M-*"):
        mission_id = mdir.name
        if not validate_mission_id(mission_id)[0]:
            continue
        try:
            meta = engine.load_mission_meta(mission_id)
        except (PathError, IdentifierError, OSError, ValueError):
            continue
        if not meta:
            continue
        created = _parse_ts(meta.get("created_at"))
        if created is None:
            continue
        if since_dt is not None and created < since_dt:
            continue
        if until_dt is not None and created > until_dt:
            continue
        if status is not None and meta.get("status") != status.strip():
            continue
        try:
            contract = engine.current_contract(mission_id) or {}
        except (PathError, IdentifierError, OSError, ValueError):
            contract = {}
        events = audit.get(mission_id, [])
        rows.append(
            {
                "mission_id": mission_id,
                "status": meta.get("status"),
                "created_at": _iso(created),
                "closed_at": meta.get("closed_at"),
                "contract_versions": int(meta.get("current_version") or 1),
                "blockers": _count(events, "mission_blocked"),
                "recontracts": _count(events, "mission_recontracted"),
                "objective": _safe_text(contract.get("objective"), OBJECTIVE_LIST_MAX_CHARS),
            }
        )
    rows.sort(key=lambda r: (r["created_at"], r["mission_id"]), reverse=True)
    total = len(rows)
    return {
        "ok": True,
        "timezone": "UTC",
        "total_matching": total,
        "returned": min(total, parsed_limit),
        "missions": rows[:parsed_limit],
    }


def _segment(kind: str, start: datetime, end: datetime, version: int, label: str | None = None) -> dict[str, Any]:
    if end < start:
        end = start
    out: dict[str, Any] = {
        "kind": kind,
        "version": version,
        "start": _iso(start),
        "end": _iso(end),
        "seconds": int((end - start).total_seconds()),
    }
    if label:
        out["label"] = label
    return out


def build_timeline(
    engine: MissionEngine,
    mission_id: str,
    *,
    include_text: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Derive spans (work / blocked), milestones and stats for one mission. Read-only.

    ``now`` is injectable so open missions are deterministic in tests.
    """
    ok, message = validate_mission_id(mission_id)
    if not ok:
        return {"ok": False, "error": "invalid_args", "message": message}
    try:
        meta = engine.load_mission_meta(mission_id)
    except (PathError, IdentifierError) as exc:
        return {"ok": False, "error": getattr(exc, "code", "invalid_args"), "message": str(exc)}
    if not meta:
        return {"ok": False, "error": "mission_not_found", "message": f"mission not found: {mission_id}"}

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    events = _load_audit_by_mission(engine).get(mission_id, [])
    status = str(meta.get("status") or "unknown")
    created = _parse_ts(meta.get("created_at"))
    first_at = _parse_ts(events[0].get("at")) if events else None
    start = min(t for t in (created, first_at) if t is not None) if (created or first_at) else now

    segments: list[dict[str, Any]] = []
    milestones: list[dict[str, Any]] = []
    version = 1
    work_start: datetime | None = None
    work_version = 1
    block_start: datetime | None = None
    block_version = 1
    block_label: str | None = None
    last_at = start
    stats = {
        "sessions": 0,
        "blockers": 0,
        "recontracts": 0,
        "drift_events": 0,
        "rejected_completions": 0,
        "lease_expiries": 0,
        "parked_items": 0,
        "activity_checks": 0,
    }

    def milestone(kind: str, at: datetime, label: str, ver: int) -> None:
        milestones.append({"kind": kind, "at": _iso(at), "version": ver, "label": label})

    def close_work(at: datetime) -> None:
        nonlocal work_start
        if work_start is not None:
            segments.append(_segment("work", work_start, at, work_version))
            work_start = None

    def close_block(at: datetime) -> None:
        nonlocal block_start, block_label
        if block_start is not None:
            segments.append(_segment("blocked", block_start, at, block_version, block_label))
            block_start = None
            block_label = None

    terminal_seen = False
    for event in events:
        at = _parse_ts(event.get("at")) or last_at
        last_at = max(last_at, at)
        etype = event.get("event_type")

        if event.get("_ends_superseded"):
            close_work(at)
            close_block(at)
            milestone("superseded", at, "Superseded", version)
            terminal_seen = True
        elif etype == "mission_opened":
            version = int(event.get("contract_version") or 1)
            milestone("opened", at, "Mission opened", version)
        elif etype == "session_authorized":
            stats["sessions"] += 1
            close_block(at)
            if work_start is None:
                work_start, work_version = at, version
        elif etype == "lease_expired":
            stats["lease_expiries"] += 1
            close_work(at)
        elif etype == "mission_blocked":
            stats["blockers"] += 1
            close_work(at)
            if block_start is None:
                block_start, block_version = at, version
                block_label = _safe_text(event.get("reason")) if include_text else None
        elif etype == "mission_resumed":
            close_block(at)
        elif etype == "mission_recontracted":
            stats["recontracts"] += 1
            close_work(at)
            close_block(at)
            new_version = event.get("new_version") or event.get("contract_version")
            version = int(new_version) if isinstance(new_version, int) else version + 1
            milestone("recontract", at, f"Recontract v{version}", version)
        elif etype == "activity_checked":
            stats["activity_checks"] += 1
            classification = event.get("classification")
            if classification == "drift":
                stats["drift_events"] += 1
                milestone("drift", at, "Drift detected", version)
        elif etype == "completion_rejected":
            stats["rejected_completions"] += 1
            milestone("completion_rejected", at, "Completion rejected", version)
        elif etype == "item_parked":
            stats["parked_items"] += 1
        elif etype in ("mission_completed", "mission_abandoned", "mission_parked"):
            close_work(at)
            close_block(at)
            label = {
                "mission_completed": "Mission completed",
                "mission_abandoned": "Mission abandoned",
                "mission_parked": "Mission parked",
            }[etype]
            milestone(etype.removeprefix("mission_"), at, label, version)
            terminal_seen = True

    is_open = status in OPEN_STATUSES and not terminal_seen
    if is_open:
        end = max(now, last_at)
    else:
        closed = _parse_ts(meta.get("closed_at"))
        end = max(last_at, closed) if closed else last_at
    close_work(end)
    close_block(end)

    total = max(0, int((end - start).total_seconds()))
    work_seconds = sum(s["seconds"] for s in segments if s["kind"] == "work")
    blocked_seconds = sum(s["seconds"] for s in segments if s["kind"] == "blocked")
    timeline: dict[str, Any] = {
        "ok": True,
        "mission_id": mission_id,
        "status": status,
        "open": is_open,
        "timezone": "UTC",
        "started_at": _iso(start),
        "ended_at": _iso(end),
        "contract_versions": int(meta.get("current_version") or version),
        "segments": segments,
        "milestones": milestones,
        "stats": {
            **stats,
            "total_seconds": total,
            "work_seconds": work_seconds,
            "blocked_seconds": blocked_seconds,
            "idle_seconds": max(0, total - work_seconds - blocked_seconds),
        },
    }
    if include_text:
        contract = engine.current_contract(mission_id) or {}
        timeline["objective"] = _safe_text(contract.get("objective"), 200)
        summary = meta.get("outcome_summary") or meta.get("abandon_reason")
        if summary:
            timeline["outcome"] = _safe_text(summary, 200)
    return timeline


def _fmt_duration(seconds: int) -> str:
    minutes, _ = divmod(max(0, seconds), 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02d}min"
    return f"{minutes}min"


def _gantt_ts(iso: str) -> str:
    return iso.replace("T", " ").rstrip("Z")


def render_mermaid(timeline: dict[str, Any]) -> str:
    """Mermaid gantt: one section per contract version, red bars for blockers.

    Labels are structural by default. The axis shows clock time for missions up to 24h and
    date + time beyond that.
    """
    stats = timeline["stats"]
    multi_day = stats["total_seconds"] > 24 * 3600
    title = (
        f"Mission {timeline['mission_id']} ({_fmt_duration(stats['total_seconds'])}, "
        f"{timeline['contract_versions']} contract versions, {timeline['status']})"
    )
    if timeline.get("objective"):
        title += f" - {timeline['objective']}"
    lines = [
        "gantt",
        f"    title {_safe_text(title, 200)}",
        "    dateFormat YYYY-MM-DD HH:mm:ss",
        f"    axisFormat {'%m-%d %H:%M' if multi_day else '%H:%M'}",
    ]

    items: list[tuple[str, int, str, dict[str, Any]]] = []
    for seg in timeline["segments"]:
        items.append((seg["start"], seg["version"], "segment", seg))
    for ms in timeline["milestones"]:
        items.append((ms["at"], ms["version"], "milestone", ms))
    items.sort(key=lambda it: (it[0], 0 if it[2] == "milestone" else 1))

    current_section: int | None = None
    for n, (at, version, kind, item) in enumerate(items, start=1):
        if version != current_section:
            lines.append(f"    section v{version}")
            current_section = version
        if kind == "milestone":
            tag = ":crit, milestone" if item["kind"] in ("drift", "completion_rejected") else ":milestone"
            lines.append(f"    {_safe_text(item['label'])} {tag}, i{n}, {_gantt_ts(at)}, 0s")
            continue
        end = _gantt_ts(item["end"]) if item["seconds"] > 0 else _gantt_ts(_bump_one_second(item["start"]))
        if item["kind"] == "blocked":
            label = "BLOCKER" + (f" {item['label']}" if item.get("label") else "")
            lines.append(f"    {_safe_text(label)} :crit, i{n}, {_gantt_ts(item['start'])}, {end}")
        else:
            lines.append(f"    Agent working :i{n}, {_gantt_ts(item['start'])}, {end}")
    return "\n".join(lines) + "\n"


def _bump_one_second(iso: str) -> str:
    parsed = _parse_ts(iso)
    assert parsed is not None
    return _iso(datetime.fromtimestamp(parsed.timestamp() + 1, tz=timezone.utc))


def mission_timeline(
    engine: MissionEngine,
    mission_id: str,
    *,
    include_text: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Tool-level result: timeline data + a ready-to-render Mermaid block."""
    timeline = build_timeline(engine, mission_id, include_text=include_text, now=now)
    if not timeline.get("ok"):
        return timeline
    mermaid = render_mermaid(timeline)
    return {**timeline, "mermaid": mermaid, "markdown": f"```mermaid\n{mermaid}```"}


__all__ = ["build_timeline", "list_missions", "mission_timeline", "render_mermaid"]
