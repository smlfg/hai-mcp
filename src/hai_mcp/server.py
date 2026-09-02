from __future__ import annotations

import argparse
import json
import os
from typing import Any

import anyio
from mcp.server.fastmcp import FastMCP

from hai_mcp.boundary import strict_optional_time_limit_hours
from hai_mcp.config import Config, SERVER_NAME
from hai_mcp.http_transport import http_bind_allowed, http_token_from_env, wrap_with_bearer_token
from hai_mcp.state import ControlPlane

mcp = FastMCP(SERVER_NAME)

_cp: ControlPlane | None = None


def get_control_plane() -> ControlPlane:
    global _cp
    if _cp is None:
        _cp = ControlPlane(Config.from_env())
    return _cp


def _json(result: dict[str, Any]) -> str:
    return json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True)


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
def hai_read_artifacts(project_path: str, max_chars: Any = 4000) -> str:
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
    """Set focus and register an ACTIVE lane (max 2). Fails if a third ACTIVE is requested."""
    return _json(get_control_plane().set_focus(focus_id, project_path=project_path, label=label))


@mcp.tool()
def hai_propose_next_step(project_path: str, content: str) -> str:
    """Write NEXT_STEP.proposed.md (not canonical). Promote via hai_accept_next_step."""
    return _json(get_control_plane().propose_next_step(project_path, content))


@mcp.tool()
def hai_accept_next_step(
    project_path: str,
    owner_ack: Any,
    reason: str,
    content: str | None = None,
) -> str:
    """Promote proposed (or provided) content to canonical NEXT_STEP.md. Requires owner_ack=true + reason."""
    return _json(
        get_control_plane().accept_next_step(
            project_path,
            owner_ack=owner_ack,
            reason=reason,
            content=content,
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
) -> str:
    """Open a bounded mission with a versioned canonical contract. One active mission globally."""
    return _json(
        get_control_plane().open_mission(
            objective=objective,
            artifact=artifact,
            done_criteria=done_criteria,
            non_goals=non_goals,
            constraints=constraints,
            owner=owner,
        )
    )


@mcp.tool()
def hai_authorize_session(
    mission_id: str,
    contract_version: Any,
    agent_identity: str,
    role: str,
    contribution: str,
    expected_result: str,
    duration_minutes: Any,
    criterion_ids: list[str],
    capabilities: list[str] | None = None,
    device_id: str | None = None,
    harness_id: str | None = None,
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
        )
    )


@mcp.tool()
def hai_bind_project(
    project_id: str,
    device_id: str,
    local_path: str,
    owner_ack: Any,
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
    declares_blocker: Any = False,
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
    contract_version: Any,
    reason: str,
    changes: dict[str, Any],
    owner_ack: Any = False,
    mode: str = "normal",
    break_glass_marker: Any = False,
) -> str:
    """Apply a visible field-level contract diff. Requires owner_ack=true; revokes all leases."""
    return _json(
        get_control_plane().recontract(
            mission_id=mission_id,
            contract_version=contract_version,
            reason=reason,
            changes=changes,
            mode=mode,
            owner_ack=owner_ack,
            break_glass_marker=break_glass_marker,
        )
    )


@mcp.tool()
def hai_close_mission(
    mission_id: str,
    contract_version: Any,
    closure: str,
    outcome_summary: str,
    evidence: dict[str, Any] | None = None,
    owner_ack: Any = False,
    device_id: str | None = None,
) -> str:
    """Complete with verified per-criterion evidence, or abandon with owner_ack and reason."""
    return _json(
        get_control_plane().close_mission(
            mission_id=mission_id,
            contract_version=contract_version,
            evidence=evidence,
            outcome_summary=outcome_summary,
            closure=closure,
            owner_ack=owner_ack,
            device_id=device_id,
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
    time_limit_hours: Any = None,
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
    declares_blocker: Any = False,
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
    contract_version: Any,
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
) -> str:
    """Hard day terminal: record the three answers, invalidate active leases; no next-day plan. Missions are not closed."""
    return _json(
        get_control_plane().stop_day(
            day=day,
            loop_closed=loop_closed,
            clearer=clearer,
            agency_gained=agency_gained,
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
