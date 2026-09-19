from __future__ import annotations

import argparse
import json
import os
from typing import Annotated, Any

import anyio
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

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

mcp = FastMCP(SERVER_NAME)

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


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )
)
def hai_health(
    project_path: Annotated[
        str | None, Field(description="Optional project directory to check; it must resolve to an existing path.")
    ] = None,
) -> str:
    """Check HAI-MCP server health and optional project path usability without model calls.

    Use this when you need version, hai_home writability, owner-gate mode, or configuration
    warnings; use hai_status instead to inspect ACTIVE lanes, focus, inbox count, and
    next-step flags. Read-only. Returns JSON with ok and health fields.
    """
    return _json(get_control_plane().health(project_path))


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )
)
def hai_status(
    project_path: Annotated[
        str | None, Field(description="Optional project directory whose next-step flags should be reported.")
    ] = None,
) -> str:
    """Return ACTIVE lanes, focus, inbox count, and optional project next-step flags.

    Use this when you need current control-plane state; use hai_health instead to diagnose
    the server or project path itself. Read-only. Returns JSON with ok and the status fields.
    """
    return _json(get_control_plane().status(project_path))


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )
)
def hai_get_next_step(
    project_path: Annotated[
        str, Field(description="Project directory whose canonical NEXT_STEP.md should be read.")
    ],
) -> str:
    """Read the canonical NEXT_STEP.md for a project, or report that none exists.

    Use this when you want to execute the already promoted step; use hai_propose_next_step
    instead to draft a new step without promoting it, and use hai_accept_next_step to
    promote a proposal under the owner gate. Read-only. Returns JSON with ok, exists, path,
    content, and source.
    """
    return _json(get_control_plane().get_next_step(project_path))


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )
)
def hai_read_artifacts(
    project_path: Annotated[
        str, Field(description="Project directory containing the HAI run-contract artifacts.")
    ],
    max_chars: Annotated[
        StrictInt, Field(description="Maximum characters kept per artifact (clamped to 500..20000).")
    ] = 4000,
) -> str:
    """Read-only summary of HAI Projek-Managment run-contract artifacts.

    Use this when you need the current artifact contents; use hai_get_next_step instead for
    only the canonical next step. Character counts are capped per artifact. Returns JSON with
    ok, artifacts, and present.
    """
    return _json(get_control_plane().read_artifacts(project_path, max_chars=max_chars))


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
    )
)
def hai_park(
    text: Annotated[str, Field(description="Thought or meta item to park in the inbox.")],
    tags: Annotated[
        list[str] | None, Field(description="Optional tags stored with the parked item.")
    ] = None,
) -> str:
    """Park a thought or meta item in the HAI inbox without changing ACTIVE lanes.

    Use this when a quick, unscoped thought must leave active work untouched; use
    hai_park_item instead for a mission-linked idea with origin and rationale. Additive
    write; no model calls. Returns JSON with ok, path, and active_unchanged.
    """
    return _json(get_control_plane().park(text, tags=tags))


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )
)
def hai_set_focus(
    focus_id: Annotated[str, Field(description="Stable identifier of the lane to focus.")],
    project_path: Annotated[
        str | None, Field(description="Optional project directory associated with the focus lane.")
    ] = None,
    label: Annotated[
        str | None, Field(description="Optional human-readable label for the lane.")
    ] = None,
) -> str:
    """Set focus and register an ACTIVE lane (maximum two).

    Use this when choosing what to work on; use hai_park instead for thoughts that must not
    become lanes. Repeating the same focus is idempotent. Returns JSON with ok, focus_id,
    active, and active_count, or max_active_lanes.
    """
    return _json(get_control_plane().set_focus(focus_id, project_path=project_path, label=label))


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )
)
def hai_propose_next_step(
    project_path: Annotated[
        str, Field(description="Project directory that receives NEXT_STEP.proposed.md.")
    ],
    content: Annotated[
        str, Field(description="Proposed next-step text; canonical only after owner-gated acceptance.")
    ],
) -> str:
    """Write NEXT_STEP.proposed.md without making it canonical.

    Use this when drafting a step; use hai_get_next_step instead to read the canonical step,
    and use hai_accept_next_step to promote the proposal under the owner gate. Does not change
    NEXT_STEP.md. Returns JSON with ok, path, and canonical=false.
    """
    return _json(get_control_plane().propose_next_step(project_path, content))


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
    )
)
def hai_accept_next_step(
    project_path: Annotated[
        str, Field(description="Project directory whose canonical NEXT_STEP.md should be updated.")
    ],
    owner_code: Annotated[
        str | None,
        Field(description="One-time owner code delivered through the owner channel; omit on the first call."),
    ] = None,
    reason: Annotated[
        str | None,
        Field(description="Owner reason; required for the legacy ack path and recorded on promotion."),
    ] = None,
    content: Annotated[
        str | None,
        Field(description="Optional next-step text; when omitted the existing proposal is promoted."),
    ] = None,
    owner_ack: Annotated[
        StrictBool,
        Field(description="Legacy gate only: literal true asserts owner approval (requires reason)."),
    ] = False,
    approval_context: Annotated[
        dict[str, str] | None,
        Field(description="Display labels for the owner prompt: agent, session (readable chat title), topic. Not authenticated identity; repeat them unchanged on the follow-up call."),
    ] = None,
    ctx: Context | None = None,
) -> str:
    """Promote proposed or provided content to canonical NEXT_STEP.md under the owner gate.

    Use this when a proposal should become canonical; use hai_propose_next_step instead to
    draft it, or hai_get_next_step to read it. The nonce gate needs a first call without
    owner_code, then a second call with the owner code; legacy mode needs owner_ack=true plus
    reason. Bound to the exact content and single-use; returns JSON with ok or error.
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


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
    )
)
def hai_checkpoint(
    note: Annotated[
        str | None, Field(description="Optional note stored in the checkpoint metadata.")
    ] = None,
    project_path: Annotated[
        str | None,
        Field(description="Optional project directory whose key artifacts should be snapshotted."),
    ] = None,
) -> str:
    """Snapshot ACTIVE_CONTEXT and optional project artifacts under HAI_HOME/history/checkpoints.

    Use this when creating a recovery point before risky work; use hai_recover instead to read
    the latest or named checkpoint. Each call creates a new checkpoint id. Returns JSON with
    ok, checkpoint_id, path, and meta.
    """
    return _json(get_control_plane().checkpoint(note=note, project_path=project_path))


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )
)
def hai_recover(
    checkpoint_id: Annotated[
        str | None, Field(description="Optional checkpoint id; defaults to the latest checkpoint.")
    ] = None,
) -> str:
    """Return the smallest recovery next action from the latest or named checkpoint.

    Use this when resuming after an interruption; use hai_checkpoint instead to create a
    snapshot. Read-only advice only. Returns JSON with ok, mode, checkpoint_id,
    snapshot_active, and next_action.
    """
    return _json(get_control_plane().recover(checkpoint_id=checkpoint_id))


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
    )
)
def hai_open_mission(
    objective: Annotated[str, Field(description="Bounded mission objective.")],
    artifact: Annotated[
        str, Field(description="Expected artifact or outcome that defines completion.")
    ],
    done_criteria: Annotated[
        list[dict[str, Any]],
        Field(description="List of criteria objects, each with an id and a description."),
    ],
    owner: Annotated[str, Field(description="Owner principal accountable for the mission.")],
    project_path: Annotated[
        str, Field(description="Existing project directory the mission is bound to (required).")
    ],
    non_goals: Annotated[
        list[str] | None, Field(description="Optional list of explicit non-goals.")
    ] = None,
    constraints: Annotated[
        dict[str, Any] | None,
        Field(description="Optional constraint map, e.g. allowed_paths, capabilities, max_parallel_sessions."),
    ] = None,
    supersede_orphan: Annotated[
        StrictBool,
        Field(description="Literal true allows abandoning an orphaned active mission under the owner gate."),
    ] = False,
    supersede_reason: Annotated[
        str, Field(description="Reason recorded when an orphaned mission is superseded.")
    ] = "",
    owner_ack: Annotated[
        StrictBool,
        Field(description="Legacy gate only: literal true asserts owner approval."),
    ] = False,
    owner_code: Annotated[
        str | None,
        Field(description="One-time owner code for superseding; omit on the first call."),
    ] = None,
) -> str:
    """Open a bounded mission with a versioned canonical contract; one active mission globally.

    Use this when you need the full contract surface; use hai_mission_start for the thin
    fast-start wrapper, or hai_proof / hai_close_mission to finish the mission. An orphaned
    active mission can be superseded with supersede_orphan=true and a reason under the owner
    gate. Purely additive when no mission is active; returns JSON with ok or error.
    """
    merged = dict(constraints or {})
    merged["project_path"] = project_path
    return _json(
        get_control_plane().open_mission(
            objective=objective,
            artifact=artifact,
            done_criteria=done_criteria,
            non_goals=non_goals,
            constraints=merged,
            owner=owner,
            supersede_orphan=supersede_orphan,
            supersede_reason=supersede_reason,
            owner_ack=owner_ack,
            owner_code=owner_code,
        )
    )


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
    )
)
def hai_authorize_session(
    mission_id: Annotated[str, Field(description="Active mission to bind the lease to.")],
    contract_version: Annotated[
        StrictInt, Field(description="Exact contract version integer the lease is bound to.")
    ],
    agent_identity: Annotated[str, Field(description="Identity of the agent requesting the lease.")],
    role: Annotated[str, Field(description="Role of the agent for this session.")],
    contribution: Annotated[str, Field(description="Intended contribution of the session.")],
    expected_result: Annotated[str, Field(description="Expected result of the session.")],
    duration_minutes: Annotated[
        StrictInt, Field(description="Lease duration in minutes (positive integer).")
    ],
    criterion_ids: Annotated[
        list[str], Field(description="Contract criterion ids this session may work on.")
    ],
    capabilities: Annotated[
        list[str] | None, Field(description="Optional capabilities requested for the session.")
    ] = None,
    device_id: Annotated[
        str | None,
        Field(description="Optional device id; required when the contract has a project_id."),
    ] = None,
    harness_id: Annotated[str | None, Field(description="Optional harness identifier.")] = None,
) -> str:
    """Grant a time-bounded session lease tied to the mission id and exact contract version.

    Use this when an agent needs permission to work under a contract; use hai_get_contract
    instead to read that contract. The mission must be active and the contract version must
    match. Returns JSON with ok, session_id, and lease fields, or error.
    """
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


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )
)
def hai_bind_project(
    project_id: Annotated[str, Field(description="Logical project identifier.")],
    device_id: Annotated[str, Field(description="Device whose mount should be bound.")],
    local_path: Annotated[str, Field(description="Device-local directory to bind.")],
    owner_ack: Annotated[
        StrictBool, Field(description="Must be literal true; confirms owner approval.")
    ],
    reason: Annotated[str, Field(description="Reason for the owner-acknowledged mount change.")],
) -> str:
    """Bind a device-local directory to a logical project_id mount table entry.

    Use this when mapping a device path to a project; use hai_health instead to check path
    usability only. Requires owner_ack=true plus reason. Returns JSON with ok or error.
    """
    return _json(
        get_control_plane().bind_project(
            project_id=project_id,
            device_id=device_id,
            local_path=local_path,
            owner_ack=owner_ack,
            reason=reason,
        )
    )


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )
)
def hai_get_contract(
    session_id: Annotated[str, Field(description="Session lease id returned by hai_authorize_session.")],
) -> str:
    """Return the exact canonical mission contract for a valid session lease.

    Use this when you need the full contract rather than a summary; use hai_recontract instead
    to change it under the owner gate, or hai_check_activity to check activity. Read-only.
    Returns JSON with ok and the contract.
    """
    return _json(get_control_plane().get_contract(session_id))


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
    )
)
def hai_check_activity(
    session_id: Annotated[str, Field(description="Valid session lease id.")],
    activity_step: Annotated[
        str, Field(description="Planned or observed activity step to classify.")
    ],
    criterion_id: Annotated[
        str | None,
        Field(description="Optional contract criterion id the activity maps to."),
    ] = None,
    affected_paths: Annotated[
        list[str] | None, Field(description="Optional paths the activity touches.")
    ] = None,
    trace_events: Annotated[
        list[dict[str, Any]] | None, Field(description="Optional structured trace events.")
    ] = None,
    activity_kind: Annotated[
        str | None, Field(description="Optional activity kind used for classification.")
    ] = None,
    evidence: Annotated[
        dict[str, Any] | None, Field(description="Optional evidence map supporting the activity.")
    ] = None,
    declares_blocker: Annotated[
        StrictBool, Field(description="Literal true declares that the activity is blocked.")
    ] = False,
) -> str:
    """Deterministically classify planned or observed activity against the mission contract.

    Use this when you need full audit classification; use hai_drift_check instead for the
    lightweight wrapper over the same engine. Requires a valid session lease and writes audit
    only. Returns JSON with ok and the classification.
    """
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


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
    )
)
def hai_park_item(
    idea: Annotated[str, Field(description="Mission-linked idea to park.")],
    origin_session_id: Annotated[
        str, Field(description="Session lease id where the idea originated.")
    ],
    trigger_event: Annotated[str, Field(description="Event that triggered parking the idea.")],
    mission_id: Annotated[str, Field(description="Mission the idea is linked to.")],
    rationale: Annotated[
        str, Field(description="Why the idea is parked; required and grants no execution right.")
    ],
) -> str:
    """Park a mission-linked idea with full context; grants no execution right.

    Use this when an idea belongs to a mission and needs origin, trigger, and rationale; use
    hai_park instead for a quick inbox thought. The contract is unchanged. Returns JSON with
    ok and the parking record.
    """
    return _json(
        get_control_plane().park_item(
            idea=idea,
            origin_session_id=origin_session_id,
            trigger_event=trigger_event,
            mission_id=mission_id,
            rationale=rationale,
        )
    )


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
    )
)
def hai_recontract(
    mission_id: Annotated[str, Field(description="Active mission whose contract should change.")],
    contract_version: Annotated[
        StrictInt, Field(description="Current contract version the diff applies to.")
    ],
    reason: Annotated[str, Field(description="Owner-visible reason for the contract change.")],
    changes: Annotated[dict[str, Any], Field(description="Field-level contract changes to apply.")],
    owner_ack: Annotated[
        StrictBool, Field(description="Legacy gate only: literal true asserts owner approval.")
    ] = False,
    mode: Annotated[
        str, Field(description="Recontract mode, normally normal; break_glass requires the marker.")
    ] = "normal",
    break_glass_marker: Annotated[
        StrictBool, Field(description="Literal true is required for break_glass mode friction.")
    ] = False,
    owner_code: Annotated[
        str | None,
        Field(description="One-time owner code bound to the diff; omit on the first call."),
    ] = None,
    approval_context: Annotated[
        dict[str, str] | None,
        Field(description="Display labels for the owner prompt: agent, session (readable chat title), topic. Not authenticated identity; repeat them unchanged on the follow-up call."),
    ] = None,
    ctx: Context | None = None,
) -> str:
    """Apply a visible field-level contract diff and revoke all leases, under the owner gate.

    Use this when the contract itself must change; use hai_get_contract instead to read it, or
    hai_close_mission to end the mission instead. The nonce gate returns the diff on the first
    call, then takes an owner_code; legacy mode uses owner_ack=true. Destructive because it
    replaces contract state and revokes leases; returns JSON with ok or error.
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


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
    )
)
def hai_close_mission(
    mission_id: Annotated[str, Field(description="Mission to close.")],
    contract_version: Annotated[
        StrictInt, Field(description="Current contract version being closed.")
    ],
    closure: Annotated[str, Field(description="Closure kind: completed or abandoned.")],
    outcome_summary: Annotated[str, Field(description="Human-readable outcome summary.")],
    evidence: Annotated[
        dict[str, Any] | None,
        Field(description="Per-criterion evidence map; required for completed closure."),
    ] = None,
    owner_ack: Annotated[
        StrictBool,
        Field(description="Legacy gate only: literal true asserts owner approval for abandonment."),
    ] = False,
    device_id: Annotated[
        str | None,
        Field(description="Optional device id; required when the contract has a project_id."),
    ] = None,
    owner_code: Annotated[
        str | None,
        Field(description="One-time owner code for abandonment; omit on the first call."),
    ] = None,
    approval_context: Annotated[
        dict[str, str] | None,
        Field(description="Display labels for the owner prompt: agent, session (readable chat title), topic. Not authenticated identity; repeat them unchanged on the follow-up call."),
    ] = None,
    ctx: Context | None = None,
) -> str:
    """Complete a mission with verified evidence, or abandon it under the owner gate.

    Use this when ending a mission; use hai_proof instead for the completed-only wrapper, or
    hai_recontract to modify it. Completed needs verified evidence per criterion; abandoned
    needs the owner gate and reason. Destructive terminal state change with lease revocation;
    returns JSON with ok or error.
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


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
    )
)
def hai_intake(
    raw: Annotated[str, Field(description="Raw thought to capture immutably.")],
) -> str:
    """Capture a raw thought immutably and return an intake id only.

    Use this when capturing without deciding; use hai_distill instead to turn an intake into
    one decision plus one next step, or hai_park for a plain inbox item. Never actionable and
    never starts an agent. Returns JSON with ok and intake_id.
    """
    return _json(get_control_plane().intake(raw))


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
    )
)
def hai_distill(
    intake_id: Annotated[str, Field(description="Intake id returned by hai_intake.")],
    decision: Annotated[
        str, Field(description="Exactly one decision; no bundled multi-line plan.")
    ],
    next_step: Annotated[
        str, Field(description="Exactly one next step; no bundled multi-line plan.")
    ],
    parklist: Annotated[
        list[str] | None,
        Field(description="Optional items the server should park instead of acting on."),
    ] = None,
) -> str:
    """Reduce an intake to exactly one decision and one next step; the server parks the rest.

    Use this when processing an intake; use hai_intake instead to capture raw text, or
    hai_park to park a single thought. Enforces cardinality and rejects bundled multi-line
    plans. Returns JSON with ok, decision, next_step, and parked_count.
    """
    return _json(
        get_control_plane().distill(
            intake_id=intake_id,
            decision=decision,
            next_step=next_step,
            parklist=parklist,
        )
    )


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
    )
)
def hai_mission_start(
    problem: Annotated[str, Field(description="Problem statement used as the mission objective.")],
    artifact: Annotated[
        str, Field(description="Expected artifact or outcome that defines completion.")
    ],
    done_criteria: Annotated[
        list[dict[str, Any]],
        Field(description="List of criteria objects, each with an id and a description."),
    ],
    owner: Annotated[str, Field(description="Owner principal accountable for the mission.")],
    project_path: Annotated[
        str, Field(description="Existing project directory the mission is bound to (required).")
    ],
    time_limit_hours: Annotated[
        StrictNumber, Field(description="Optional finite time limit in hours (>= 1).")
    ] = None,
    non_goals: Annotated[
        list[str] | None, Field(description="Optional list of explicit non-goals.")
    ] = None,
    constraints: Annotated[
        dict[str, Any] | None,
        Field(description="Optional constraint map; time_limit_hours is merged into it."),
    ] = None,
) -> str:
    """Fast-start a mission from problem, artifact, and optional time limit.

    Use this when you want the thin wrapper; use hai_open_mission instead for the full contract
    surface with supersede and owner options. Has the same gate as hai_open_mission. Returns
    JSON with ok or error.
    """
    merged = dict(constraints or {})
    merged["project_path"] = project_path
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


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
    )
)
def hai_drift_check(
    session_id: Annotated[str, Field(description="Valid session lease id.")],
    activity_step: Annotated[
        str, Field(description="Planned or observed activity step to compare.")
    ],
    criterion_id: Annotated[
        str | None,
        Field(description="Optional contract criterion id the activity maps to."),
    ] = None,
    affected_paths: Annotated[
        list[str] | None, Field(description="Optional paths the activity touches.")
    ] = None,
    trace_events: Annotated[
        list[dict[str, Any]] | None, Field(description="Optional structured trace events.")
    ] = None,
    activity_kind: Annotated[
        str | None, Field(description="Optional activity kind used for comparison.")
    ] = None,
    evidence: Annotated[
        dict[str, Any] | None, Field(description="Optional evidence map supporting the activity.")
    ] = None,
    declares_blocker: Annotated[
        StrictBool, Field(description="Literal true declares that the activity is blocked.")
    ] = False,
) -> str:
    """Lightweight mismatch check against the mission contract.

    Use this when you want the thin wrapper; use hai_check_activity instead for the full audit
    surface. Requires a valid session lease and writes audit only. Returns JSON with ok and the
    classification.
    """
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


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
    )
)
def hai_proof(
    mission_id: Annotated[str, Field(description="Mission to close as completed.")],
    contract_version: Annotated[
        StrictInt, Field(description="Current contract version being closed.")
    ],
    evidence: Annotated[
        dict[str, Any], Field(description="Per-criterion verified evidence map; required.")
    ],
    outcome_summary: Annotated[str, Field(description="Human-readable outcome summary.")],
    device_id: Annotated[
        str | None,
        Field(description="Optional device id; required when the contract has a project_id."),
    ] = None,
) -> str:
    """Close a mission only against verified per-criterion evidence.

    Use this when the mission is completed with proof; use hai_close_mission instead to
    abandon or close with more options. Thin wrapper over hai_close_mission in completed mode;
    destructive terminal state change. Returns JSON with ok or error.
    """
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


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
    )
)
def hai_stop(
    day: Annotated[str, Field(description="Day identifier to seal, e.g. 2026-11-04.")],
    loop_closed: Annotated[
        bool, Field(description="Literal true when the day's loop is closed.")
    ],
    clearer: Annotated[str, Field(description="Answer describing what became clearer.")],
    agency_gained: Annotated[str, Field(description="Answer describing agency gained.")],
    manual_practice: Annotated[
        list[str] | None,
        Field(description="Optional list of things the owner did by hand today."),
    ] = None,
) -> str:
    """Hard day terminal: record the three answers, invalidate active leases, and plan no next day.

    Use this when ending the workday; use hai_close_mission or hai_proof instead to finish a
    mission, because stop does not close missions. Destructive because it invalidates leases;
    manual_practice records what the owner did by hand. Returns JSON with ok, status, stop_id,
    and revoked_sessions.
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
