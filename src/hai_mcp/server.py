from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Annotated, Any

import anyio
from mcp.server.fastmcp import Context, FastMCP
from pydantic import Field

from hai_mcp.blocker_reporting import publish as publish_blocker
from hai_mcp.boundary import strict_optional_time_limit_hours
from hai_mcp.config import Config, SERVER_NAME
from hai_mcp.http_transport import http_bind_allowed, http_token_from_env, wrap_with_bearer_token
from hai_mcp.state import ControlPlane
from hai_mcp.owner_gate import normalize_approval_context

# The boundary deliberately refuses coercion (see boundary.py): a string "2" must be
# rejected, never silently become 2. Bare `Any` however advertises a schema with no type
# at all, so a compliant client is free to serialize an integer as a string — and then the
# fail-closed check makes the tool permanently unreachable instead of merely strict.
# These aliases keep the runtime value un-coerced while telling the client what to send.
StrictInt = Annotated[Any, Field(json_schema_extra={"type": "integer"})]
StrictBool = Annotated[Any, Field(json_schema_extra={"type": "boolean"})]
StrictNumber = Annotated[Any, Field(json_schema_extra={"type": "number"})]

BLOCKER_INSTRUCTIONS = (
    "Wenn ein eigener HAI-MCP-Fehler die Arbeit blockiert, melde ihn selbstständig mit "
    "hai_report_blocker an smlfg/hai-mcp; keine erneute Einzelfrage. Beschreibe Problem, "
    "Soll/Ist, blockierte Aktion, Belege und warum HAI verantwortlich ist. Kennzeichne "
    "die Ursache ehrlich als unbekannt, vermutet oder belegt. Normale Gate-Ablehnungen, "
    "Owner-Abbruch/Timeout und Lease-Ablauf sind keine Bugs. Geschützte Arbeit bleibt gestoppt. "
    "Bei Transport closed nutze denselben Publisher ohne MCP über die konfigurierte Python-CLI: "
    + repr([sys.executable, "-m", "hai_mcp.blocker_reporting", "--report-file", "<structured-report.json>"])
    + ". Verwende nur minimale nicht vertrauliche Belege, keine Rohlogs, Umgebungsvariablen "
    "oder Projektinhalte. Publisher-Ausfälle niemals rekursiv melden: pending lokal sichern, "
    "Status ehrlich nennen und mit der ausgegebenen retry_argv erneut abgleichen. Erst bestätigte "
    "GitHub-URL und Readback zählen als gemeldet; Reporting umgeht keine Owner-/Lease-Gates."
)
mcp = FastMCP(SERVER_NAME, instructions=BLOCKER_INSTRUCTIONS)

_cp: ControlPlane | None = None


def get_control_plane() -> ControlPlane:
    global _cp
    if _cp is None:
        _cp = ControlPlane(Config.from_env())
    return _cp


def _json(result: dict[str, Any]) -> str:
    return json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True)


def _approval_context(value: dict[str, str] | None, ctx: Context | None) -> dict[str, str]:
    """Prefer explicit labels; protocol clientInfo can only supply the agent label."""
    labels = normalize_approval_context(value)
    if "agent" not in labels and ctx is not None:
        try:
            params = ctx.session.client_params
            info = params.clientInfo if params else None
            agent = (getattr(info, "title", None) or info.name) if info else None
            labels.update(normalize_approval_context({"agent": agent}))
        except (ValueError, AttributeError):
            pass  # Direct Python calls have no active MCP request context.
    return labels


@mcp.tool()
def hai_report_blocker(category: str, problem: str, observed: str, expected: str,
                       blocked_action: str, hai_responsibility: str, evidence: list[str],
                       cause_confidence: str = "unknown", cause: str | None = None,
                       cause_evidence: list[str] | None = None, agent: str | None = None,
                       session: str | None = None, error_code: str | None = None,
                       unexpected_gate: StrictBool = False, unexpected_gate_reason: str | None = None,
                       unexpected_gate_evidence: list[str] | None = None) -> str:
    """Autonomously report a genuine blocking HAI defect to the fixed smlfg/hai-mcp repo.

    Expected gates are skipped. Submit minimal non-confidential evidence; never raw logs,
    environment or project content. No lease or ControlPlane is required. Pending is not
    success; retry the returned CLI command. Do not recursively report publisher failures.
    """
    report = {"category": category, "problem": problem, "observed": observed, "expected": expected,
              "blocked_action": blocked_action, "hai_responsibility": hai_responsibility,
              "evidence": evidence, "cause_confidence": cause_confidence, "unexpected_gate": unexpected_gate}
    for key, value in {"cause": cause, "cause_evidence": cause_evidence, "agent": agent,
                       "session": session, "error_code": error_code, "unexpected_gate_reason": unexpected_gate_reason,
                       "unexpected_gate_evidence": unexpected_gate_evidence}.items():
        if value is not None:
            report[key] = value
    return _json(publish_blocker(report))


@mcp.tool()
def hai_health(project_path: str | None = None) -> str:
    """Check HAI-MCP server health and optional project path usability. No model calls."""
    return _json(get_control_plane().health(project_path))


@mcp.tool()
def hai_status(project_path: str | None = None) -> str:
    """Return ACTIVE lanes, focus, inbox count, and optional project next-step flags."""
    return _json(get_control_plane().status(project_path))


@mcp.tool()
def hai_get_next_step(project_path: str) -> str:
    """Read the canonical NEXT_STEP.md for a project (or report none)."""
    return _json(get_control_plane().get_next_step(project_path))


@mcp.tool()
def hai_read_artifacts(project_path: str, max_chars: StrictInt = 4000) -> str:
    """Read-only summary of HAI Projek-Managment run-contract artifacts."""
    return _json(get_control_plane().read_artifacts(project_path, max_chars=max_chars))


@mcp.tool()
def hai_park(text: str, tags: list[str] | None = None) -> str:
    """Park a thought/meta item in HAI inbox without changing ACTIVE lanes."""
    return _json(get_control_plane().park(text, tags=tags))


@mcp.tool()
def hai_set_focus(
    focus_id: str,
    project_path: str | None = None,
    label: str | None = None,
) -> str:
    """Set focus and register an ACTIVE lane; default maximum two.

    Above two, an active bounded window and distinct registered Git worktrees are
    mandatory for every lane (maximum five). Linked roots require native owner binding.
    Root updates and focusing existing lanes validate the whole prospective group.
    Inspect hai_health.lane_isolation for conditional capacity and recovery details.
    """
    return _json(get_control_plane().set_focus(focus_id, project_path=project_path, label=label))


@mcp.tool()
def hai_propose_next_step(project_path: str, content: str) -> str:
    """Write NEXT_STEP.proposed.md (not canonical). Promote via hai_accept_next_step."""
    return _json(get_control_plane().propose_next_step(project_path, content))


@mcp.tool()
def hai_accept_next_step(
    project_path: str,
    owner_code: str | None = None,
    reason: str | None = None,
    content: str | None = None,
    owner_ack: StrictBool = False,
    approval_context: dict[str, str] | None = None,
    ctx: Context | None = None,
) -> str:
    """Promote proposed (or provided) content to canonical NEXT_STEP.md — owner-gated.

    Default gate (nonce): call once WITHOUT owner_code. file/ntfy delivers a one-time code
    to the owner (never to this client); call again with owner_code. touchid (macOS) shows
    a Touch ID prompt in the server process; when status is owner_approved, call again with
    the same arguments and no owner_code. Bound to the exact content, single-use.
    Legacy gate (ack_legacy): owner_ack=true + reason.
    Supply approval_context with agent, session (readable chat title), and topic.
    Labels are client-provided display context, not authenticated identity.
    Repeat the same labels on the follow-up call; changed labels require new approval.
    """
    return _json(
        get_control_plane().accept_next_step(
            project_path,
            owner_ack=owner_ack,
            reason=reason,
            content=content,
            owner_code=owner_code,
            approval_context=_approval_context(approval_context, ctx),
        )
    )


@mcp.tool()
def hai_checkpoint(note: str | None = None, project_path: str | None = None) -> str:
    """Snapshot ACTIVE_CONTEXT and optional project artifacts under HAI_HOME/history/checkpoints."""
    return _json(get_control_plane().checkpoint(note=note, project_path=project_path))


@mcp.tool()
def hai_recover(checkpoint_id: str | None = None) -> str:
    """Return the smallest recovery next action from latest or named checkpoint (read-only advice)."""
    return _json(get_control_plane().recover(checkpoint_id=checkpoint_id))


@mcp.tool()
def hai_open_mission(
    objective: str,
    artifact: str,
    done_criteria: list[dict[str, Any]],
    owner: str,
    non_goals: list[str] | None = None,
    constraints: dict[str, Any] | None = None,
    supersede_orphan: StrictBool = False,
    supersede_reason: str = "",
    owner_ack: StrictBool = False,
    owner_code: str | None = None,
    approval_context: dict[str, str] | None = None,
    ctx: Context | None = None,
) -> str:
    """Open a bounded mission with a versioned canonical contract.

    Orphaned blocker: if active_mission_exists comes back with orphaned=true (no valid session
    lease left), retry with supersede_orphan=true + supersede_reason. That abandons the orphan
    under the owner gate exactly like hai_close_mission(closure="abandoned") (touchid: on
    owner_approved call again with the same arguments), then opens this mission. A mission
    with a live lease is never superseded.

    Default: one active mission. An owner-enabled exact-project capacity window permits
    at most five missions with disjoint canonical contract paths and five writers globally.
    Above two, every mission must use a different registered Git worktree.
    Admitting another mission preserves the existing primary mission, pointer and leases.
    Inspect hai_health.mission_capacity before admission and supply the canonical
    constraints.project_path matching the policy project or an exact owner-approved
    hai_bind_worktree binding; an unapproved worktree does not qualify for the exception.
    """
    return _json(
        get_control_plane().open_mission(
            objective=objective,
            artifact=artifact,
            done_criteria=done_criteria,
            non_goals=non_goals,
            constraints=constraints,
            owner=owner,
            supersede_orphan=supersede_orphan,
            supersede_reason=supersede_reason,
            owner_ack=owner_ack,
            owner_code=owner_code,
            approval_context=_approval_context(approval_context, ctx),
        )
    )


@mcp.tool()
def hai_authorize_session(
    mission_id: str,
    contract_version: StrictInt,
    agent_identity: str,
    role: str,
    contribution: str,
    expected_result: str,
    duration_minutes: StrictInt,
    criterion_ids: list[str],
    capabilities: list[str] | None = None,
    device_id: str | None = None,
    harness_id: str | None = None,
    write_paths: list[str] | None = None,
) -> str:
    """Grant a time-bounded session lease tied to mission ID and exact contract version."""
    return _json(
        get_control_plane().authorize_session(
            mission_id=mission_id,
            contract_version=contract_version,
            agent_identity=agent_identity,
            role=role,
            contribution=contribution,
            expected_result=expected_result,
            duration_minutes=duration_minutes,
            capabilities=capabilities,
            criterion_ids=criterion_ids,
            device_id=device_id,
            harness_id=harness_id,
            write_paths=write_paths,
        )
    )


@mcp.tool()
def hai_bind_worktree(project_path: str, worktree_path: str, expires_at: str, reason: str,
                      approval_context: dict[str, str] | None = None, ctx: Context | None = None) -> str:
    """Approve one exact registered linked worktree via native Touch ID, then repeat identically.

    Requires an active exact-project capacity window. Explicit UTC expiry must be within
    one hour and no later than that window. No owner_ack/code or file fallback accepted.
    Existing canonical-project missions and leases remain unchanged.
    """
    return _json(get_control_plane().bind_worktree(project_path, worktree_path, expires_at, reason,
                                                  _approval_context(approval_context, ctx)))


@mcp.tool()
def hai_bind_project(
    project_id: str,
    device_id: str,
    local_path: str,
    owner_ack: StrictBool,
    reason: str,
) -> str:
    """Bind a device-local directory to a logical project_id mount table entry. Requires owner_ack=true + reason."""
    return _json(
        get_control_plane().bind_project(
            project_id=project_id,
            device_id=device_id,
            local_path=local_path,
            owner_ack=owner_ack,
            reason=reason,
        )
    )


@mcp.tool()
def hai_get_contract(session_id: str) -> str:
    """Return the exact canonical mission contract for a valid session lease (not a summary)."""
    return _json(get_control_plane().get_contract(session_id))


@mcp.tool()
def hai_check_activity(
    session_id: str,
    activity_step: str,
    criterion_id: str | None = None,
    affected_paths: list[str] | None = None,
    trace_events: list[dict[str, Any]] | None = None,
    activity_kind: str | None = None,
    evidence: dict[str, Any] | None = None,
    declares_blocker: StrictBool = False,
) -> str:
    """Deterministically classify planned or observed activity against the mission contract."""
    return _json(
        get_control_plane().check_activity(
            session_id=session_id,
            activity_step=activity_step,
            affected_paths=affected_paths,
            trace_events=trace_events,
            criterion_id=criterion_id,
            activity_kind=activity_kind,
            evidence=evidence,
            declares_blocker=declares_blocker,
        )
    )


@mcp.tool()
def hai_park_item(
    idea: str,
    origin_session_id: str,
    trigger_event: str,
    mission_id: str,
    rationale: str,
) -> str:
    """Park a mission-linked idea with full context. Grants no execution right; contract unchanged."""
    return _json(
        get_control_plane().park_item(
            idea=idea,
            origin_session_id=origin_session_id,
            trigger_event=trigger_event,
            mission_id=mission_id,
            rationale=rationale,
        )
    )


@mcp.tool()
def hai_recontract(
    mission_id: str,
    contract_version: StrictInt,
    reason: str,
    changes: dict[str, Any],
    owner_ack: StrictBool = False,
    mode: str = "normal",
    break_glass_marker: StrictBool = False,
    owner_code: str | None = None,
    approval_context: dict[str, str] | None = None,
    ctx: Context | None = None,
) -> str:
    """Apply a visible field-level contract diff; revokes all leases. Owner-gated.

    First call (no owner_code) returns the diff. file/ntfy delivers a one-time code to the
    owner; call again with owner_code. touchid (macOS): when status is owner_approved, call
    again with the same arguments and no owner_code. Legacy gate: owner_ack=true.
    Supply approval_context with agent, session (readable chat title), and topic.
    Labels are client-provided display context, not authenticated identity.
    Repeat the same labels on the follow-up call; changed labels require new approval.
    """
    return _json(
        get_control_plane().recontract(
            mission_id=mission_id,
            contract_version=contract_version,
            reason=reason,
            changes=changes,
            mode=mode,
            owner_ack=owner_ack,
            break_glass_marker=break_glass_marker,
            owner_code=owner_code,
            approval_context=_approval_context(approval_context, ctx),
        )
    )


@mcp.tool()
def hai_close_mission(
    mission_id: str,
    contract_version: StrictInt,
    closure: str,
    outcome_summary: str,
    evidence: dict[str, Any] | None = None,
    owner_ack: StrictBool = False,
    device_id: str | None = None,
    owner_code: str | None = None,
    approval_context: dict[str, str] | None = None,
    ctx: Context | None = None,
) -> str:
    """Complete with verified per-criterion evidence, or abandon (owner-gated).

    Abandon: file/ntfy delivers a one-time code; call again with owner_code. touchid (macOS):
    when status is owner_approved, call again with the same arguments and no owner_code.
    Legacy gate: owner_ack=true.
    Supply approval_context with agent, session (readable chat title), and topic.
    Labels are client-provided display context, not authenticated identity.
    Repeat the same labels on the follow-up call; changed labels require new approval.
    """
    return _json(
        get_control_plane().close_mission(
            mission_id=mission_id,
            contract_version=contract_version,
            evidence=evidence,
            outcome_summary=outcome_summary,
            closure=closure,
            owner_ack=owner_ack,
            device_id=device_id,
            owner_code=owner_code,
            approval_context=_approval_context(approval_context, ctx),
        )
    )


# --- Additional seven flow tools (daily loop) over the canonical hardened engine ---


@mcp.tool()
def hai_intake(raw: str) -> str:
    """Capture a raw thought immutably. Returns an intake id only — never actionable, never starts an agent."""
    return _json(get_control_plane().intake(raw))


@mcp.tool()
def hai_distill(
    intake_id: str,
    decision: str,
    next_step: str,
    parklist: list[str] | None = None,
) -> str:
    """Reduce an intake to EXACTLY one decision + one next step; the server parks everything else."""
    return _json(
        get_control_plane().distill(
            intake_id=intake_id,
            decision=decision,
            next_step=next_step,
            parklist=parklist,
        )
    )


@mcp.tool()
def hai_mission_start(
    problem: str,
    artifact: str,
    done_criteria: list[dict[str, Any]],
    owner: str,
    time_limit_hours: StrictNumber = None,
    non_goals: list[str] | None = None,
    constraints: dict[str, Any] | None = None,
) -> str:
    """Fast start: declare problem + artifact + time limit. Thin wrapper over hai_open_mission (one canonical contract)."""
    merged = dict(constraints or {})
    if time_limit_hours is not None:
        tl, err = strict_optional_time_limit_hours(time_limit_hours)
        if err:
            return _json(err)
        merged["time_limit_hours"] = tl
    return _json(
        get_control_plane().open_mission(
            objective=problem,
            artifact=artifact,
            done_criteria=done_criteria,
            non_goals=non_goals,
            constraints=merged,
            owner=owner,
        )
    )


@mcp.tool()
def hai_drift_check(
    session_id: str,
    activity_step: str,
    criterion_id: str | None = None,
    affected_paths: list[str] | None = None,
    trace_events: list[dict[str, Any]] | None = None,
    activity_kind: str | None = None,
    evidence: dict[str, Any] | None = None,
    declares_blocker: StrictBool = False,
) -> str:
    """Lightweight mismatch check against the mission contract. Thin wrapper over hai_check_activity."""
    return _json(
        get_control_plane().check_activity(
            session_id=session_id,
            activity_step=activity_step,
            affected_paths=affected_paths,
            trace_events=trace_events,
            criterion_id=criterion_id,
            activity_kind=activity_kind,
            evidence=evidence,
            declares_blocker=declares_blocker,
        )
    )


@mcp.tool()
def hai_proof(
    mission_id: str,
    contract_version: StrictInt,
    evidence: dict[str, Any],
    outcome_summary: str,
    device_id: str | None = None,
) -> str:
    """Close a mission only against verified per-criterion evidence. Thin wrapper over hai_close_mission (completed)."""
    return _json(
        get_control_plane().close_mission(
            mission_id=mission_id,
            contract_version=contract_version,
            evidence=evidence,
            outcome_summary=outcome_summary,
            closure="completed",
            device_id=device_id,
        )
    )


@mcp.tool()
def hai_stop(
    day: str,
    loop_closed: bool,
    clearer: str,
    agency_gained: str,
    manual_practice: list[str] | None = None,
) -> str:
    """Hard day terminal: record the three answers, invalidate active leases; no next-day plan. Missions are not closed.

    manual_practice: what the owner did BY HAND today (e.g. "read the auth diff",
    "ran pytest", "wrote the date regex") — the currency logbook for verification skills.
    """
    return _json(
        get_control_plane().stop_day(
            day=day,
            loop_closed=loop_closed,
            clearer=clearer,
            agency_gained=agency_gained,
            manual_practice=manual_practice,
        )
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="HAI-MCP control-plane server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default=os.environ.get("HAI_TRANSPORT", "stdio"),
        help="MCP transport (default: stdio; env HAI_TRANSPORT)",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("HAI_HTTP_HOST", "127.0.0.1"),
        help="HTTP bind host for streamable-http (env HAI_HTTP_HOST)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("HAI_HTTP_PORT", "8765")),
        help="HTTP bind port for streamable-http (env HAI_HTTP_PORT)",
    )
    args = parser.parse_args(argv)

    if args.transport == "streamable-http":
        token = http_token_from_env()
        allowed, msg = http_bind_allowed(args.host, token)
        if not allowed:
            raise SystemExit(msg)
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        if token:

            async def _serve_with_token() -> None:
                import uvicorn

                app = wrap_with_bearer_token(mcp.streamable_http_app(), token)
                config = uvicorn.Config(
                    app,
                    host=args.host,
                    port=args.port,
                    log_level=mcp.settings.log_level.lower(),
                )
                server = uvicorn.Server(config)
                await server.serve()

            anyio.run(_serve_with_token)
        else:
            mcp.run(transport="streamable-http")
        return

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
