# TOOL_CONTRACT — HAI-MCP v0.1

## Non-goals

- No LLM inside the server
- No commit/push/delete tools
- No harness execution
- No merge with `hai-intake` packages

## Gate matrix

| Tool | Mutates | Gate |
|---|---|---|
| `hai_health` | no | none |
| `hai_status` | no | none |
| `hai_get_next_step` | no | none |
| `hai_read_artifacts` | no | none |
| `hai_park` | inbox only | none |
| `hai_set_focus` | ACTIVE_CONTEXT | soft: max 2 ACTIVE |
| `hai_propose_next_step` | NEXT_STEP.proposed.md | none |
| `hai_accept_next_step` | NEXT_STEP.md | **owner gate**: one-time code from the owner channel, bound to the body (`ack_legacy`: owner_ack=true + reason) |
| `hai_checkpoint` | history/checkpoints | none |
| `hai_recover` | no (read + advice) | none |
| `hai_open_mission` | mission + contract v1 | valid finite contract; no second active mission |
| `hai_bind_project` | mount table | **owner_ack=true + reason** |
| `hai_authorize_session` | session lease | active mission; current contract version; capacity |
| `hai_get_contract` | no | valid non-expired session lease |
| `hai_check_activity` | audit only | valid session lease |
| `hai_park_item` | parking record | rationale required; no execution right |
| `hai_recontract` | new contract version + lease revocation | **owner gate**: one-time code bound to the diff (`ack_legacy`: owner_ack=true); reason required; break_glass needs marker |
| `hai_close_mission` | terminal mission state + lease revocation | completed: verified evidence per criterion; abandoned: **owner gate** (one-time code; `ack_legacy`: owner_ack=true) + reason |
| `hai_intake` | intake record only | none |
| `hai_distill` | decision + next step (parks the rest) | none |
| `hai_mission_start` | mission + contract v1 | thin wrapper over `hai_open_mission`; same gate |
| `hai_drift_check` | audit only | thin wrapper over `hai_check_activity`; valid session lease |
| `hai_proof` | terminal mission state (completed) | thin wrapper over `hai_close_mission`; verified evidence per criterion |
| `hai_stop` | day-stop record + lease invalidation | none (does not close missions) |

## Owner gate

The owner is a separate principal. Default mode `nonce`: the server delivers a one-time code through an owner channel the client cannot read; the client can pass the gate only with a code a human relayed. `ack_legacy` keeps the old self-asserted `owner_ack`. Full contract: `docs/OWNER_GATE.md`.

## Paths

- `HAI_HOME` (env, default `~/.hai`)
- `HAI_OWNER_HOME` (env, default `~/.hai-owner`) — owner channel `file`; never inside `HAI_HOME`
- Logical projects: `HAI_HOME/core/projects.json` (`project_id` + per-device `mounts`)
- Project artifacts: `<project_path>/Projek-Managment/` (legacy) or device mount + relative prefixes when `project_id` is set
- All writes resolved and confined

## Artifact names (Run Contract)

- `PROJECT_STATE.md`
- `PROMPT.md` / `INTENT.md`
- `CODE_STATE.md`
- `BRIEFING.md`
- `NEXT_STEP.md` (canonical)
- `NEXT_STEP.proposed.md` (proposal only)
- Optional: `EXECUTION_REPORT_NEXT_STEP.md`, `VALIDATION_REPORT_NEXT_STEP.md`, …

## Errors

Structured JSON in tool result with `ok: false` and `error` code:

- `path_outside_root`
- `missing_project`
- `max_active_lanes`
- `owner_gate_required`
- `no_proposal`
- `invalid_args`
- `active_mission_exists`
- `mission_not_active`
- `contract_version_mismatch`
- `lease_expired`
- `lease_revoked`
- `review_required`
- `parallel_session_denied`
- `device_mount_required`
- `owner_channel_unavailable` (owner code could not be delivered; gate stays closed)

`owner_gate_required` carries `detail`: `malformed_owner_code`, `invalid_owner_code`, `no_pending_challenge`; or `status: pending_owner_code` with the `challenge_id`. See `docs/OWNER_GATE.md`.

## Transport note

Optional `streamable-http` transport is trusted-localhost or bearer-token gated (`HAI_HTTP_TOKEN`); it is not adversarial owner authentication.

## Session / close device fields

- `hai_authorize_session`: optional `device_id`, `harness_id` (required `device_id` when contract has `project_id`)
- `hai_close_mission` / `hai_proof`: optional `device_id` (required when contract has `project_id`)
