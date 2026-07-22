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
| `hai_accept_next_step` | NEXT_STEP.md | **owner_ack=true + reason** |
| `hai_checkpoint` | history/checkpoints | none |
| `hai_recover` | no (read + advice) | none |
| `hai_open_mission` | mission + contract v1 | valid finite contract; no second active mission |
| `hai_authorize_session` | session lease | active mission; current contract version; capacity |
| `hai_get_contract` | no | valid non-expired session lease |
| `hai_check_activity` | audit only | valid session lease |
| `hai_park_item` | parking record | rationale required; no execution right |
| `hai_recontract` | new contract version + lease revocation | **owner_ack=true + reason**; break_glass needs marker |
| `hai_close_mission` | terminal mission state + lease revocation | completed: verified evidence per criterion; abandoned: **owner_ack=true + reason** |

## Paths

- `HAI_HOME` (env, default `~/.hai`)
- Project artifacts: `<project_path>/Projek-Managment/`
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
