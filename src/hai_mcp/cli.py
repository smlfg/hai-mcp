"""Interactive setup CLI ("Arbeitsprofil"): `hai-mcp`, `hai-mcp status`, `hai-mcp reset`.

Run in a TTY by the owner before agentic work starts. The dialog stores a
time-bounded work profile under HAI_HOME/profiles; agents then work inside it
without the owner releasing capacity again.
"""

from __future__ import annotations

import getpass
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from hai_mcp.config import Config
from hai_mcp.paths import PathError, require_project_path
from hai_mcp.state import ControlPlane
from hai_mcp.work_profile import (
    DEFAULT_PROFILE,
    FAST_PRESET,
    WorkProfileStore,
    compute_expires_at,
    current_day_boundary,
    day_boundary_warning,
    is_increase,
    new_profile_id,
    parse_duration_minutes,
    parse_expires,
    parse_int_field,
    parse_mode,
)

_PROFILE_FIELDS = ("max_active_missions", "max_writer_sessions", "lease_budget", "lease_ttl_minutes")


def confirm_increase(engine: Any, subject: dict[str, Any], io: tuple[Callable, Callable]) -> tuple[str, str | None]:
    """Exactly one owner confirmation for a work-profile raise.

    Returns ``(approved_via, challenge_id)``. Raises ``RuntimeError`` if the owner
    does not confirm (abort without saving).
    """
    input_fn, output_fn = io
    gate = engine.owner_gate
    action = "set_work_profile"
    gate_subject = subject.get("subject", {})
    summary = subject.get("summary", "Work profile")
    preview = subject.get("preview", {})

    if engine.cfg.owner_gate == "ack_legacy":
        while True:
            answer = input_fn("Bestätigen mit 'ja' (oder 'yes'): ").strip().lower()
            if answer in ("ja", "yes"):
                return "ack_legacy", None
            output_fn("Bitte 'ja' (oder 'yes') eingeben.")

    if engine.cfg.owner_channel == "touchid":
        passed, result = gate.require(
            action=action, subject=gate_subject, summary=summary, preview=preview, owner_code=None
        )
        if passed:
            return "touchid", result.get("challenge_id")
        if result.get("status") == "owner_approved":
            passed2, result2 = gate.require(
                action=action, subject=gate_subject, summary=summary, preview=preview, owner_code=None
            )
            if passed2:
                return "touchid", result2.get("challenge_id")
            raise RuntimeError(result2.get("message", "owner denied"))
        return _confirm_via_code(gate, action, gate_subject, summary, preview, result, input_fn, output_fn)

    passed, result = gate.require(
        action=action, subject=gate_subject, summary=summary, preview=preview, owner_code=None
    )
    if passed:
        return result.get("channel") or engine.cfg.owner_channel, result.get("challenge_id")
    return _confirm_via_code(gate, action, gate_subject, summary, preview, result, input_fn, output_fn)


def _confirm_via_code(gate, action, subject, summary, preview, result, input_fn, output_fn):
    if result.get("status") != "pending_owner_code":
        # Touch ID failed/cancelled, delivery failed, or the gate is otherwise closed: fail closed.
        raise RuntimeError(result.get("message") or result.get("error") or "owner confirmation failed")
    channel = result.get("channel") or "file"
    output_fn(f"Code wurde über '{channel}' zugestellt. Bitte Code eingeben (max. 3 Versuche).")
    for _ in range(3):
        code = input_fn("Code: ").strip()
        passed, res = gate.require(
            action=action, subject=subject, summary=summary, preview=preview, owner_code=code
        )
        if passed:
            return channel, res.get("challenge_id")
        output_fn("Code abgelehnt.")
    raise RuntimeError("Owner-Code nicht akzeptiert; Abbruch ohne Speichern.")


def _ask(input_fn: Callable, output_fn: Callable, label: str, default: Any, parser: Callable) -> Any | None:
    while True:
        raw = input_fn(f"{label} [{default}]: ").strip()
        if raw == "":
            return default
        value, err = parser(raw)
        if err:
            output_fn(err)
            continue
        return value


def _ask_ttl(input_fn: Callable, output_fn: Callable, default: int) -> int | None:
    while True:
        raw = input_fn(f"lease_ttl_minutes [{default} min]: ").strip()
        if raw == "":
            return default
        if re.fullmatch(r"(?:\d+\s*[hm]\s*)+", raw, re.IGNORECASE):
            minutes = parse_duration_minutes(raw)
            if minutes:
                value, err = parse_int_field("lease_ttl_minutes", minutes)
                if not err:
                    return value
                output_fn(err)
                continue
        value, err = parse_int_field("lease_ttl_minutes", raw)
        if err:
            output_fn(err)
            continue
        return value


def _choose_project(project_path: str | None, input_fn: Callable, output_fn: Callable) -> Path | None:
    raw = project_path if project_path else input_fn(f"Projekt [{os.getcwd()}]: ").strip() or os.getcwd()
    try:
        return require_project_path(raw)
    except PathError as exc:
        output_fn(f"Fehler: {exc.message} (z. B. {os.getcwd()})")
        return None


def _choose_mission(plane: ControlPlane, project: Path, input_fn: Callable, output_fn: Callable) -> tuple[str, int] | None:
    engine = plane.mission
    active = engine.load_active_pointer()
    if active.get("mission_id") and active.get("status") == "active":
        mid = active["mission_id"]
        meta = engine.load_mission_meta(mid) or {}
        contract = engine.current_contract(mid) or {}
        objective = str(contract.get("objective", "")).strip() or "(kein Ziel)"
        output_fn(f"Aktive Mission: {mid}")
        output_fn(f"Ziel: {objective}")
        bound = contract.get("constraints", {}).get("project_path")
        if bound and str(bound) != str(project):
            output_fn(f"Hinweis: Mission gehört zu {bound}, nicht zu {project}.")
        answer = input_fn("Mission übernehmen? [Enter=ja]: ").strip().lower()
        if answer in ("", "j", "y", "ja", "yes"):
            return mid, int(meta.get("current_version", 1))
        output_fn("Neue Mission anlegen.")
    else:
        output_fn("Keine aktive Mission — neue anlegen.")
    objective = input_fn("Ziel: ").strip()
    if not objective:
        output_fn("Fehler: Ziel ist erforderlich.")
        return None
    criteria_raw = input_fn("Fertig-Kriterien (kommagetrennt): ").strip()
    criteria = [c.strip() for c in criteria_raw.split(",") if c.strip()]
    if not criteria:
        output_fn("Fehler: mindestens ein Fertig-Kriterium erforderlich.")
        return None
    done_criteria = [{"id": f"dc-{i}", "description": c} for i, c in enumerate(criteria, start=1)]
    result = plane.open_mission(
        objective=objective,
        artifact=objective,
        done_criteria=done_criteria,
        non_goals=[],
        constraints={"project_path": str(project)},
        owner=getpass.getuser(),
    )
    if not result.get("ok"):
        output_fn(f"Fehler beim Anlegen der Mission: {result.get('message') or result.get('error')}")
        return None
    return result["mission_id"], result["contract_version"]


def _collect_values(input_fn: Callable, output_fn: Callable) -> dict[str, Any] | None:
    mode = _ask(input_fn, output_fn, "mode (careful/fast)", DEFAULT_PROFILE["mode"], parse_mode)
    if mode is None:
        return None
    preset = FAST_PRESET if mode == "fast" else DEFAULT_PROFILE
    values: dict[str, Any] = {"mode": mode}
    for field in ("max_active_missions", "max_writer_sessions", "lease_budget"):
        value = _ask(input_fn, output_fn, field, preset[field], lambda raw, f=field: parse_int_field(f, raw))
        if value is None:
            return None
        values[field] = value
    ttl = _ask_ttl(input_fn, output_fn, int(preset["lease_ttl_minutes"]))
    if ttl is None:
        return None
    values["lease_ttl_minutes"] = ttl
    expires = _ask(input_fn, output_fn, "expires", preset["expires"], parse_expires)
    if expires is None:
        return None
    values["expires"] = expires
    return values


def run_dialog(
    project_path: str | None = None,
    *,
    input_fn: Callable | None = None,
    output_fn: Callable | None = None,
    now: datetime | None = None,
    _confirm_increase: Callable | None = None,
) -> int:
    input_fn = input_fn or input
    output_fn = output_fn or print
    if _confirm_increase is None:
        _confirm_increase = confirm_increase
    if now is None:
        now = datetime.now().astimezone()

    cfg = Config.from_env()
    plane = ControlPlane(cfg)
    engine = plane.mission
    store = WorkProfileStore(cfg, engine)

    project = _choose_project(project_path, input_fn, output_fn)
    if project is None:
        return 1

    mission = _choose_mission(plane, project, input_fn, output_fn)
    if mission is None:
        return 1
    mission_id, contract_version = mission

    values = _collect_values(input_fn, output_fn)
    if values is None:
        return 1

    boundary = current_day_boundary()
    warning = day_boundary_warning()
    if warning:
        output_fn(warning)

    expires_at = compute_expires_at(values["expires"], now, boundary)
    expires_at_local = expires_at.astimezone().strftime("%Y-%m-%d %H:%M")

    old = store.describe(project_path=str(project), mission_id=mission_id, now=now)

    output_fn("Zusammenfassung:")
    for field in ("mode", "max_active_missions", "max_writer_sessions", "lease_budget", "lease_ttl_minutes", "expires"):
        output_fn(f"{field}: {old.get(field)} -> {values[field]}")
    output_fn(f"Läuft ab: {expires_at_local}")

    answer = input_fn("Speichern? [J/n] ").strip().lower()
    if answer and answer not in ("j", "y", "ja", "yes"):
        output_fn("Nicht gespeichert.")
        return 0

    approved_via = "none"
    challenge_id = None
    if is_increase(values):
        subject = {
            "project_path": str(project),
            "mission_id": mission_id,
            "mode": values["mode"],
            "max_active_missions": values["max_active_missions"],
            "max_writer_sessions": values["max_writer_sessions"],
            "lease_budget": values["lease_budget"],
            "lease_ttl_minutes": values["lease_ttl_minutes"],
            "expires": values["expires"],
        }
        try:
            approved_via, challenge_id = _confirm_increase(
                engine,
                {
                    "subject": subject,
                    "summary": f"Work profile: {project.name}",
                    "preview": subject,
                },
                (input_fn, output_fn),
            )
        except RuntimeError as exc:
            output_fn(f"Abbruch: {exc}")
            return 1

    record = {
        "profile_id": new_profile_id(),
        "project_path": str(project),
        "mission_id": mission_id,
        "contract_version": contract_version,
        "mode": values["mode"],
        "max_active_missions": values["max_active_missions"],
        "max_writer_sessions": values["max_writer_sessions"],
        "lease_budget": values["lease_budget"],
        "lease_ttl_minutes": values["lease_ttl_minutes"],
        "expires": values["expires"],
        "created_at": _utc_iso(now),
        "expires_at": _utc_iso(expires_at),
        "day_boundary": boundary,
        "approved_via": approved_via,
        "challenge_id": challenge_id,
        "status": "active",
        "ended_reason": None,
        "ended_at": None,
    }
    store.save(record)
    output_fn("Profil gespeichert.")
    return cli_status(str(project), output_fn=output_fn, now=now)


def _utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cli_status(project: str | None = None, json_out: bool = False, output_fn: Callable = print, now: datetime | None = None) -> int:
    cfg = Config.from_env()
    plane = ControlPlane(cfg)
    store = WorkProfileStore(cfg, plane.mission)
    project = project or os.getcwd()
    try:
        project = require_project_path(project)
    except PathError as exc:
        output_fn(f"Fehler: {exc.message}")
        return 1
    d = store.describe(project_path=str(project), now=now)
    if json_out:
        output_fn(json.dumps(d, indent=2, ensure_ascii=False, sort_keys=True))
        return 0
    if d["source"] == "profile":
        output_fn(f"Profil: {project}")
        output_fn(f"Modus: {d['mode']}")
    else:
        output_fn("Defaults (kein Profil)")
        output_fn(f"Modus: {d['mode']}")
    output_fn(f"Mission: {d.get('mission_id') or '(keine)'}")
    output_fn(f"Läuft ab: {d.get('expires_at_local') or '(kein Ablauf)'}")
    output_fn(f"Writer-Slots: {d['active_writer_sessions']} belegt, {d['free_writer_slots']} frei")
    output_fn(f"Leases: {d['leases_used']} belegt, {d['leases_free']} frei")
    for w in d.get("waiting", []):
        output_fn(f"Wartet: {w.get('agent_identity')} ({w.get('waiting_for')}) — {w.get('reason')}")
    output_fn(d["how_to_change"])
    return 0


def cli_reset(project: str | None = None, output_fn: Callable = print, now: datetime | None = None) -> int:
    cfg = Config.from_env()
    plane = ControlPlane(cfg)
    store = WorkProfileStore(cfg, plane.mission)
    project = project or os.getcwd()
    try:
        project = require_project_path(project)
    except PathError as exc:
        output_fn(f"Fehler: {exc.message}")
        return 1
    record = store.reset(str(project))
    if record is None:
        output_fn("Kein Profil vorhanden.")
    else:
        output_fn("Profil zurückgesetzt (Defaults).")
    return 0


def run_cli(
    argv: list[str],
    *,
    input_fn: Callable | None = None,
    output_fn: Callable | None = None,
    now: datetime | None = None,
    confirm_increase: Callable | None = None,
) -> int:
    input_fn = input_fn or input
    output_fn = output_fn or print
    sub = argv[0]
    rest = argv[1:]
    try:
        return _dispatch(sub, rest, input_fn, output_fn, now, confirm_increase)
    except (EOFError, KeyboardInterrupt):
        output_fn("\nAbgebrochen, nichts gespeichert.")
        return 130


def _dispatch(sub, rest, input_fn, output_fn, now, confirm_increase) -> int:

    def _take_project(args: list[str], *, allow_json: bool) -> tuple[str | None, bool, bool]:
        project: str | None = None
        json_out = False
        ok = True
        i = 0
        while i < len(args):
            arg = args[i]
            if arg == "--project":
                if i + 1 < len(args):
                    project = args[i + 1]
                    i += 2
                else:
                    output_fn("Fehler: --project benötigt einen Pfad.")
                    ok = False
                    break
            elif arg == "--json" and allow_json:
                json_out = True
                i += 1
            else:
                output_fn(f"Unbekanntes Argument: {arg}")
                ok = False
                break
        return project, json_out, ok

    if sub == "status":
        project, json_out, ok = _take_project(rest, allow_json=True)
        if not ok:
            return 1
        return cli_status(project, json_out=json_out, output_fn=output_fn, now=now)
    if sub == "reset":
        project, _, ok = _take_project(rest, allow_json=False)
        if not ok:
            return 1
        return cli_reset(project, output_fn=output_fn, now=now)
    if sub == "setup":
        return run_dialog(None, input_fn=input_fn, output_fn=output_fn, now=now, _confirm_increase=confirm_increase)
    output_fn(f"Unbekanntes Kommando: {sub}")
    return 1
