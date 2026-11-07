"""Enforcement layer: one deterministic decision for every harness hook.

An MCP server can only offer tools; it cannot force an agent to call them. Harness hooks
(Claude Code / Codex `UserPromptSubmit` + `PreToolUse`, an OpenCode plugin, ...) can. Each
hook adapter normalizes its payload into a `GuardEvent` and asks `evaluate()` here. The
answer is computed from the same persistent `HAI_HOME` mission state the MCP server writes,
so every harness on a device sees the same active mission, across restarts.

Scope (same thesis as GOAL.md): this is a fail-closed guard rail for a cooperating human,
not an adversarial sandbox. Shell commands are not parsed exhaustively; the cwd, `cd`
targets and a few sensitive verbs are checked. No LLM calls.
"""

from __future__ import annotations

import fnmatch
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hai_mcp.paths import PathError, assert_under
from hai_mcp.state import ControlPlane

# Tools that never change files or external state. Anything not listed (including unknown
# MCP tools) is treated as acting and must happen inside the active mission's project.
READ_ONLY_TOOLS = frozenset(
    {
        "Read", "Glob", "Grep", "LS", "NotebookRead", "WebFetch", "WebSearch",
        "TodoWrite", "TodoRead", "BashOutput", "ToolSearch", "AskUserQuestion",
        "ExitPlanMode", "EnterPlanMode", "Task", "Agent",
        # Codex / OpenCode spellings
        "read", "glob", "grep", "list", "view_image", "web_search", "update_plan", "todowrite",
    }
)
FILE_WRITE_TOOLS = {
    "Write": ("file_path",),
    "Edit": ("file_path",),
    "MultiEdit": ("file_path",),
    "NotebookEdit": ("notebook_path",),
    "write": ("filePath", "file_path", "path"),
    "edit": ("filePath", "file_path", "path"),
}
PATCH_TOOLS = frozenset({"apply_patch", "patch"})
SHELL_TOOLS = frozenset({"Bash", "bash", "shell", "exec_command", "local_shell", "unified_exec"})
# HAI's own tools must always stay reachable: they are how an agent parks or the owner
# deliberately changes direction (recontract / close / supersede, all owner-gated).
HAI_TOOL_RE = re.compile(r"^(?:mcp__[^_].*?__)?hai_[a-z_]+$")
PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$|^\*\*\* Move to: (.+)$", re.M)


@dataclass
class GuardEvent:
    kind: str  # "prompt" | "tool"
    harness_id: str
    cwd: str
    device_id: str | None = None
    prompt: str = ""
    tool_name: str = ""
    tool_input: dict[str, Any] = field(default_factory=dict)


@dataclass
class Decision:
    allow: bool
    code: str
    reason: str
    mission_id: str | None = None
    intake_id: str | None = None
    audit_event_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


def _allowlisted(tool_name: str) -> bool:
    raw = os.environ.get("HAI_GUARD_ALLOW_TOOLS", "")
    return any(fnmatch.fnmatchcase(tool_name, p.strip()) for p in raw.split(",") if p.strip())


def _abs(raw: str, cwd: str) -> Path:
    p = Path(str(raw)).expanduser()
    return p if p.is_absolute() else Path(cwd) / p


def _shell_command(tool_input: dict[str, Any]) -> str:
    cmd = tool_input.get("command", tool_input.get("cmd", ""))
    if isinstance(cmd, list):
        return " ".join(str(c) for c in cmd)
    return str(cmd or "")


def _shell_facts(command: str) -> tuple[list[str], set[str]]:
    """Return (cd targets, sensitive actions) for a shell command. Best effort, not a parser."""
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = command.split()
    cd_targets: list[str] = []
    actions: set[str] = set()
    separators = {"&&", "||", ";", "|", "&"}
    segments: list[list[str]] = [[]]
    for tok in tokens:
        parts = re.split(r"(&&|\|\||;|\||&)", tok) if tok not in separators else [tok]
        for part in parts:
            if not part:
                continue
            if part in separators:
                segments.append([])
            else:
                segments[-1].append(part)
    for seg in segments:
        if not seg:
            continue
        head, rest = seg[0], seg[1:]
        if head in {"cd", "pushd"} and rest:
            cd_targets.append(rest[0])
        elif head == "git" and rest:
            sub, skip = "", False
            for t in rest:  # skip git's global options, including the ones taking a value
                if skip:
                    skip = False
                elif t in {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}:
                    skip = True
                elif not t.startswith("-"):
                    sub = t
                    break
            if sub == "commit":
                actions.add("commit")
            elif sub == "push":
                forced = any(t in {"-f", "--force", "--force-with-lease"} or t.startswith("+") for t in rest)
                actions.add("force_push" if forced else "push")
        elif head == "rm":
            flags = "".join(t.lstrip("-") for t in rest if t.startswith("-") and not t.startswith("--"))
            if "r" in flags.lower() or "--recursive" in rest:
                actions.add("rm_rf")
            else:
                actions.add("delete")
    return cd_targets, actions


class Guard:
    def __init__(self, plane: ControlPlane) -> None:
        self.plane = plane
        self.engine = plane.mission

    # -- helpers -----------------------------------------------------------------------
    def _deny(self, event: GuardEvent, code: str, reason: str, mission_id: str | None, *, park: bool = False) -> Decision:
        intake_id = None
        if park and event.prompt.strip():
            parked = self.plane.intake(event.prompt)
            intake_id = parked.get("intake_id") if parked.get("ok") else None
        audit = self.engine.append_audit(
            "guard_denied",
            {
                "mission_id": mission_id,
                "harness_id": event.harness_id,
                "device_id": event.device_id,
                "kind": event.kind,
                "tool_name": event.tool_name or None,
                "cwd": event.cwd,
                "code": code,
                "reason": reason,
                "intake_id": intake_id,
            },
        )
        return Decision(False, code, reason, mission_id, intake_id, audit["event_id"])

    def _under(self, path: Path, root: Path) -> bool:
        try:
            assert_under(path, root)
            return True
        except PathError:
            return False

    # -- decision ----------------------------------------------------------------------
    def evaluate(self, event: GuardEvent) -> Decision:
        if event.kind == "tool":
            name = event.tool_name
            if name in READ_ONLY_TOOLS or HAI_TOOL_RE.match(name) or _allowlisted(name):
                return Decision(True, "read_only_or_hai", f"{name} does not act outside HAI")

        active = self.engine.load_active_pointer()
        mission_id = active.get("mission_id") if active.get("status") == "active" else None
        contract = self.engine.current_contract(mission_id) if mission_id else None
        if not mission_id or not contract:
            if event.kind == "prompt":
                return Decision(True, "no_mission", "no active mission; prompts may open one")
            return self._deny(
                event,
                "no_active_mission",
                "No active HAI mission. Open one with hai_open_mission (or hai_mission_start) before acting.",
                None,
            )

        root, root_err = self.engine._resolve_project_root(contract, device_id=event.device_id)
        if root_err or root is None:
            msg = (root_err or {}).get("message", "mission has no project root to check against")
            return self._deny(
                event,
                (root_err or {}).get("error", "missing_project"),
                f"Active mission {mission_id}: {msg}. Bind this device with hai_bind_project (owner gate).",
                mission_id,
                park=event.kind == "prompt",
            )

        objective = str(contract.get("objective", ""))[:160]
        redirect = (
            f"Active HAI mission {mission_id} ('{objective}') is bound to {root}. "
            "Park the new idea (hai_park_item / hai_intake) or change direction deliberately "
            "via hai_recontract or hai_close_mission (owner gate)."
        )

        if not self._under(Path(event.cwd), root):
            return self._deny(
                event,
                "outside_mission",
                f"Working directory {event.cwd} is outside the mission project. {redirect}",
                mission_id,
                park=event.kind == "prompt",
            )

        if event.kind == "prompt":
            ng = self.engine._matches_non_goal(event.prompt, contract.get("non_goals", []))
            if ng:
                return self._deny(
                    event, "non_goal", f"Prompt matches mission non-goal {ng!r}. {redirect}", mission_id, park=True
                )
            return Decision(True, "in_mission", "prompt inside the active mission project", mission_id)

        return self._evaluate_tool(event, contract, mission_id, root, redirect)

    def _evaluate_tool(
        self, event: GuardEvent, contract: dict[str, Any], mission_id: str, root: Path, redirect: str
    ) -> Decision:
        caps = set(contract.get("constraints", {}).get("capabilities", []))
        session = {"device_id": event.device_id} if event.device_id else None
        name, ti = event.tool_name, event.tool_input or {}

        paths: list[str] = []
        actions: set[str] = set()
        if name in FILE_WRITE_TOOLS:
            paths = [str(ti[k]) for k in FILE_WRITE_TOOLS[name] if ti.get(k)][:1]
            if not paths:
                return self._deny(event, "invalid_args", f"{name} without a target path", mission_id)
            actions.add("write")
        elif name in PATCH_TOOLS:
            text = str(ti.get("input") or ti.get("patch") or _shell_command(ti))
            paths = [m.group(1) or m.group(2) for m in PATCH_FILE_RE.finditer(text)]
            if not paths:
                return self._deny(event, "invalid_args", f"{name}: no file targets found in patch", mission_id)
            actions.add("write")
        elif name in SHELL_TOOLS:
            cd_targets, actions = _shell_facts(_shell_command(ti))
            for target in cd_targets:
                if not self._under(_abs(target, event.cwd), root):
                    return self._deny(
                        event, "outside_mission", f"Command changes into {target}, outside the mission project. {redirect}", mission_id
                    )
        # Any other tool: the cwd check in evaluate() is the scope check.

        for raw in paths:
            allowed, msg = self.engine._path_allowed(contract, str(_abs(raw, event.cwd)), session=session)
            if not allowed:
                return self._deny(event, "outside_mission", f"{name} target {raw}: {msg}. {redirect}", mission_id)

        missing = sorted(a for a in actions if a not in caps)
        if missing:
            return self._deny(
                event,
                "capability_missing",
                f"Mission {mission_id} does not grant {', '.join(missing)} "
                f"(constraints.capabilities={sorted(caps)}). Recontract (owner gate) to add it.",
                mission_id,
            )
        return Decision(True, "in_mission", f"{name} inside the active mission scope", mission_id)


def event_from_hook_payload(payload: dict[str, Any], harness_id: str, device_id: str | None) -> GuardEvent:
    """Normalize a Claude Code / Codex style hook payload (they share the field names)."""
    hook = str(payload.get("hook_event_name") or payload.get("event") or "")
    cwd = str(payload.get("cwd") or os.getcwd())
    if hook == "UserPromptSubmit" or ("prompt" in payload and "tool_name" not in payload):
        return GuardEvent("prompt", harness_id, cwd, device_id, prompt=str(payload.get("prompt") or ""))
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {"input": tool_input} if tool_input is not None else {}
    return GuardEvent("tool", harness_id, cwd, device_id, tool_name=str(payload.get("tool_name") or ""), tool_input=tool_input)
