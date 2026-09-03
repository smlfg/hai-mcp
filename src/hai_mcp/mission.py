from __future__ import annotations

import calendar
import copy
import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hai_mcp.boundary import strict_constraint_max_parallel, strict_int, strict_optional_time_limit_hours
from hai_mcp.config import Config, ensure_hai_home
from hai_mcp.ids import (
    IdentifierError,
    require_mission_id,
    validate_mission_id,
    validate_session_id,
)
from hai_mcp.locking import mission_state_lock
from hai_mcp.owner_gate import OwnerGate
from hai_mcp.paths import PathError, assert_relative_allowed, assert_under, real_path, require_project_path
from hai_mcp.projects import ProjectStore, validate_ident
from hai_mcp.storage import read_json, write_json

SENSITIVE_TRACE_ACTIONS = frozenset(
    {"commit", "push", "deploy", "delete", "secrets", "force_push", "rm_rf"}
)
VAGUE_OBJECTIVE_PATTERNS = (
    re.compile(r"^\s*an?\s+hai\s+arbeiten\s*$", re.I),
    re.compile(r"^\s*everything\s*$", re.I),
    re.compile(r"^\s*misc(?:ellaneous)?\s*$", re.I),
)
RECONTRACT_MUTABLE_FIELDS = frozenset({"objective", "artifact", "done_criteria", "non_goals", "constraints"})
RECONTRACT_SYSTEM_FIELDS = frozenset(
    {
        "mission_id",
        "contract_version",
        "contract_hash",
        "status",
        "created_at",
        "recontracted_at",
        "recontract_reason",
        "recontract_mode",
    }
)
VALID_RECONTRACT_MODES = frozenset({"normal", "blocker", "break_glass"})


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _new_id(prefix: str) -> str:
    return f"{prefix}-{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"


def _canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def contract_hash(contract_body: dict[str, Any]) -> str:
    payload = {k: v for k, v in contract_body.items() if k != "contract_hash"}
    digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _normalize_criteria(raw: list[Any] | None) -> tuple[list[dict[str, Any]], list[str]]:
    issues: list[str] = []
    if not raw:
        return [], ["done_criteria must be a non-empty finite list"]
    if not isinstance(raw, list):
        return [], ["done_criteria must be a list"]
    out: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for idx, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            issues.append(f"done_criteria[{idx - 1}] must be an object")
            continue
        cid = str(item.get("id") or f"dc-{idx}").strip()
        desc = str(item.get("description") or "").strip()
        if cid in seen_ids:
            issues.append(f"duplicate done_criteria id: {cid}")
        seen_ids.add(cid)
        if not desc:
            issues.append(f"done_criteria[{cid}] missing description")
        verifiable = item.get("verifiable", True)
        if verifiable is False:
            issues.append(f"done_criteria[{cid}] is not verifiable")
        out.append({"id": cid, "description": desc, "verifiable": bool(verifiable)})
    if not out:
        issues.append("done_criteria must contain at least one criterion")
    return out, issues


@dataclass
class MissionEngine:
    cfg: Config

    def __post_init__(self) -> None:
        ensure_hai_home(self.cfg)
        self.missions_dir.mkdir(parents=True, exist_ok=True)
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        self.parking_dir.mkdir(parents=True, exist_ok=True)
        self.owner_gate = OwnerGate(self.cfg, audit=self.append_audit)

    @property
    def missions_dir(self) -> Path:
        return self.cfg.hai_home / "missions"

    @property
    def audit_dir(self) -> Path:
        return self.cfg.hai_home / "audit"

    @property
    def parking_dir(self) -> Path:
        return self.cfg.hai_home / "parking"

    @property
    def audit_head_path(self) -> Path:
        return self.audit_dir / "HEAD.json"

    def _projects(self) -> ProjectStore:
        return ProjectStore(self.cfg.hai_home)

    def _contract_project_id(self, contract: dict[str, Any]) -> str | None:
        pid = contract.get("constraints", {}).get("project_id")
        if pid is None or not str(pid).strip():
            return None
        return str(pid).strip()

    def _resolve_project_root(
        self,
        contract: dict[str, Any],
        *,
        device_id: str | None = None,
        session: dict[str, Any] | None = None,
    ) -> tuple[Path | None, dict[str, Any] | None]:
        constraints = contract.get("constraints", {})
        project_id = self._contract_project_id(contract)
        if project_id:
            did = device_id or (session.get("device_id") if session else None)
            if not did:
                return None, {
                    "ok": False,
                    "error": "device_mount_required",
                    "message": "device_id is required for project_id missions",
                }
            mount = self._projects().get_mount_path(project_id, str(did))
            if mount is None:
                return None, {
                    "ok": False,
                    "error": "device_mount_required",
                    "message": f"no mount for device {did!r} on project {project_id!r}",
                }
            try:
                return require_project_path(str(mount)), None
            except PathError as exc:
                return None, {"ok": False, "error": exc.code, "message": exc.message}
        project_path = constraints.get("project_path")
        if project_path:
            try:
                return require_project_path(str(project_path)), None
            except PathError as exc:
                return None, {"ok": False, "error": exc.code, "message": exc.message}
        return None, None

    @property
    def active_pointer_path(self) -> Path:
        return self.missions_dir / "active_mission.json"

    def append_audit(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        with mission_state_lock(self.cfg.hai_home):
            event_id = _new_id("A")
            while (self.audit_dir / f"{event_id}.json").exists():
                event_id = _new_id("A")

            head = read_json(
                self.audit_head_path,
                {"seq": 0, "event_id": None, "event_hash": None},
            )
            seq = int(head.get("seq", 0)) + 1
            prev_event_id = head.get("event_id")
            prev_hash = head.get("event_hash")

            entry = {
                "event_id": event_id,
                "event_type": event_type,
                "at": _utc_now(),
                "seq": seq,
                "prev_event_id": prev_event_id,
                "prev_hash": prev_hash,
                **payload,
            }
            digest = hashlib.sha256(_canonical_json(entry).encode("utf-8")).hexdigest()
            event_hash = f"sha256:{digest}"
            entry["event_hash"] = event_hash

            path = self.audit_dir / f"{event_id}.json"
            write_json(path, entry)
            write_json(
                self.audit_head_path,
                {"seq": seq, "event_id": event_id, "event_hash": event_hash},
            )
            return entry

    def load_active_pointer(self) -> dict[str, Any]:
        return read_json(self.active_pointer_path, {"mission_id": None, "status": "none"})

    def save_active_pointer(self, mission_id: str | None, status: str) -> None:
        write_json(
            self.active_pointer_path,
            {"mission_id": mission_id, "status": status, "updated_at": _utc_now()},
        )

    def _reject_mission_id(self, mission_id: str) -> dict[str, Any] | None:
        ok, message = validate_mission_id(mission_id)
        if not ok:
            return {"ok": False, "error": "invalid_args", "message": message}
        return None

    def _reject_session_id(self, session_id: str) -> dict[str, Any] | None:
        ok, message = validate_session_id(session_id)
        if not ok:
            return {"ok": False, "error": "invalid_args", "message": message}
        return None

    def mission_dir(self, mission_id: str) -> Path:
        # Syntactically invalid id fails closed (IdentifierError), never masked as not-found.
        require_mission_id(mission_id)
        base = real_path(self.missions_dir)
        path = real_path(self.missions_dir / mission_id)
        try:
            path.relative_to(base)
        except ValueError as exc:
            raise PathError(
                "path_outside_root",
                f"mission path {path} is outside HAI_HOME missions root",
                path=str(path),
                root=str(base),
            ) from exc
        return self.missions_dir / mission_id

    def contract_path(self, mission_id: str, version: int) -> Path:
        path = self.mission_dir(mission_id) / "contracts" / f"v{int(version)}.json"
        assert_under(path, self.missions_dir)  # reject symlinked contracts/ escaping HAI_HOME
        return path

    def sessions_dir(self, mission_id: str) -> Path:
        path = self.mission_dir(mission_id) / "sessions"
        assert_under(path, self.missions_dir)  # reject symlinked sessions/ escaping HAI_HOME
        return path

    def load_contract(self, mission_id: str, version: int) -> dict[str, Any] | None:
        path = self.contract_path(mission_id, version)  # raises IdentifierError if invalid id
        if not path.is_file():
            return None
        return read_json(path, {})

    def current_contract(self, mission_id: str) -> dict[str, Any] | None:
        meta = self.load_mission_meta(mission_id)
        if not meta:
            return None
        return self.load_contract(mission_id, int(meta["current_version"]))

    def load_mission_meta(self, mission_id: str) -> dict[str, Any] | None:
        path = self.mission_dir(mission_id) / "mission.json"  # raises IdentifierError if invalid id
        if not path.is_file():
            return None
        return read_json(path, {})

    def list_sessions(self, mission_id: str) -> list[dict[str, Any]]:
        sdir = self.sessions_dir(mission_id)
        if not sdir.is_dir():
            return []
        out: list[dict[str, Any]] = []
        for p in sorted(sdir.glob("*.json")):
            out.append(read_json(p, {}))
        return out

    def save_session(self, session: dict[str, Any]) -> None:
        sid = session["session_id"]
        mission_id = session["mission_id"]
        if self._reject_session_id(sid) or self._reject_mission_id(mission_id):
            raise ValueError("invalid session or mission identifier")
        path = self.sessions_dir(mission_id) / f"{sid}.json"
        write_json(path, session)

    def revoke_all_sessions(self, mission_id: str, reason: str) -> list[str]:
        revoked: list[str] = []
        for session in self.list_sessions(mission_id):
            if session.get("status") == "active":
                session["status"] = "revoked"
                session["revoked_at"] = _utc_now()
                session["revoke_reason"] = reason
                self.save_session(session)
                revoked.append(session["session_id"])
        return revoked

    def _validate_open_payload(
        self,
        objective: str,
        artifact: str,
        done_criteria: list[Any] | None,
        non_goals: list[str] | None,
        constraints: dict[str, Any] | None,
        owner: str,
        *,
        require_mount_fields: bool = False,
    ) -> tuple[dict[str, Any] | None, list[str], str]:
        issues: list[str] = []
        objective = str(objective or "").strip()
        artifact = str(artifact or "").strip()
        owner = str(owner or "").strip()
        if not objective:
            issues.append("objective is required")
        elif any(pat.match(objective) for pat in VAGUE_OBJECTIVE_PATTERNS):
            issues.append("objective is too broad to be verifiable")
        if not artifact:
            issues.append("artifact is required")
        if not owner:
            issues.append("owner is required")

        criteria, crit_issues = _normalize_criteria(done_criteria)
        issues.extend(crit_issues)

        non_goals_norm = [str(x).strip() for x in (non_goals or []) if str(x).strip()]
        constraints = dict(constraints or {})
        project_id = constraints.get("project_id")
        device_id = constraints.get("device_id")
        project_path = constraints.get("project_path")
        allowed_paths = [str(p).strip() for p in constraints.get("allowed_paths", []) if str(p).strip()]
        for ap in allowed_paths:
            if ap.startswith("/") or ap.startswith("~") or ap.startswith("\\") or ".." in ap or "\x00" in ap:
                issues.append(f"allowed_paths entry must be a safe relative prefix: {ap}")
        capabilities = [str(c).strip() for c in constraints.get("capabilities", ["read", "write", "test"]) if str(c).strip()]
        max_parallel, mp_err = strict_constraint_max_parallel(constraints.get("max_parallel_sessions", 1))
        if mp_err:
            issues.append(mp_err["message"])
        elif max_parallel is not None and max_parallel < 1:
            issues.append("max_parallel_sessions must be >= 1")

        time_limit, tl_err = strict_optional_time_limit_hours(constraints.get("time_limit_hours"))
        if tl_err:
            issues.append(tl_err["message"])

        stored_project_id: str | None = None
        stored_project_path: str | None = None
        raw_project_id = constraints.get("project_id")
        if raw_project_id is not None:
            if not str(raw_project_id).strip():
                issues.append("project_id must not be empty")
            else:
                project_id = str(raw_project_id).strip()
                ok, msg = validate_ident(project_id, "project_id")
                if not ok:
                    issues.append(msg)
                else:
                    stored_project_id = project_id
                if require_mount_fields:
                    if not device_id:
                        issues.append("device_id is required when project_id is set")
                    else:
                        ok, msg = validate_ident(str(device_id), "device_id")
                        if not ok:
                            issues.append(msg)
                    if not project_path:
                        issues.append("project_path is required for first mount when project_id is set")
                    elif stored_project_id:
                        try:
                            require_project_path(str(project_path))
                        except PathError as exc:
                            issues.append(exc.message)
                stored_project_path = None
        elif project_path:
            try:
                require_project_path(str(project_path))
            except PathError as exc:
                issues.append(exc.message)
            stored_project_path = str(project_path)

        body = {
            "objective": objective,
            "artifact": artifact,
            "done_criteria": criteria,
            "non_goals": non_goals_norm,
            "constraints": {
                "owner": owner,
                "project_id": stored_project_id,
                "project_path": stored_project_path,
                "allowed_paths": allowed_paths,
                "capabilities": capabilities,
                "max_parallel_sessions": max_parallel if max_parallel is not None else 1,
                "time_limit_hours": time_limit,
            },
        }
        status = "review_required" if issues else "validated"
        return body, issues, status

    def open_mission(
        self,
        objective: str,
        artifact: str,
        done_criteria: list[Any] | None,
        non_goals: list[str] | None,
        constraints: dict[str, Any] | None,
        owner: str,
    ) -> dict[str, Any]:
        with mission_state_lock(self.cfg.hai_home):
            active = self.load_active_pointer()
            if active.get("mission_id") and active.get("status") == "active":
                return {
                    "ok": False,
                    "error": "active_mission_exists",
                    "message": "only one active mission is allowed until close or abandon",
                    "active_mission_id": active["mission_id"],
                }

            body, issues, validation = self._validate_open_payload(
                objective, artifact, done_criteria, non_goals, constraints, owner,
                require_mount_fields=True,
            )
            if validation == "review_required":
                audit = self.append_audit(
                    "mission_open_review_required",
                    {"issues": issues, "objective": objective, "artifact": artifact},
                )
                # Fail-closed: no mission was opened, so ok must be false.
                return {
                    "ok": False,
                    "error": "review_required",
                    "status": "review_required",
                    "validation": validation,
                    "issues": issues,
                    "audit_event_id": audit["event_id"],
                    "message": "mission not opened; contract payload needs review",
                }

            mission_id = _new_id("M")
            version = 1
            contract_body = {
                "mission_id": mission_id,
                "contract_version": version,
                "status": "active",
                "created_at": _utc_now(),
                **body,
            }
            contract_body["contract_hash"] = contract_hash(contract_body)

            stored_project_id = body["constraints"].get("project_id")
            open_device_id = (constraints or {}).get("device_id")
            open_project_path = (constraints or {}).get("project_path")
            if stored_project_id and open_device_id and open_project_path:
                _, mount_err = self._projects().bind_mount(
                    stored_project_id,
                    str(open_device_id),
                    str(open_project_path),
                )
                if mount_err:
                    return mount_err

            mdir = self.mission_dir(mission_id)
            (mdir / "contracts").mkdir(parents=True, exist_ok=True)
            self.sessions_dir(mission_id).mkdir(parents=True, exist_ok=True)

            write_json(self.contract_path(mission_id, version), contract_body)
            write_json(
                mdir / "mission.json",
                {
                    "mission_id": mission_id,
                    "status": "active",
                    "current_version": version,
                    "created_at": contract_body["created_at"],
                    "owner": owner,
                },
            )
            self.save_active_pointer(mission_id, "active")

            audit = self.append_audit(
                "mission_opened",
                {
                    "mission_id": mission_id,
                    "contract_version": version,
                    "contract_hash": contract_body["contract_hash"],
                },
            )
            return {
                "ok": True,
                "status": "active",
                "validation": validation,
                "mission_id": mission_id,
                "contract_version": version,
                "contract_hash": contract_body["contract_hash"],
                "contract": contract_body,
                "audit_event_id": audit["event_id"],
            }

    def bind_project(
        self,
        project_id: str,
        device_id: str,
        local_path: str,
        owner_ack: Any = False,
        reason: str = "",
    ) -> dict[str, Any]:
        if owner_ack is not True:
            return {
                "ok": False,
                "error": "owner_gate_required",
                "message": "hai_bind_project requires owner_ack=true (literal boolean) and a reason",
            }
        reason = str(reason or "").strip()
        if not reason:
            return {
                "ok": False,
                "error": "owner_gate_required",
                "message": "reason is required when binding a project mount",
            }

        ok, msg = validate_ident(str(project_id), "project_id")
        if not ok:
            return {"ok": False, "error": "invalid_args", "message": msg}
        ok, msg = validate_ident(str(device_id), "device_id")
        if not ok:
            return {"ok": False, "error": "invalid_args", "message": msg}

        with mission_state_lock(self.cfg.hai_home):
            data = self._projects().load()
            if project_id not in data.get("projects", {}):
                return {
                    "ok": False,
                    "error": "invalid_args",
                    "message": f"unknown project_id: {project_id}",
                }

            record, err = self._projects().bind_mount(project_id, device_id, local_path)
            if err:
                return err
            assert record is not None

            audit = self.append_audit(
                "project_mount_bound",
                {
                    "project_id": project_id,
                    "device_id": device_id,
                    "path": record["path"],
                    "reason": reason,
                },
            )
            return {
                "ok": True,
                "project_id": project_id,
                "device_id": device_id,
                "mount": record,
                "audit_event_id": audit["event_id"],
            }

    def _session_valid(
        self,
        session: dict[str, Any],
        contract: dict[str, Any] | None = None,
        *,
        audit_expiry: bool = True,
    ) -> tuple[bool, str, str]:
        session_id = str(session.get("session_id", ""))
        mission_id = str(session.get("mission_id", ""))
        if self._reject_session_id(session_id) or self._reject_mission_id(mission_id):
            return False, "invalid_args", "invalid session or mission identifier"

        meta = self.load_mission_meta(mission_id)
        if not meta or meta.get("status") != "active":
            return False, "mission_not_active", "mission is not active"

        active = self.load_active_pointer()
        if active.get("mission_id") != mission_id or active.get("status") != "active":
            return False, "mission_not_active", "mission is not the active mission"

        if contract is None:
            contract = self.current_contract(mission_id)
        if not contract:
            return False, "contract_version_mismatch", "current contract missing for mission"

        if int(session.get("contract_version", -1)) != int(meta.get("current_version", -2)):
            return False, "contract_version_mismatch", "session contract version is stale"
        if int(session.get("contract_version", -1)) != int(contract.get("contract_version", -2)):
            return False, "contract_version_mismatch", "session contract version does not match current contract"
        if session.get("contract_hash") != contract.get("contract_hash"):
            return False, "contract_version_mismatch", "session contract hash does not match current contract"

        expires_at = session.get("expires_at")
        if expires_at and _utc_now() > str(expires_at):
            if session.get("status") == "active":
                session["status"] = "expired"
                self.save_session(session)
                if audit_expiry:
                    self.append_audit(
                        "lease_expired",
                        {
                            "session_id": session_id,
                            "mission_id": mission_id,
                            "expires_at": expires_at,
                            "contract_version": session.get("contract_version"),
                            "contract_hash": session.get("contract_hash"),
                        },
                    )
            return False, "lease_expired", "session lease has expired"

        if session.get("status") != "active":
            return False, "lease_revoked", "session lease is revoked"

        return True, "", ""

    def authorize_session(
        self,
        mission_id: str,
        contract_version: Any,
        agent_identity: str,
        role: str,
        contribution: str,
        expected_result: str,
        duration_minutes: Any,
        capabilities: list[str] | None,
        criterion_ids: list[str] | None,
        device_id: str | None = None,
        harness_id: str | None = None,
    ) -> dict[str, Any]:
        err = self._reject_mission_id(mission_id)
        if err:
            return err

        version, ver_err = strict_int(contract_version, "contract_version", min_value=1)
        if ver_err:
            return ver_err
        assert version is not None

        agent_identity = str(agent_identity or "").strip()
        role = str(role or "").strip()
        contribution = str(contribution or "").strip()
        expected_result = str(expected_result or "").strip()
        if not agent_identity:
            return {"ok": False, "status": "denied", "error": "invalid_args", "message": "agent_identity is required"}
        if not role:
            return {"ok": False, "status": "denied", "error": "invalid_args", "message": "role is required"}
        if not contribution:
            return {
                "ok": False,
                "status": "denied",
                "error": "invalid_args",
                "message": "contribution is required",
            }
        if not expected_result:
            return {
                "ok": False,
                "status": "denied",
                "error": "invalid_args",
                "message": "expected_result is required",
            }

        duration_raw, dur_err = strict_int(duration_minutes, "duration_minutes", min_value=1)
        if dur_err:
            return dur_err
        assert duration_raw is not None

        with mission_state_lock(self.cfg.hai_home):
            meta = self.load_mission_meta(mission_id)
            if not meta or meta.get("status") != "active":
                return {"ok": False, "error": "mission_not_active", "message": f"mission not active: {mission_id}"}

            contract = self.load_contract(mission_id, version)
            if not contract:
                return {
                    "ok": False,
                    "error": "contract_version_mismatch",
                    "message": f"contract version {version} not found",
                }
            if int(meta["current_version"]) != version:
                return {
                    "ok": False,
                    "status": "denied",
                    "error": "contract_version_mismatch",
                    "message": "requested contract version is stale",
                    "current_version": meta["current_version"],
                }

            crit_ids = [str(c).strip() for c in (criterion_ids or []) if str(c).strip()]
            valid_ids = {c["id"] for c in contract.get("done_criteria", [])}
            if not crit_ids or not set(crit_ids).issubset(valid_ids):
                return {
                    "ok": False,
                    "status": "denied",
                    "error": "invalid_args",
                    "message": "contribution must reference at least one valid done criterion id",
                    "clause": "authorize_session.criterion_ids",
                }

            max_parallel = int(contract.get("constraints", {}).get("max_parallel_sessions", 1))
            active_sessions = [s for s in self.list_sessions(mission_id) if s.get("status") == "active"]
            now_valid = []
            for s in active_sessions:
                ok, _, _ = self._session_valid(s, contract)
                if ok:
                    now_valid.append(s)
            if len(now_valid) >= max_parallel:
                return {
                    "ok": False,
                    "status": "denied",
                    "error": "parallel_session_denied",
                    "message": f"max_parallel_sessions={max_parallel} already reached",
                    "clause": "constraints.max_parallel_sessions",
                }

            duration = max(1, min(duration_raw, 24 * 60))
            granted_caps = [str(c).strip() for c in (capabilities or []) if str(c).strip()]
            allowed_caps = set(contract.get("constraints", {}).get("capabilities", []))
            if granted_caps and not set(granted_caps).issubset(allowed_caps):
                return {
                    "ok": False,
                    "status": "denied",
                    "error": "invalid_args",
                    "message": "requested capabilities exceed contract allowances",
                    "clause": "constraints.capabilities",
                }

            contract_project_id = self._contract_project_id(contract)
            lease_device_id: str | None = None
            lease_harness_id: str | None = None

            if contract_project_id:
                if not device_id:
                    return {
                        "ok": False,
                        "status": "denied",
                        "error": "invalid_args",
                        "message": "device_id is required when contract has project_id",
                    }
                ok, msg = validate_ident(str(device_id), "device_id")
                if not ok:
                    return {"ok": False, "status": "denied", "error": "invalid_args", "message": msg}
                if self._projects().get_mount_path(contract_project_id, str(device_id)) is None:
                    return {
                        "ok": False,
                        "status": "denied",
                        "error": "device_mount_required",
                        "message": f"no mount for device {device_id!r}",
                    }
                lease_device_id = str(device_id)
            elif device_id:
                ok, msg = validate_ident(str(device_id), "device_id")
                if not ok:
                    return {"ok": False, "status": "denied", "error": "invalid_args", "message": msg}
                lease_device_id = str(device_id)

            if harness_id:
                ok, msg = validate_ident(str(harness_id), "harness_id")
                if not ok:
                    return {"ok": False, "status": "denied", "error": "invalid_args", "message": msg}
                lease_harness_id = str(harness_id)

            session_id = _new_id("S")
            granted_at = _utc_now()
            expires_at = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ",
                time.gmtime(time.time() + duration * 60),
            )
            lease = {
                "session_id": session_id,
                "mission_id": mission_id,
                "contract_version": version,
                "contract_hash": contract["contract_hash"],
                "agent_identity": agent_identity,
                "role": role,
                "contribution": contribution,
                "expected_result": expected_result,
                "criterion_ids": crit_ids,
                "capabilities": granted_caps or sorted(allowed_caps),
                "granted_at": granted_at,
                "expires_at": expires_at,
                "status": "active",
            }
            if lease_device_id:
                lease["device_id"] = lease_device_id
            if lease_harness_id:
                lease["harness_id"] = lease_harness_id
            self.save_session(lease)
            audit = self.append_audit(
                "session_authorized",
                {"mission_id": mission_id, "session_id": session_id, "contract_version": version},
            )
            return {
                "ok": True,
                "status": "granted",
                "session_id": session_id,
                "session_lease": lease,
                "audit_event_id": audit["event_id"],
            }

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        if self._reject_session_id(session_id):
            return None
        for mdir in sorted(self.missions_dir.glob("M-*")):
            if not validate_mission_id(mdir.name)[0]:
                continue
            try:
                sdir = self.sessions_dir(mdir.name)  # confined; raises if it escapes HAI_HOME
                path = sdir / f"{session_id}.json"
                assert_under(path, self.missions_dir)  # reject a symlinked session file
            except (PathError, IdentifierError):
                continue
            # do not follow a symlinked session record out of the store
            if not path.is_file() or path.is_symlink():
                continue
            data = read_json(path, {})
            # bind stored ids to the directory and the requested id
            if data.get("session_id") == session_id and data.get("mission_id") == mdir.name:
                return data
        return None

    def get_contract(self, session_id: str) -> dict[str, Any]:
        err = self._reject_session_id(session_id)
        if err:
            return err

        session = self.get_session(session_id)
        if not session:
            return {"ok": False, "error": "invalid_args", "message": f"session not found: {session_id}"}

        contract = self.current_contract(session["mission_id"])
        if not contract:
            return {"ok": False, "error": "contract_version_mismatch", "message": "contract missing for session"}

        ok, code, message = self._session_valid(session, contract)
        if not ok:
            return {
                "ok": False,
                "error": code,
                "message": message,
                "required_action": "pause",
            }

        remaining_seconds = 0
        expires_at = session.get("expires_at")
        if expires_at:
            try:
                exp_struct = time.strptime(str(expires_at), "%Y-%m-%dT%H:%M:%SZ")
                remaining_seconds = max(0, int(calendar.timegm(exp_struct) - time.time()))
            except ValueError:
                remaining_seconds = 0

        project_id = self._contract_project_id(contract)
        session_device_id = session.get("device_id")
        mount_path: str | None = None
        if project_id and session_device_id:
            mp = self._projects().get_mount_path(project_id, str(session_device_id))
            if mp is not None:
                mount_path = str(mp)

        return {
            "ok": True,
            "session_id": session_id,
            "mission_id": session["mission_id"],
            "contract_version": contract["contract_version"],
            "contract_hash": contract["contract_hash"],
            "remaining_seconds": remaining_seconds,
            "expires_at": expires_at,
            "project_id": project_id,
            "device_id": session_device_id,
            "mount_path": mount_path,
            "contract": contract,
            "done_criteria": contract.get("done_criteria", []),
            "non_goals": contract.get("non_goals", []),
        }

    def _matches_non_goal(self, text: str, non_goals: list[str]) -> str | None:
        lowered = text.lower()
        for ng in non_goals:
            token = ng.lower().strip()
            if token and token in lowered:
                return ng
        return None

    def _path_allowed(
        self,
        contract: dict[str, Any],
        raw_path: str,
        session: dict[str, Any] | None = None,
    ) -> tuple[bool, str | None]:
        constraints = contract.get("constraints", {})
        allowed_paths = constraints.get("allowed_paths", [])
        if not raw_path:
            return True, None
        root, root_err = self._resolve_project_root(contract, session=session)
        if root_err:
            return False, root_err.get("message", "device mount required")
        if not root:
            return False, "no project_path configured; path cannot be validated against a root"
        raw = str(raw_path)
        if "\x00" in raw:
            return False, "path contains a null byte"
        try:
            path = Path(raw).expanduser()
            if not path.is_absolute():
                path = root / path
            assert_relative_allowed(path, root, allowed_paths or [])
            return True, None
        except PathError as exc:
            return False, exc.message
        except (OSError, ValueError) as exc:
            return False, f"invalid path: {exc}"

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
        err = self._reject_session_id(session_id)
        if err:
            return {
                **err,
                "classification": "unclear",
                "required_action": "pause",
            }

        session = self.get_session(session_id)
        if not session:
            return {
                "ok": False,
                "error": "invalid_args",
                "message": f"session not found: {session_id}",
                "classification": "unclear",
                "required_action": "pause",
            }

        contract = self.current_contract(session["mission_id"])
        if not contract:
            return {
                "ok": False,
                "error": "contract_version_mismatch",
                "classification": "unclear",
                "required_action": "pause",
            }

        ok, code, message = self._session_valid(session, contract)
        if not ok:
            return {
                "ok": False,
                "error": code,
                "message": message,
                "classification": "unclear",
                "required_action": "pause",
            }

        step = str(activity_step or "").strip()
        crit_id = str(criterion_id or "").strip()
        valid_ids = {c["id"] for c in contract.get("done_criteria", [])}
        session_crits = set(session.get("criterion_ids", []))
        non_goals = contract.get("non_goals", [])
        caps = set(session.get("capabilities", []))

        classification = "unclear"
        required_action = "pause"
        reason = "no observable authorized linkage to contract criteria"
        clause: str | None = None

        path_violation: tuple[str, str] | None = None
        for raw in affected_paths or []:
            allowed, err_msg = self._path_allowed(contract, raw, session=session)
            if not allowed:
                path_violation = (err_msg or f"path outside allowed scope: {raw}", "constraints.allowed_paths")
                break

        cap_violation: tuple[str, str] | None = None
        if path_violation is None:
            for event in trace_events or []:
                action = str(event.get("action", "")).strip().lower()
                if action in SENSITIVE_TRACE_ACTIONS and action not in caps:
                    cap_violation = (f"trace action {action} lacks capability", "constraints.capabilities")
                    break

        if path_violation:
            classification = "drift"
            required_action = "stop"
            reason, clause = path_violation
        elif cap_violation:
            classification = "drift"
            required_action = "stop"
            reason, clause = cap_violation
        elif not step:
            classification = "unclear"
            required_action = "pause"
            reason = "missing activity_step"
        elif not crit_id:
            classification = "unclear"
            required_action = "pause"
            reason = "missing done criterion reference"
            clause = "done_criteria.reference_required"
        elif crit_id not in valid_ids:
            classification = "drift"
            required_action = "stop"
            reason = f"criterion {crit_id} is not part of the contract"
            clause = "done_criteria.scope"
        elif crit_id not in session_crits:
            classification = "drift"
            required_action = "request_recontract"
            reason = f"session not authorized for criterion {crit_id}"
            clause = "session.criterion_ids"
        else:
            ng = self._matches_non_goal(step, non_goals)
            if ng:
                classification = "park_candidate"
                required_action = "park"
                reason = f"activity matches non-goal: {ng}"
                clause = "non_goals"
            elif declares_blocker is True:
                classification = "blocker"
                required_action = "pause"
                reason = "activity declares a blocker preventing artifact completion"
                clause = "done_criteria.blocked"
            else:
                classification = "in_scope"
                required_action = "continue"
                reason = "activity aligns with authorized session criteria and paths"

        trace_actions = [
            str(event.get("action", "")).strip().lower()
            for event in (trace_events or [])
            if str(event.get("action", "")).strip()
        ]
        result = {
            "ok": True,
            "session_id": session_id,
            "mission_id": session["mission_id"],
            "contract_version": contract["contract_version"],
            "classification": classification,
            "required_action": required_action,
            "reason": reason,
            "clause": clause,
            "activity_kind": activity_kind,
            "criterion_id": crit_id or None,
            "evidence": evidence,
        }
        self.append_audit(
            "activity_checked",
            {
                "session_id": session_id,
                "mission_id": session["mission_id"],
                "contract_version": contract["contract_version"],
                "contract_hash": contract.get("contract_hash"),
                "classification": classification,
                "required_action": required_action,
                "reason": reason,
                "clause": clause,
                "activity_step_present": bool(step),
                "criterion_id": crit_id or None,
                "declares_blocker": declares_blocker,
                "affected_path_count": len(affected_paths or []),
                "trace_action_count": len(trace_actions),
                "trace_actions": trace_actions,
                "activity_kind": activity_kind,
            },
        )
        return result

    def park_item(
        self,
        idea: str,
        origin_session_id: str,
        trigger_event: str,
        mission_id: str,
        rationale: str,
    ) -> dict[str, Any]:
        idea = str(idea or "").strip()
        rationale = str(rationale or "").strip()
        trigger_event = str(trigger_event or "").strip()
        if not idea:
            return {"ok": False, "error": "invalid_args", "message": "idea is required"}
        if not rationale:
            return {"ok": False, "error": "invalid_args", "message": "rationale is required for mission-linked parking"}
        if not trigger_event:
            return {"ok": False, "error": "invalid_args", "message": "trigger_event is required"}

        err = self._reject_mission_id(mission_id)
        if err:
            return err
        origin_err = self._reject_session_id(origin_session_id)
        if origin_err:
            return origin_err

        meta = self.load_mission_meta(mission_id)
        if not meta:
            return {"ok": False, "error": "invalid_args", "message": f"mission not found: {mission_id}"}

        origin = self.get_session(origin_session_id)
        if not origin or origin.get("mission_id") != mission_id:
            return {
                "ok": False,
                "error": "invalid_args",
                "message": "origin_session_id must belong to the stated active mission",
            }

        parking_id = _new_id("P")
        record = {
            "parking_id": parking_id,
            "status": "parked",
            "idea": idea,
            "origin_session_id": origin_session_id,
            "trigger_event": trigger_event,
            "mission_id": mission_id,
            "rationale": rationale,
            "parked_at": _utc_now(),
            "actionable": False,
        }
        write_json(self.parking_dir / f"{parking_id}.json", record)
        audit = self.append_audit("item_parked", {"parking_id": parking_id, "mission_id": mission_id})
        return {
            "ok": True,
            "parking_id": parking_id,
            "status": "parked",
            "record": record,
            "audit_event_id": audit["event_id"],
            "message": "parked without execution rights; contract unchanged",
        }

    def _mutable_field_diff(self, old: dict[str, Any], new: dict[str, Any]) -> list[dict[str, Any]]:
        diff: list[dict[str, Any]] = []
        for key in sorted(RECONTRACT_MUTABLE_FIELDS):
            ov = old.get(key)
            nv = new.get(key)
            if ov != nv:
                diff.append({"field": key, "old": ov, "new": nv})
        return diff

    def _build_recontract_candidate(
        self,
        old_contract: dict[str, Any],
        changes: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        if not isinstance(changes, dict):
            return None, {"ok": False, "error": "invalid_args", "message": "changes must be an object"}

        for key in changes:
            if key in RECONTRACT_SYSTEM_FIELDS:
                return None, {
                    "ok": False,
                    "error": "invalid_args",
                    "message": f"system-managed field cannot be changed via recontract: {key}",
                }
            if key not in RECONTRACT_MUTABLE_FIELDS:
                return None, {
                    "ok": False,
                    "error": "invalid_args",
                    "message": f"unknown recontract field: {key}",
                }

        candidate = copy.deepcopy(old_contract)
        for key in RECONTRACT_MUTABLE_FIELDS:
            if key in changes:
                candidate[key] = copy.deepcopy(changes[key])

        old_owner = old_contract.get("constraints", {}).get("owner")
        old_project_id = old_contract.get("constraints", {}).get("project_id")
        old_project_path = old_contract.get("constraints", {}).get("project_path")
        if "constraints" in changes:
            new_constraints = changes.get("constraints")
            if isinstance(new_constraints, dict) and "project_id" in new_constraints:
                if new_constraints.get("project_id") != old_project_id:
                    return None, {
                        "ok": False,
                        "error": "invalid_args",
                        "message": "project_id cannot be changed via recontract",
                    }
        candidate_constraints = dict(candidate.get("constraints") or {})
        candidate_constraints["owner"] = old_owner
        candidate_constraints["project_id"] = old_project_id
        if old_project_id:
            candidate_constraints["project_path"] = None
        else:
            candidate_constraints["project_path"] = old_project_path
        candidate["constraints"] = candidate_constraints

        body, issues, validation = self._validate_open_payload(
            objective=str(candidate.get("objective", "")),
            artifact=str(candidate.get("artifact", "")),
            done_criteria=candidate.get("done_criteria"),
            non_goals=candidate.get("non_goals"),
            constraints=candidate.get("constraints"),
            owner=str(old_owner or ""),
        )
        if validation == "review_required":
            return None, {
                "ok": False,
                "error": "invalid_args",
                "message": "recontract candidate failed contract validation",
                "issues": issues,
            }

        candidate.update(body)
        candidate["mission_id"] = old_contract["mission_id"]
        candidate["contract_version"] = old_contract["contract_version"]
        candidate["status"] = old_contract.get("status", "active")
        candidate["created_at"] = old_contract.get("created_at")
        candidate.pop("contract_hash", None)
        return candidate, None

    def recontract(
        self,
        mission_id: str,
        contract_version: Any,
        reason: str,
        changes: dict[str, Any],
        mode: str = "normal",
        owner_ack: Any = False,
        break_glass_marker: Any = False,
        owner_code: Any = None,
    ) -> dict[str, Any]:
        err = self._reject_mission_id(mission_id)
        if err:
            return err

        reason = str(reason or "").strip()
        mode = str(mode or "normal").strip().lower()
        if not reason:
            return {"ok": False, "error": "invalid_args", "message": "reason is required"}
        if mode not in VALID_RECONTRACT_MODES:
            return {
                "ok": False,
                "error": "invalid_args",
                "message": f"mode must be one of: {', '.join(sorted(VALID_RECONTRACT_MODES))}",
            }

        version, ver_err = strict_int(contract_version, "contract_version", min_value=1)
        if ver_err:
            return ver_err
        assert version is not None

        meta = self.load_mission_meta(mission_id)
        if not meta or meta.get("status") != "active":
            return {"ok": False, "error": "mission_not_active", "message": "mission is not active"}

        if int(meta["current_version"]) != version:
            return {
                "ok": False,
                "error": "contract_version_mismatch",
                "message": "stale contract version",
                "current_version": meta["current_version"],
            }

        old_contract = self.load_contract(mission_id, version)
        if not old_contract:
            return {"ok": False, "error": "contract_version_mismatch", "message": "contract not found"}

        candidate, candidate_err = self._build_recontract_candidate(old_contract, changes)
        if candidate_err:
            return candidate_err
        assert candidate is not None

        diff = self._mutable_field_diff(old_contract, candidate)
        if not diff:
            return {"ok": False, "error": "invalid_args", "message": "recontract changes produce an empty diff"}

        # Owner gate. The diff is shown in both modes so the owner decides on the exact change.
        if self.cfg.owner_gate == "ack_legacy":
            if owner_ack is not True:
                return {
                    "ok": False,
                    "error": "owner_gate_required",
                    "gate": "ack_legacy",
                    "status": "pending_owner_confirmation",
                    "owner_ack_required": True,
                    "diff": diff,
                    "mode": mode,
                    "message": "owner_ack=true (literal boolean) required to apply contract changes",
                }
            gate_info: dict[str, Any] = {"gate": "ack_legacy"}
        else:
            passed, gate_result = self.owner_gate.require(
                action="recontract",
                subject={"mission_id": mission_id, "contract_version": version, "mode": mode, "diff": diff},
                summary=f"Recontract mission {mission_id} v{version} -> v{version + 1} ({mode})",
                preview={"mission_id": mission_id, "mode": mode, "reason": reason, "diff": diff},
                owner_code=owner_code,
            )
            if not passed:
                gate_result["diff"] = diff
                gate_result["mode"] = mode
                return gate_result
            gate_info = gate_result

        if mode == "break_glass" and break_glass_marker is not True:
            return {
                "ok": False,
                "error": "owner_gate_required",
                "message": "break_glass mode requires break_glass_marker=true",
                "audit_classification": "break_glass_denied",
            }

        with mission_state_lock(self.cfg.hai_home):
            meta = self.load_mission_meta(mission_id)
            if not meta or meta.get("status") != "active":
                return {"ok": False, "error": "mission_not_active", "message": "mission is not active"}
            if int(meta["current_version"]) != version:
                return {
                    "ok": False,
                    "error": "contract_version_mismatch",
                    "message": "stale contract version",
                    "current_version": meta["current_version"],
                }

            revoked = self.revoke_all_sessions(mission_id, reason="recontract")

            new_version = int(meta["current_version"]) + 1
            new_contract = copy.deepcopy(candidate)
            new_contract["contract_version"] = new_version
            new_contract["status"] = "active"
            new_contract["recontracted_at"] = _utc_now()
            new_contract["recontract_reason"] = reason
            new_contract["recontract_mode"] = mode
            new_contract.pop("contract_hash", None)
            new_contract["contract_hash"] = contract_hash(new_contract)

            write_json(self.contract_path(mission_id, new_version), new_contract)
            meta["current_version"] = new_version
            write_json(self.mission_dir(mission_id) / "mission.json", meta)

        audit_payload = {
            "mission_id": mission_id,
            "old_version": version,
            "new_version": new_version,
            "diff": diff,
            "mode": mode,
            "reason": reason,
            "revoked_sessions": revoked,
            "owner_gate": gate_info,
        }
        if mode == "break_glass":
            audit_payload["audit_classification"] = "break_glass"
        audit = self.append_audit("mission_recontracted", audit_payload)

        return {
            "ok": True,
            "status": "approved",
            "mission_id": mission_id,
            "contract_version": new_version,
            "contract_hash": new_contract["contract_hash"],
            "diff": diff,
            "contract": new_contract,
            "revoked_sessions": revoked,
            "audit_event_id": audit["event_id"],
            "audit_classification": audit_payload.get("audit_classification"),
            "owner_gate": gate_info,
        }

    def _verify_evidence_path(
        self,
        contract: dict[str, Any],
        raw_path: str,
        device_id: str | None = None,
    ) -> tuple[Path | None, str | None, dict[str, Any] | None]:
        if not raw_path or not str(raw_path).strip():
            return None, "evidence path is required", {"error": "invalid_args", "criterion_id": None}
        raw = str(raw_path)
        if "\x00" in raw:
            return None, "evidence path contains a null byte", {"error": "invalid_args", "path": raw}
        root, root_err = self._resolve_project_root(contract, device_id=device_id)
        if root_err:
            return None, root_err.get("message", "device mount required"), {
                "error": root_err.get("error", "device_mount_required"),
                "path": raw,
            }
        if not root:
            return None, "no project_path configured; evidence cannot be validated", {
                "error": "invalid_args",
                "path": raw,
            }
        try:
            project = root
            path = Path(raw).expanduser()
            if not path.is_absolute():
                path = project / path
            # Confinement is checked BEFORE any existence/type/read probe.
            if path.is_symlink():
                target = real_path(path)
                try:
                    target.relative_to(real_path(project))
                except ValueError:
                    return None, "evidence path escapes project via symlink", {
                        "error": "path_outside_root",
                        "path": str(path),
                        "root": str(project),
                    }
            confined = assert_under(path, project)
            if not confined.exists():
                return None, f"evidence path does not exist: {raw}", {"error": "invalid_args", "path": raw}
            if confined.is_dir():
                return None, f"evidence path is a directory, not a readable file: {raw}", {
                    "error": "invalid_args",
                    "path": raw,
                }
            if not confined.is_file():
                return None, f"evidence path is not a readable file: {raw}", {
                    "error": "invalid_args",
                    "path": raw,
                }
            digest = hashlib.sha256(confined.read_bytes()).hexdigest()
            return confined, None, {"path": str(confined), "sha256": f"sha256:{digest}"}
        except PathError as exc:
            return None, exc.message, exc.as_dict()
        except (OSError, ValueError) as exc:
            return None, f"evidence file is not readable: {raw}", {
                "error": "invalid_args",
                "path": raw,
                "message": str(exc),
            }

    def close_mission(
        self,
        mission_id: str,
        contract_version: Any,
        evidence: dict[str, Any] | None,
        outcome_summary: str,
        closure: str,
        owner_ack: Any = False,
        device_id: str | None = None,
        owner_code: Any = None,
    ) -> dict[str, Any]:
        err = self._reject_mission_id(mission_id)
        if err:
            return err

        closure = str(closure or "").strip().lower()
        outcome_summary = str(outcome_summary or "").strip()

        version, ver_err = strict_int(contract_version, "contract_version", min_value=1)
        if ver_err:
            return ver_err
        assert version is not None

        meta = self.load_mission_meta(mission_id)
        if not meta or meta.get("status") != "active":
            return {"ok": False, "error": "mission_not_active", "message": "mission is not active"}

        if int(meta["current_version"]) != version:
            return {
                "ok": False,
                "error": "contract_version_mismatch",
                "message": "stale contract version",
                "current_version": meta["current_version"],
            }

        contract = self.load_contract(mission_id, version)
        if not contract:
            return {"ok": False, "error": "contract_version_mismatch", "message": "contract not found"}

        contract_project_id = self._contract_project_id(contract)
        if contract_project_id:
            if not device_id:
                return {
                    "ok": False,
                    "error": "invalid_args",
                    "message": "device_id is required when contract has project_id",
                }
            ok, msg = validate_ident(str(device_id), "device_id")
            if not ok:
                return {"ok": False, "error": "invalid_args", "message": msg}
            if self._projects().get_mount_path(contract_project_id, str(device_id)) is None:
                return {
                    "ok": False,
                    "error": "device_mount_required",
                    "message": f"no mount for device {device_id!r}",
                }

        if closure == "abandoned":
            if self.cfg.owner_gate == "ack_legacy":
                if owner_ack is not True:
                    return {
                        "ok": False,
                        "error": "owner_gate_required",
                        "gate": "ack_legacy",
                        "message": "abandon requires owner_ack=true (literal boolean)",
                    }
                if not outcome_summary:
                    return {"ok": False, "error": "invalid_args", "message": "abandon reason required in outcome_summary"}
                gate_info: dict[str, Any] = {"gate": "ack_legacy"}
            else:
                if not outcome_summary:
                    return {"ok": False, "error": "invalid_args", "message": "abandon reason required in outcome_summary"}
                passed, gate_result = self.owner_gate.require(
                    action="abandon_mission",
                    subject={"mission_id": mission_id, "contract_version": version, "closure": "abandoned"},
                    summary=f"Abandon mission {mission_id} (contract v{version})",
                    preview={
                        "mission_id": mission_id,
                        "objective": str(contract.get("objective", ""))[:200],
                        "abandon_reason": outcome_summary[:200],
                    },
                    owner_code=owner_code,
                )
                if not passed:
                    return gate_result
                gate_info = gate_result
            with mission_state_lock(self.cfg.hai_home):
                revoked = self.revoke_all_sessions(mission_id, reason="abandoned")
                meta = self.load_mission_meta(mission_id) or meta
                meta["status"] = "abandoned"
                meta["closed_at"] = _utc_now()
                meta["abandon_reason"] = outcome_summary
                write_json(self.mission_dir(mission_id) / "mission.json", meta)
                self.save_active_pointer(None, "none")
            audit = self.append_audit(
                "mission_abandoned",
                {
                    "mission_id": mission_id,
                    "reason": outcome_summary,
                    "revoked_sessions": revoked,
                    "owner_gate": gate_info,
                },
            )
            return {
                "ok": True,
                "status": "abandoned",
                "mission_id": mission_id,
                "reason": outcome_summary,
                "revoked_sessions": revoked,
                "audit_event_id": audit["event_id"],
                "next_mission": None,
                "owner_gate": gate_info,
            }

        if closure != "completed":
            return {"ok": False, "error": "invalid_args", "message": "closure must be completed or abandoned"}

        if not outcome_summary:
            return {"ok": False, "error": "invalid_args", "message": "outcome_summary is required for completion"}

        if not isinstance(evidence, dict):
            return {"ok": False, "error": "invalid_args", "message": "evidence must be an object mapping criterion ids to records"}

        verified: dict[str, Any] = {}
        missing: list[str] = []
        invalid: list[dict[str, Any]] = []
        for crit in contract.get("done_criteria", []):
            cid = crit["id"]
            item = evidence.get(cid)
            if not isinstance(item, dict):
                missing.append(cid)
                continue
            raw_path = item.get("path")
            if not raw_path or not str(raw_path).strip():
                invalid.append(
                    {
                        "criterion_id": cid,
                        "error": "invalid_args",
                        "message": "evidence record requires a non-empty path",
                    }
                )
                continue
            _, err_msg, meta_ev = self._verify_evidence_path(contract, str(raw_path), device_id=device_id)
            if err_msg:
                item_out: dict[str, Any] = {"criterion_id": cid, "message": err_msg}
                if meta_ev:
                    item_out.update(meta_ev)
                invalid.append(item_out)
                continue
            verified[cid] = meta_ev

        if missing or invalid:
            return {
                "ok": True,
                "status": "incomplete",
                "missing_criteria": missing,
                "invalid_evidence": invalid,
                "verified_criteria": verified,
                "message": "completion requires verified evidence for every criterion",
            }

        with mission_state_lock(self.cfg.hai_home):
            revoked = self.revoke_all_sessions(mission_id, reason="completed")
            meta = self.load_mission_meta(mission_id) or meta
            meta["status"] = "completed"
            meta["closed_at"] = _utc_now()
            meta["outcome_summary"] = outcome_summary
            write_json(self.mission_dir(mission_id) / "mission.json", meta)
            self.save_active_pointer(None, "none")
        audit = self.append_audit(
            "mission_completed",
            {
                "mission_id": mission_id,
                "verified_criteria": verified,
                "outcome_summary": outcome_summary,
                "revoked_sessions": revoked,
            },
        )
        return {
            "ok": True,
            "status": "completed",
            "mission_id": mission_id,
            "verified_criteria": verified,
            "outcome_summary": outcome_summary,
            "revoked_sessions": revoked,
            "audit_event_id": audit["event_id"],
            "next_mission": None,
        }

    def list_audit_ids(self) -> list[str]:
        return sorted(p.stem for p in self.audit_dir.glob("A-*.json"))
