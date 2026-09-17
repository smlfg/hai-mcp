"""Recall: bring captured items back when they matter, and let them be closed.

Why this exists
---------------
Capture is the cheap half. ``HAI_HOME`` already does it well — intake, parking,
distillations — and the triage path runs: a distillation records a ``decision``
and a ``next_step`` for an intake item, usually on the day it arrives.

What is missing is the other direction. Nothing brings an item back *later*.
A thought parked six weeks ago stays parked; a ``next_step`` the owner decided
on is never asked about again. The corpus therefore grows and its value per
item falls, and the owner re-derives in conversation what his own system wrote
down in August.

So this module reports three backlogs, each derived from structure that is
already there rather than from a new instrument:

1. **intake without distillation** — captured, never triaged.
2. **next_step without an outcome** — a step the owner decided and nobody asked
   about since. A forgotten note is a missed chance; a forgotten decision is a
   broken promise to yourself, which is why this backlog ranks first.
3. **parking without an exit** — parked items have status ``parked`` and no
   terminal state, so the channel has no way to end an item.

Closing an item never edits it
------------------------------
A resolution is appended as its own record under ``HAI_HOME/resolutions``,
pointing at the item's id. The captured corpus is never rewritten, so a wrong
call is undone by deleting one file and nothing of the original is lost.
Resolutions carry ``seq`` and ``prev_hash`` like the audit chain: the log cannot
be quietly rewritten, only visibly torn.

Deliberately not here
---------------------
No search and no embeddings. The owner's problem is not finding — it is being
confronted. Search assumes someone goes looking, and in six weeks nobody did.
Retrieval earns its place later, for grouping and duplicates, not for this.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from hai_mcp.config import Config
from hai_mcp.ids import validate_generated_id
from hai_mcp.storage import read_json, write_json

#: How a resolution may end. ``promoted`` means it became a mission.
OUTCOMES = frozenset({"resolved", "dropped", "promoted"})

#: Show at most this many per backlog. A command that presents 67 items is
#: ignored exactly like the directory it replaces.
DEFAULT_LIMIT = 3

#: Backlogs, most costly first. A decided-and-forgotten step outranks a note.
BACKLOG_ORDER = ("next_step_open", "parking_open", "intake_untriaged")

BACKLOG_TITLES = {
    "next_step_open": "Beschlossen und nie nachgefragt",
    "parking_open": "Geparkt ohne Ausgang",
    "intake_untriaged": "Eingegangen, nie triagiert",
}

_TAGS_RE = re.compile(r"^-\s*tags:\s*(.+)$", re.M)
_WORD_RE = re.compile(r"[a-zäöüß0-9-]{4,}", re.I)


class RecallError(ValueError):
    """Structured refusal, so a caller fails closed instead of guessing."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Item:
    """One thing that was captured and has not been closed."""

    backlog: str
    item_id: str
    created_at: str
    age_days: float | None
    summary: str
    detail: str
    source: str
    relevance: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _utc_iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts if ts is not None else time.time()))


def _canonical(data: Any) -> str:
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _parse_iso(value: str | None) -> float | None:
    """Accept the two shapes HAI_HOME actually contains, and give up quietly."""
    if not value:
        return None
    text = str(value).strip().replace("Z", "+0000")
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            return time.mktime(time.strptime(text, fmt)) - time.timezone
        except ValueError:
            continue
    return None


def _age_days(iso: str | None) -> float | None:
    stamp = _parse_iso(iso)
    if stamp is None:
        return None
    return round(max(time.time() - stamp, 0.0) / 86400.0, 1)


def _one_line(text: str, width: int = 150) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= width else flat[: width - 1].rstrip() + "…"


def _load_dir(path: Path, pattern: str = "*.json") -> list[dict[str, Any]]:
    if not path.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for file in sorted(path.glob(pattern)):
        try:
            data = read_json(file, None)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


# --- resolutions -------------------------------------------------------------


def resolutions_dir(cfg: Config) -> Path:
    return cfg.hai_home / "resolutions"


def resolutions_head_path(cfg: Config) -> Path:
    return resolutions_dir(cfg) / "HEAD.json"


def load_resolutions(cfg: Config) -> dict[str, dict[str, Any]]:
    """item_id -> the record that closed it. Later records win, but see :func:`resolve`."""
    out: dict[str, dict[str, Any]] = {}
    for record in _load_dir(resolutions_dir(cfg)):
        item_id = str(record.get("item_id", ""))
        if item_id:
            out[item_id] = record
    return out


def _new_resolution_id() -> str:
    import uuid

    return f"R-{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"


def resolve(
    cfg: Config,
    *,
    item_id: str,
    outcome: str,
    rationale: str,
    mission_id: str | None = None,
) -> dict[str, Any]:
    """Append a resolution. Never edits the captured item.

    Fail-closed on every unclear input: an unknown item, an empty rationale, a
    second resolution for the same item. ``promoted`` needs the mission it became,
    otherwise the claim cannot be checked later.
    """
    item_id = str(item_id or "").strip()
    rationale = str(rationale or "").strip()
    outcome = str(outcome or "").strip()

    if outcome not in OUTCOMES:
        raise RecallError("invalid_args", f"outcome must be one of {sorted(OUTCOMES)}")
    if not rationale:
        raise RecallError("invalid_args", "rationale is required — a closure without a reason is noise")

    ok, message = validate_generated_id(item_id)
    if not ok:
        raise RecallError("invalid_args", message)

    index = {item.item_id for item in collect(cfg, apply_resolutions=False)}
    if item_id not in index:
        raise RecallError("unknown_item", f"no open item with id {item_id}")

    existing = load_resolutions(cfg).get(item_id)
    if existing:
        raise RecallError(
            "already_resolved",
            f"{item_id} was closed as '{existing.get('outcome')}' on {existing.get('resolved_at')} "
            f"({existing.get('resolution_id')}); delete that record to reopen it",
        )

    if outcome == "promoted":
        ok, message = validate_generated_id(str(mission_id or ""), expected_prefix="M")
        if not ok:
            raise RecallError("invalid_args", f"promoted needs the mission it became: {message}")

    directory = resolutions_dir(cfg)
    directory.mkdir(parents=True, exist_ok=True)
    head = read_json(resolutions_head_path(cfg), {"seq": 0, "resolution_id": None, "record_hash": None})

    record = {
        "resolution_id": _new_resolution_id(),
        "item_id": item_id,
        "outcome": outcome,
        "rationale": rationale,
        "mission_id": mission_id if outcome == "promoted" else None,
        "resolved_at": _utc_iso(),
        "seq": int(head.get("seq", 0)) + 1,
        "prev_resolution_id": head.get("resolution_id"),
        "prev_hash": head.get("record_hash"),
    }
    digest = hashlib.sha256(_canonical(record).encode("utf-8")).hexdigest()
    record["record_hash"] = f"sha256:{digest}"

    write_json(directory / f"{record['resolution_id']}.json", record)
    write_json(
        resolutions_head_path(cfg),
        {"seq": record["seq"], "resolution_id": record["resolution_id"], "record_hash": record["record_hash"]},
    )
    return record


# --- collecting the backlogs -------------------------------------------------


def _intake_items(cfg: Config, distilled_ids: set[str]) -> list[Item]:
    out: list[Item] = []
    for entry in _load_dir(cfg.hai_home / "intake"):
        item_id = str(entry.get("intake_id", ""))
        if not item_id or item_id in distilled_ids:
            continue
        raw = str(entry.get("raw", ""))
        created = str(entry.get("captured_at", ""))
        out.append(
            Item(
                backlog="intake_untriaged",
                item_id=item_id,
                created_at=created,
                age_days=_age_days(created),
                summary=_one_line(raw),
                detail=raw,
                source="intake",
            )
        )
    return out


def _next_step_items(cfg: Config) -> list[Item]:
    out: list[Item] = []
    for entry in _load_dir(cfg.hai_home / "distillations"):
        item_id = str(entry.get("distill_id", ""))
        next_step = str(entry.get("next_step", "")).strip()
        if not item_id or not next_step:
            continue
        created = str(entry.get("distilled_at", ""))
        out.append(
            Item(
                backlog="next_step_open",
                item_id=item_id,
                created_at=created,
                age_days=_age_days(created),
                summary=_one_line(next_step),
                detail=f"Beschluss: {entry.get('decision', '')}\nNächster Schritt: {next_step}",
                source="distillations",
            )
        )
    return out


def _parking_items(cfg: Config) -> list[Item]:
    out: list[Item] = []
    for entry in _load_dir(cfg.hai_home / "parking"):
        item_id = str(entry.get("parking_id", ""))
        if not item_id or entry.get("status") != "parked":
            continue
        created = str(entry.get("parked_at", ""))
        idea = str(entry.get("idea", ""))
        out.append(
            Item(
                backlog="parking_open",
                item_id=item_id,
                created_at=created,
                age_days=_age_days(created),
                summary=_one_line(idea),
                detail=f"{idea}\n\nBegründung: {entry.get('rationale', '')}",
                source="parking",
            )
        )
    return out


def context_terms(cfg: Config) -> set[str]:
    """Words describing what the owner is focused on right now, from ACTIVE_CONTEXT."""
    data = read_json(cfg.hai_home / "ACTIVE_CONTEXT.json", {})
    if not isinstance(data, dict):
        return set()
    focus = str(data.get("focus_id", ""))
    blob = [focus]
    for entry in data.get("active", []) or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("id") == focus or not focus:
            blob += [str(entry.get("label", "")), str(entry.get("project_path", ""))]
    return {w.lower() for w in _WORD_RE.findall(" ".join(blob))}


def _score(item: Item, terms: set[str]) -> int:
    if not terms:
        return 0
    words = {w.lower() for w in _WORD_RE.findall(item.detail)}
    return len(words & terms)


def collect(cfg: Config, *, apply_resolutions: bool = True) -> list[Item]:
    """Every open item across the three backlogs, scored for relevance."""
    distilled = {
        str(d.get("intake_id", ""))
        for d in _load_dir(cfg.hai_home / "distillations")
        if d.get("intake_id")
    }
    items = _next_step_items(cfg) + _parking_items(cfg) + _intake_items(cfg, distilled)

    if apply_resolutions:
        closed = set(load_resolutions(cfg))
        items = [i for i in items if i.item_id not in closed]

    terms = context_terms(cfg)
    scored = [
        Item(**{**item.as_dict(), "relevance": _score(item, terms)})
        for item in items
    ]
    # Relevance first — recall, not archive — then the oldest, which rots longest.
    scored.sort(key=lambda i: (-i.relevance, -(i.age_days or 0.0)))
    return scored


def review(cfg: Config | None = None, *, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    cfg = cfg or Config.from_env()
    items = collect(cfg)
    backlogs = []
    for name in BACKLOG_ORDER:
        group = [i for i in items if i.backlog == name]
        backlogs.append(
            {
                "backlog": name,
                "title": BACKLOG_TITLES[name],
                "open": len(group),
                "shown": [i.as_dict() for i in group[:limit]],
            }
        )
    return {
        "ok": True,
        "generated_at": _utc_iso(),
        "hai_home": str(cfg.hai_home),
        "open_total": len(items),
        "context_terms": sorted(context_terms(cfg)),
        "backlogs": backlogs,
    }


def render_text(report: dict[str, Any]) -> str:
    lines = [f"WIEDERVORLAGE — {report['open_total']} offene Posten", ""]
    for backlog in report["backlogs"]:
        lines.append(f"{backlog['title'].upper()}  ({backlog['open']} offen)")
        if not backlog["shown"]:
            lines += ["  —", ""]
            continue
        for item in backlog["shown"]:
            age = f"{item['age_days']:.0f} Tage" if item["age_days"] is not None else "Alter unbekannt"
            flag = "  ⟵ passt zum aktuellen Fokus" if item["relevance"] else ""
            lines.append(f"  {item['item_id']}  ({age}){flag}")
            lines.append(f"      {item['summary']}")
        hidden = backlog["open"] - len(backlog["shown"])
        if hidden > 0:
            lines.append(f"      … {hidden} weitere nicht gezeigt")
        lines.append("")
    lines.append("Schliessen:  hai-recall resolve <id> --as resolved|dropped|promoted --why \"…\"")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    import sys

    args = list(sys.argv[1:] if argv is None else argv)

    parser = argparse.ArgumentParser(
        prog="hai-recall",
        description="Legt vor, was erfasst und nie geschlossen wurde. Erledigungen werden "
        "angehängt, nie in den Bestand hineingeschrieben.",
    )
    sub = parser.add_subparsers(dest="command")

    show = sub.add_parser("show", help="offene Posten vorlegen (Vorgabe)")
    show.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    show.add_argument("--json", action="store_true")

    close = sub.add_parser("resolve", help="einen Posten schliessen")
    close.add_argument("item_id")
    close.add_argument("--as", dest="outcome", required=True, choices=sorted(OUTCOMES))
    close.add_argument("--why", dest="rationale", required=True)
    close.add_argument("--mission", dest="mission_id", default=None)

    if not args or args[0] not in {"show", "resolve"}:
        args = ["show", *args]
    parsed = parser.parse_args(args)

    cfg = Config.from_env()

    if parsed.command == "resolve":
        try:
            record = resolve(
                cfg,
                item_id=parsed.item_id,
                outcome=parsed.outcome,
                rationale=parsed.rationale,
                mission_id=parsed.mission_id,
            )
        except RecallError as exc:
            print(json.dumps({"ok": False, "error": exc.code, "message": exc.message}, ensure_ascii=False))
            return 1
        print(
            f"geschlossen: {record['item_id']} als '{record['outcome']}' "
            f"({record['resolution_id']}, seq {record['seq']})"
        )
        return 0

    report = review(cfg, limit=parsed.limit)
    if parsed.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        print(render_text(report), end="")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
