from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hai_mcp.boundary import strict_int
from hai_mcp.config import Config, ensure_hai_home
from hai_mcp.ids import validate_intake_id
from hai_mcp.locking import mission_state_lock
from hai_mcp.mission import MissionEngine, _new_id, _utc_now
from hai_mcp.paths import (
    PathError,
    assert_under,
    confined_artifact_dir,
    ensure_artifact_dir,
    require_project_path,
)
from hai_mcp.storage import atomic_write_text, read_json, write_json

ARTIFACT_NAMES = [
    "PROJECT_STATE.md",
    "PROMPT.md",
    "INTENT.md",
    "CODE_STATE.md",
    "BRIEFING.md",
    "NEXT_STEP.md",
    "NEXT_STEP.proposed.md",
    "EXECUTOR_HANDOFF_NEXT_STEP.md",
    "EXECUTION_REPORT_NEXT_STEP.md",
    "VALIDATION_REPORT_NEXT_STEP.md",
    "VERIFICATION_REPORT_NEXT_STEP.md",
    "LOOP_STATUS_NEXT_STEP.md",
    "BLOCKED_LOG_NEXT_STEP.md",
    "DRIFT_LOG.md",
]


@dataclass
class ControlPlane:
    cfg: Config

    def __post_init__(self) -> None:
        ensure_hai_home(self.cfg)
        self.mission = MissionEngine(self.cfg)

    @property
    def active_path(self) -> Path:
        return self.cfg.hai_home / "ACTIVE_CONTEXT.json"

    @property
    def inbox_dir(self) -> Path:
        return self.cfg.hai_home / "inbox"

    @property
    def checkpoints_dir(self) -> Path:
        return self.cfg.hai_home / "history" / "checkpoints"

    @property
    def intake_dir(self) -> Path:
        return self.cfg.hai_home / "intake"

    @property
    def distill_dir(self) -> Path:
        return self.cfg.hai_home / "distillations"

    @property
    def stop_dir(self) -> Path:
        return self.cfg.hai_home / "stops"

    def load_active(self) -> dict[str, Any]:
        data = read_json(self.active_path, {"version": 1, "focus_id": None, "active": []})
        if "active" not in data or not isinstance(data["active"], list):
            data["active"] = []
        return data

    def save_active(self, data: dict[str, Any]) -> None:
        write_json(self.active_path, data)

    def health(self, project_path: str | None = None) -> dict[str, Any]:
        home = ensure_hai_home(self.cfg)
        result: dict[str, Any] = {
            "ok": True,
            "server": "hai-mcp",
            "version": "0.1.0",
            "hai_home": str(home),
            "hai_home_writable": os.access(home, os.W_OK),
            "model_calls": False,
            "max_active_lanes": self.cfg.max_active_lanes,
        }
        if project_path:
            try:
                project = require_project_path(project_path)
                ad = confined_artifact_dir(self.cfg, project)
                result["project_path"] = str(project)
                result["artifact_dir"] = str(ad)
                result["artifact_dir_exists"] = ad.is_dir()
            except PathError as exc:
                return {"ok": False, "error": exc.code, "message": exc.message}
        return result

    def status(self, project_path: str | None = None) -> dict[str, Any]:
        active = self.load_active()
        inbox_count = len(list(self.inbox_dir.glob("*.md"))) if self.inbox_dir.is_dir() else 0
        out: dict[str, Any] = {
            "ok": True,
            "focus_id": active.get("focus_id"),
            "active": active.get("active", []),
            "active_count": len(active.get("active", [])),
            "max_active_lanes": self.cfg.max_active_lanes,
            "inbox_count": inbox_count,
            "hai_home": str(self.cfg.hai_home),
        }
        if project_path:
            try:
                project = require_project_path(project_path)
                ad = confined_artifact_dir(self.cfg, project)
                next_step = ad / "NEXT_STEP.md"
                out["project_path"] = str(project)
                out["has_next_step"] = next_step.is_file()
                out["has_proposed"] = (ad / "NEXT_STEP.proposed.md").is_file()
            except PathError as exc:
                return {"ok": False, "error": exc.code, "message": exc.message}
        return out

    def get_next_step(self, project_path: str) -> dict[str, Any]:
        try:
            project = require_project_path(project_path)
            ad = confined_artifact_dir(self.cfg, project)
            path = ad / "NEXT_STEP.md"
            if path.is_symlink():
                assert_under(path, ad)  # reject a symlinked NEXT_STEP.md escaping the artifact dir
        except PathError as exc:
            return {"ok": False, "error": exc.code, "message": exc.message}
        if not path.is_file():
            return {
                "ok": True,
                "exists": False,
                "path": str(path),
                "content": None,
                "source": "none",
            }
        return {
            "ok": True,
            "exists": True,
            "path": str(path),
            "content": path.read_text(encoding="utf-8"),
            "source": "canonical",
        }

    def read_artifacts(self, project_path: str, max_chars: Any = 4000) -> dict[str, Any]:
        try:
            project = require_project_path(project_path)
            ad = confined_artifact_dir(self.cfg, project)
        except PathError as exc:
            return {"ok": False, "error": exc.code, "message": exc.message}
        parsed, err = strict_int(max_chars, "max_chars", min_value=1)
        if err:
            return err
        assert parsed is not None
        max_chars = max(500, min(parsed, 20000))
        artifacts: dict[str, Any] = {}
        for name in ARTIFACT_NAMES:
            p = ad / name
            if p.is_symlink():
                try:
                    assert_under(p, ad)  # skip a symlinked artifact file escaping the artifact dir
                except PathError:
                    continue
            if not p.is_file():
                continue
            text = p.read_text(encoding="utf-8")
            truncated = len(text) > max_chars
            artifacts[name] = {
                "path": str(p),
                "chars": len(text),
                "truncated": truncated,
                "content": text[:max_chars],
            }
        return {
            "ok": True,
            "project_path": str(project),
            "artifact_dir": str(ad),
            "artifact_dir_exists": ad.is_dir(),
            "artifacts": artifacts,
            "present": sorted(artifacts.keys()),
        }

    def park(self, text: str, tags: list[str] | None = None) -> dict[str, Any]:
        if not text or not str(text).strip():
            return {"ok": False, "error": "invalid_args", "message": "text is required"}
        ts = time.strftime("%Y%m%dT%H%M%S")
        # unique suffix so multiple parks within the same second never overwrite (verlustfrei parken)
        slug = f"park_{ts}_{os.getpid()}_{uuid.uuid4().hex[:8]}.md"
        path = self.inbox_dir / slug
        tag_line = ", ".join(tags or [])
        body = (
            f"# Parked thought\n\n"
            f"- created: {time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n"
            f"- tags: {tag_line or '(none)'}\n\n"
            f"{text.strip()}\n"
        )
        before = self.load_active()
        atomic_write_text(path, body)
        after = self.load_active()
        return {
            "ok": True,
            "path": str(path),
            "active_unchanged": before == after,
            "active_count": len(after.get("active", [])),
            "message": "parked; ACTIVE lanes not modified",
        }

    def set_focus(
        self,
        focus_id: str,
        project_path: str | None = None,
        label: str | None = None,
    ) -> dict[str, Any]:
        if not focus_id or not str(focus_id).strip():
            return {"ok": False, "error": "invalid_args", "message": "focus_id is required"}
        focus_id = focus_id.strip()
        data = self.load_active()
        active: list[dict[str, Any]] = list(data.get("active", []))
        existing_ids = {item.get("id") for item in active}
        if focus_id not in existing_ids:
            if len(active) >= self.cfg.max_active_lanes:
                return {
                    "ok": False,
                    "error": "max_active_lanes",
                    "message": (
                        f"already {len(active)} ACTIVE lanes "
                        f"(max {self.cfg.max_active_lanes}); park or drop one first"
                    ),
                    "active": active,
                }
            entry: dict[str, Any] = {
                "id": focus_id,
                "label": label or focus_id,
                "activated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
            if project_path:
                try:
                    project = require_project_path(project_path)
                    entry["project_path"] = str(project)
                except PathError as exc:
                    return {"ok": False, "error": exc.code, "message": exc.message}
            active.append(entry)
        else:
            # update label/project if provided
            for item in active:
                if item.get("id") == focus_id:
                    if label:
                        item["label"] = label
                    if project_path:
                        try:
                            project = require_project_path(project_path)
                            item["project_path"] = str(project)
                        except PathError as exc:
                            return {"ok": False, "error": exc.code, "message": exc.message}
        data["active"] = active
        data["focus_id"] = focus_id
        self.save_active(data)
        return {
            "ok": True,
            "focus_id": focus_id,
            "active": active,
            "active_count": len(active),
        }

    def propose_next_step(self, project_path: str, content: str) -> dict[str, Any]:
        if not content or not str(content).strip():
            return {"ok": False, "error": "invalid_args", "message": "content is required"}
        try:
            project = require_project_path(project_path)
            ad = ensure_artifact_dir(self.cfg, project)
        except PathError as exc:
            return {"ok": False, "error": exc.code, "message": exc.message}
        path = ad / "NEXT_STEP.proposed.md"
        header = (
            f"<!-- proposed by hai-mcp at {time.strftime('%Y-%m-%dT%H:%M:%S%z')} "
            f"— not canonical until hai_accept_next_step -->\n\n"
        )
        atomic_write_text(path, header + content.strip() + "\n")
        return {
            "ok": True,
            "path": str(path),
            "canonical": False,
            "message": "proposal written; call hai_accept_next_step with owner_ack=true to promote",
        }

    def accept_next_step(
        self,
        project_path: str,
        owner_ack: Any,
        reason: str,
        content: str | None = None,
    ) -> dict[str, Any]:
        if owner_ack is not True:
            return {
                "ok": False,
                "error": "owner_gate_required",
                "message": "hai_accept_next_step requires owner_ack=true (literal boolean) and a reason",
            }
        if not reason or not str(reason).strip():
            return {
                "ok": False,
                "error": "owner_gate_required",
                "message": "reason is required when accepting a next step",
            }
        try:
            project = require_project_path(project_path)
            ad = ensure_artifact_dir(self.cfg, project)
        except PathError as exc:
            return {"ok": False, "error": exc.code, "message": exc.message}
        proposed = ad / "NEXT_STEP.proposed.md"
        if content and str(content).strip():
            body = content.strip() + "\n"
        elif proposed.is_file():
            body = proposed.read_text(encoding="utf-8")
            # strip proposal marker lines
            lines = [
                ln
                for ln in body.splitlines()
                if not ln.startswith("<!-- proposed by hai-mcp")
            ]
            body = "\n".join(lines).strip() + "\n"
        else:
            return {
                "ok": False,
                "error": "no_proposal",
                "message": "no NEXT_STEP.proposed.md and no content provided",
            }
        canonical = ad / "NEXT_STEP.md"
        history_dir = ad / "history"
        history_dir.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%S")
        if canonical.is_file():
            atomic_write_text(
                history_dir / f"NEXT_STEP_{stamp}_prev.md",
                canonical.read_text(encoding="utf-8"),
            )
        meta = (
            f"\n\n---\n"
            f"_Accepted via hai-mcp_\n"
            f"- at: {time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n"
            f"- reason: {reason.strip()}\n"
        )
        # avoid duplicating meta if re-accepting same body
        final = body.rstrip() + meta
        atomic_write_text(canonical, final)
        if proposed.is_file():
            proposed.unlink()
        hist_note = {
            "event": "accept_next_step",
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "reason": reason.strip(),
            "project_path": str(project),
            "path": str(canonical),
        }
        write_json(history_dir / f"accept_{stamp}.json", hist_note)
        return {
            "ok": True,
            "path": str(canonical),
            "canonical": True,
            "reason": reason.strip(),
        }

    def checkpoint(self, note: str | None = None, project_path: str | None = None) -> dict[str, Any]:
        stamp = time.strftime("%Y%m%dT%H%M%S")
        cp_dir = self.checkpoints_dir / stamp
        cp_dir.mkdir(parents=True, exist_ok=True)
        active = self.load_active()
        write_json(cp_dir / "ACTIVE_CONTEXT.json", active)
        payload: dict[str, Any] = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "note": note or "",
            "focus_id": active.get("focus_id"),
            "active": active.get("active", []),
        }
        if project_path:
            try:
                project = require_project_path(project_path)
                ad = confined_artifact_dir(self.cfg, project)
                art_snap: dict[str, str] = {}
                if ad.is_dir():
                    for name in ("NEXT_STEP.md", "NEXT_STEP.proposed.md", "BRIEFING.md", "PROJECT_STATE.md"):
                        p = ad / name
                        if p.is_symlink():
                            try:
                                assert_under(p, ad)  # skip symlinked artifact escaping the dir
                            except PathError:
                                continue
                        if p.is_file():
                            dest = cp_dir / "project" / name
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            atomic_write_text(dest, p.read_text(encoding="utf-8"))
                            art_snap[name] = str(dest)
                payload["project_path"] = str(project)
                payload["artifacts"] = art_snap
            except PathError as exc:
                return {"ok": False, "error": exc.code, "message": exc.message}
        write_json(cp_dir / "meta.json", payload)
        return {
            "ok": True,
            "checkpoint_id": stamp,
            "path": str(cp_dir),
            "meta": payload,
        }

    def open_mission(
        self,
        objective: str,
        artifact: str,
        done_criteria: list[Any] | None,
        non_goals: list[str] | None,
        constraints: dict[str, Any] | None,
        owner: str,
    ) -> dict[str, Any]:
        return self.mission.open_mission(
            objective=objective,
            artifact=artifact,
            done_criteria=done_criteria,
            non_goals=non_goals,
            constraints=constraints,
            owner=owner,
        )

    def authorize_session(
        self,
        mission_id: str,
        contract_version: Any,
        agent_identity: str,
        role: str,
        contribution: str,
        expected_result: str,
        duration_minutes: Any,
        capabilities: list[str] | None = None,
        criterion_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        return self.mission.authorize_session(
            mission_id=mission_id,
            contract_version=contract_version,
            agent_identity=agent_identity,
            role=role,
            contribution=contribution,
            expected_result=expected_result,
            duration_minutes=duration_minutes,
            capabilities=capabilities,
            criterion_ids=criterion_ids,
        )

    def get_contract(self, session_id: str) -> dict[str, Any]:
        return self.mission.get_contract(session_id)

    def check_activity(
        self,
        session_id: str,
        activity_step: str,
        affected_paths: list[str] | None = None,
        trace_events: list[dict[str, Any]] | None = None,
        criterion_id: str | None = None,
        activity_kind: str | None = None,
        evidence: dict[str, Any] | None = None,
        declares_blocker: Any = False,
    ) -> dict[str, Any]:
        return self.mission.check_activity(
            session_id=session_id,
            activity_step=activity_step,
            affected_paths=affected_paths,
            trace_events=trace_events,
            criterion_id=criterion_id,
            activity_kind=activity_kind,
            evidence=evidence,
            declares_blocker=declares_blocker,
        )

    def park_item(
        self,
        idea: str,
        origin_session_id: str,
        trigger_event: str,
        mission_id: str,
        rationale: str,
    ) -> dict[str, Any]:
        return self.mission.park_item(
            idea=idea,
            origin_session_id=origin_session_id,
            trigger_event=trigger_event,
            mission_id=mission_id,
            rationale=rationale,
        )

    def recontract(
        self,
        mission_id: str,
        contract_version: Any,
        reason: str,
        changes: dict[str, Any],
        mode: str = "normal",
        owner_ack: Any = False,
        break_glass_marker: Any = False,
    ) -> dict[str, Any]:
        return self.mission.recontract(
            mission_id=mission_id,
            contract_version=contract_version,
            reason=reason,
            changes=changes,
            mode=mode,
            owner_ack=owner_ack,
            break_glass_marker=break_glass_marker,
        )

    def close_mission(
        self,
        mission_id: str,
        contract_version: Any,
        evidence: dict[str, Any] | None,
        outcome_summary: str,
        closure: str,
        owner_ack: Any = False,
    ) -> dict[str, Any]:
        return self.mission.close_mission(
            mission_id=mission_id,
            contract_version=contract_version,
            evidence=evidence,
            outcome_summary=outcome_summary,
            closure=closure,
            owner_ack=owner_ack,
        )

    # --- Additional daily-loop flow surface (thin, over the hardened engine) ---

    def intake(self, raw: str) -> dict[str, Any]:
        """Capture a raw, immutable thought. Returns only an id — never actionable, never an agent trigger."""
        if not raw or not str(raw).strip():
            return {"ok": False, "error": "invalid_args", "message": "raw intake text is required"}
        intake_id = _new_id("I")
        self.intake_dir.mkdir(parents=True, exist_ok=True)
        path = self.intake_dir / f"{intake_id}.json"
        try:
            assert_under(self.intake_dir, self.cfg.hai_home)
            assert_under(path, self.cfg.hai_home)
        except PathError as exc:
            return {"ok": False, "error": exc.code, "message": exc.message}
        record = {
            "intake_id": intake_id,
            "raw": str(raw),
            "actionable": False,
            "captured_at": _utc_now(),
        }
        write_json(path, record)
        return {
            "ok": True,
            "intake_id": intake_id,
            "actionable": False,
            "message": "captured only; no decision and no action taken",
        }

    def distill(
        self,
        intake_id: str,
        decision: str,
        next_step: str,
        parklist: list[str] | None = None,
    ) -> dict[str, Any]:
        """Reduce an intake to EXACTLY one decision + one next step; everything else is parked.

        The server enforces cardinality: 0 or >1 decisions/steps are rejected, and a single
        field carrying a bundled multi-line plan is refused (crude anti-smuggle guard).
        """
        ok, msg = validate_intake_id(intake_id)
        if not ok:
            return {"ok": False, "error": "invalid_args", "message": msg}
        ipath = self.intake_dir / f"{intake_id}.json"
        if not ipath.is_file():
            return {"ok": False, "error": "invalid_args", "message": f"intake not found: {intake_id}"}

        decision = str(decision or "").strip()
        next_step = str(next_step or "").strip()
        if not decision:
            return {"ok": False, "error": "invalid_args", "message": "exactly one decision is required"}
        if not next_step:
            return {"ok": False, "error": "invalid_args", "message": "exactly one next_step is required"}
        if "\n" in decision or "\n" in next_step:
            return {
                "ok": False,
                "error": "invalid_args",
                "message": "decision and next_step must each be a single item (no bundled multi-line plan)",
            }

        parklist = [str(x).strip() for x in (parklist or []) if str(x).strip()]
        parked_paths: list[str] = []
        for item in parklist:
            r = self.park(item, tags=["distilled", intake_id])
            if r.get("ok"):
                parked_paths.append(r["path"])

        distill_id = _new_id("A")
        self.distill_dir.mkdir(parents=True, exist_ok=True)
        path = self.distill_dir / f"{distill_id}.json"
        try:
            assert_under(self.distill_dir, self.cfg.hai_home)
            assert_under(path, self.cfg.hai_home)
        except PathError as exc:
            return {"ok": False, "error": exc.code, "message": exc.message}
        record = {
            "distill_id": distill_id,
            "intake_id": intake_id,
            "decision": decision,
            "next_step": next_step,
            "parked_count": len(parked_paths),
            "distilled_at": _utc_now(),
        }
        write_json(path, record)
        return {
            "ok": True,
            "intake_id": intake_id,
            "decision": decision,
            "next_step": next_step,
            "parked_count": len(parked_paths),
            "message": "one decision, one next step; the rest is parked",
        }

    def stop_day(
        self,
        day: str,
        loop_closed: bool,
        clearer: str,
        agency_gained: str,
    ) -> dict[str, Any]:
        """Hard terminal for the workday: record the three answers, invalidate active leases.

        Does NOT complete or abandon a mission (that needs proof/owner_ack); the mission is
        left paused (leases revoked) and there is deliberately no next-day plan.
        """
        day = str(day or "").strip()
        if not day:
            return {"ok": False, "error": "invalid_args", "message": "day is required"}

        paused_mission = None
        revoked: list[str] = []
        with mission_state_lock(self.cfg.hai_home):
            active = self.mission.load_active_pointer()
            if active.get("mission_id") and active.get("status") == "active":
                paused_mission = active["mission_id"]
                revoked = self.mission.revoke_all_sessions(paused_mission, reason="day_stopped")

        stop_id = _new_id("A")
        self.stop_dir.mkdir(parents=True, exist_ok=True)
        path = self.stop_dir / f"{stop_id}.json"
        try:
            assert_under(self.stop_dir, self.cfg.hai_home)
            assert_under(path, self.cfg.hai_home)
        except PathError as exc:
            return {"ok": False, "error": exc.code, "message": exc.message}
        record = {
            "stop_id": stop_id,
            "day": day,
            "loop_closed": bool(loop_closed),
            "clearer": str(clearer or "").strip(),
            "agency_gained": str(agency_gained or "").strip(),
            "paused_mission": paused_mission,
            "revoked_sessions": revoked,
            "next_mission": None,
            "stopped_at": _utc_now(),
        }
        write_json(path, record)
        return {
            "ok": True,
            "status": "day_stopped",
            "day": day,
            "paused_mission": paused_mission,
            "revoked_sessions": revoked,
            "next_mission": None,
            "message": "day sealed; leases invalidated; no next-day plan; parked items stay parked",
        }

    def recover(self, checkpoint_id: str | None = None) -> dict[str, Any]:
        cps = sorted(
            [p for p in self.checkpoints_dir.iterdir() if p.is_dir()],
            key=lambda p: p.name,
        )
        if not cps and not checkpoint_id:
            active = self.load_active()
            return {
                "ok": True,
                "mode": "no_checkpoint",
                "next_action": "Call hai_status, then hai_get_next_step on the focus project. If unclear, park meta thoughts and set one focus.",
                "focus_id": active.get("focus_id"),
                "active": active.get("active", []),
            }
        if checkpoint_id:
            cp = self.checkpoints_dir / checkpoint_id
            if not cp.is_dir():
                return {
                    "ok": False,
                    "error": "invalid_args",
                    "message": f"checkpoint not found: {checkpoint_id}",
                }
        else:
            cp = cps[-1]
        meta = read_json(cp / "meta.json", {})
        snap_active = read_json(cp / "ACTIVE_CONTEXT.json", {})
        return {
            "ok": True,
            "mode": "from_checkpoint",
            "checkpoint_id": cp.name,
            "path": str(cp),
            "meta": meta,
            "snapshot_active": snap_active,
            "next_action": (
                "Do not rebuild architecture. Restore focus from snapshot if needed, "
                "read NEXT_STEP.md, execute only that step or ask Owner."
            ),
        }
