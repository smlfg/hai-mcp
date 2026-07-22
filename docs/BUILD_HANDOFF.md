# HAI-MCP Build Handoff

**Updated:** 2026-07-22  
**Owner:** Samuel Fleig  
**Canonical purpose:** This is the single takeover document for the active HAI-MCP build. It records verified state, unresolved findings, and the remaining implementation/evaluation plan. It does not replace `GOAL.md`, `docs/TOOL_CONTRACT.md`, `7coreFunctions`, or `7Functions` as requirement sources.

## Non-negotiable boundaries

- Work only in `/home/smlflg/Projekte/MBP_Mirgraatin/HAI-MCP`.
- Read `AGENTS.md`, `GOAL.md`, `docs/TOOL_CONTRACT.md`, `7coreFunctions`, and `7Functions` before editing.
- One WIP slice at a time.
- No LLM calls inside the MCP server; models remain client-side.
- Owner gates remain fail-closed.
- Do not mutate live `~/.hermes`, live `~/.hai`, unrelated HAI apps, or the parent repository.
- The MiniMax credential may be located read-only under `~/.hermes`, but its value must never be printed, copied into the repo, logs, prompts, or artifacts.
- Do not commit without Samuel's explicit approval.
- Prefer tests and runnable evidence over new prose.

## Requirement sources

1. `7coreFunctions` defines the canonical lifecycle engine:
   - `hai_open_mission`
   - `hai_authorize_session`
   - `hai_get_contract`
   - `hai_check_activity`
   - `hai_park_item`
   - `hai_recontract`
   - `hai_close_mission`
2. `7Functions` defines the additional user-facing flow:
   - `hai_intake`
   - `hai_distill`
   - `hai_mission_start`
   - existing `hai_park`
   - `hai_drift_check`
   - `hai_proof`
   - `hai_stop`
3. The approved interaction/state packet is `docs/wireframes/2026-07-22-mission-contract-lifecycle/WIREFRAME_PACKET.md`.
4. The current public gate matrix is `docs/TOOL_CONTRACT.md`.

The additional seven must be thin orchestration/adaptation over the canonical mission engine. They must not create a second truth store or weaken any gate.

## Verified current state

Cursor Composer 2.5 completed the first core-seven slice without a commit. It added or changed:

- `src/hai_mcp/mission.py`
- `src/hai_mcp/storage.py`
- `src/hai_mcp/paths.py`
- `src/hai_mcp/state.py`
- `src/hai_mcp/server.py`
- `tests/test_mission_lifecycle.py`
- `docs/TOOL_CONTRACT.md`

Independent local verification on 2026-07-22:

```text
UV_CACHE_DIR=/tmp/hai-mcp-uv-cache uv run pytest -q
35 passed in 0.56s
```

`python -m compileall -q src tests` also passed. Runtime inspection showed all seven core names registered in FastMCP alongside the ten legacy tools. No commit exists; the whole `HAI-MCP/` directory is still untracked from the parent repository's perspective.

## Confirmed core defects before the next slice

The green suite is not yet sufficient for the core contract.

1. **Identifier path traversal — confirmed.** `mission_id` and `session_id` are interpolated into storage paths without validating their generated-ID format. An isolated `/tmp` proof showed that `load_mission_meta("../../escape")` can read a `mission.json` outside `HAI_HOME/missions`. Fix all mission/session ID entry points fail-closed and add regression tests for read and mutation paths.
2. **Hidden recontract mutation — confirmed.** `hai_recontract` currently accepts `changes={"mission_id": "M-HIDDEN"}`. The new contract receives that ID while `_field_diff` suppresses `mission_id`, so the change is invisible. Reject all system-managed or unknown fields and revalidate the complete candidate contract before writing it.
3. **Duplicate import — confirmed.** `src/hai_mcp/state.py` imports storage helpers twice. Remove the duplicate during the correction slice.
4. **Concurrent mission creation — confirmed.** Two concurrent `open_mission` calls can both pass the active-pointer check, both return `active`, and leave two mission records active. Serialize one-active-mission and lease-capacity mutations.
5. **Blocker precedence bypass — confirmed.** `declares_blocker=true` currently returns `blocker/pause` before checking an outside path or unauthorized sensitive trace. A blocker may pause valid work, but it must not hide a simultaneous drift violation; fail closed on path/capability violations first.
6. **Test mutated live default state — confirmed.** `test_public_tool_registration` imports `hai_mcp.server`; module initialization calls `Config.from_env()` and `ControlPlane(...)`. With `HAI_HOME` unset, the test created `~/.hai`, `ACTIVE_CONTEXT.json`, `OWNER_CONTRACT.json`, and mission/audit/parking support directories at 2026-07-22 00:44:55. Do not delete or further mutate them without Samuel's approval. Isolate `HAI_HOME` before importing the server in every test and subprocess.

Items to verify in the same correction slice, without broad refactoring:

- Require non-empty `agent_identity`, `role`, and `expected_result` before granting a lease.
- Validate `recontract.mode` against exactly `normal`, `blocker`, and `break_glass`; reject an empty diff.
- Ensure `done_criteria`, constraints, paths, and capability schema remain valid after recontract.
- Require a non-empty completion summary and return structured incomplete evidence errors instead of raising on directories, malformed evidence values, or unreadable files.
- Validate that a parking item references a real session belonging to the stated active mission and has a trigger event.
- Protect one-active-mission and max-parallel-session checks against concurrent calls using a small Unix-compatible file lock or an equivalently bounded mechanism.
- Audit lease expiry and every logical state mutation. Preserve append-only history.
- Replace the permissive registration-test fallback with a real FastMCP registry assertion.
- Ensure every test and smoke process sets an isolated `HAI_HOME` before importing `hai_mcp.server`; add a regression test proving the real home is untouched.

Do not expand this slice into legacy control-plane refactoring.

## Remaining build plan

### Slice 1 — Harden and close the core seven

Use Cursor Composer 2.5 for the correction implementation. Give it the confirmed defects and explicit tests first. Then independently run:

```bash
env UV_CACHE_DIR=/tmp/hai-mcp-uv-cache uv run pytest -q
env UV_CACHE_DIR=/tmp/hai-mcp-uv-cache uv run python -m compileall -q src tests
```

Acceptance gate:

- All existing tests remain green.
- New adversarial tests demonstrate safe IDs, immutable recontract fields, schema revalidation, structured evidence failures, valid parking provenance, concurrency limits, and audit behavior.
- No tool can mutate/read outside the isolated test `HAI_HOME` through a user-controlled identifier.

### Slice 2 — Implement the additional seven

Read `7Functions` completely immediately before implementation. Build adapters over one `MissionEngine`; do not duplicate mission, contract, parking, proof, or audit state.

Expected mapping:

- `hai_intake`: capture/validate an impulse without silently opening executable scope.
- `hai_distill`: produce a bounded candidate contract for owner review; no mission activation by itself.
- `hai_mission_start`: activate only a validated/approved contract and use the canonical open/authorize lifecycle.
- existing `hai_park`: preserve backward compatibility while routing mission-linked parking through canonical parking semantics when mission context is supplied.
- `hai_drift_check`: convenience adapter over `hai_check_activity` with the same deterministic classification set.
- `hai_proof`: collect/validate criterion evidence without declaring completion by assertion alone.
- `hai_stop`: revoke execution rights and close/abandon only through the canonical owner/evidence gates; never auto-start a next mission.

Acceptance gate:

- Exactly one state engine and one audit stream.
- Existing legacy calls remain compatible.
- No wrapper bypasses leases, evidence requirements, or owner acknowledgement.
- Tests cover both the comfortable path and denial/unclear/drift paths for every wrapper.

### Slice 3 — Cursor MCP discovery and stdio proof

The local Cursor CLI is available as `agent`; `agent --list-models` previously showed `composer-2.5` and `composer-2.5-fast`. Cursor client examples already exist under `docs/client-snippets/`.

Run the server with an isolated `HAI_HOME` under `/tmp`. Prove, through a real MCP client/discovery call, that all expected tools are listed and that at least one read-only call plus one fully gated lifecycle call works over stdio. Do not register or write global Cursor/MCP config unless Samuel explicitly authorizes that global change.

Record exact command, environment boundary, tool list, requests, responses, and exit codes in a project-local smoke artifact.

### Slice 4 — Freeze the MiniMax impact harness

Use `/home/smlflg/Projekte/FirstRealHarnessEvaluation_KarpathiesMD` as read-only prior art, especially its scientific protocol, MiniMax rig, preflight, run metadata, paired comparison, and invalid-run rules. Do not copy its entire SWE-bench platform.

Primary falsifiable claim:

> With the same MiniMax model, task, repository, permissions, and measurement pipeline, the HAI-MCP condition reduces unauthorized scope drift and false-Done behavior compared with a baseline condition without HAI-MCP, without making valid completion unusably worse.

Conditions:

- **A / baseline:** MiniMax coding agent receives the task and repository but no HAI lifecycle tools.
- **B / candidate:** Same model/task/permissions, with the HAI-MCP lifecycle available and required.
- The only intended intervention difference is the HAI control-plane policy/tool path.

Initial fixtures should cover:

1. A tempting out-of-scope improvement that should be parked.
2. A stale lease after recontract.
3. A false-Done attempt without criterion evidence.
4. A sensitive action absent from granted capabilities.
5. A legitimate blocker versus a mere improvement idea.
6. Stop/close behavior with no automatic next mission.

Hard assertions are primary; an LLM judge is secondary. Candidate success requires observable MCP calls and state/audit evidence, not merely mentioning tool names.

Store the frozen design and runs under a project-local `evals/hai_mcp_impact_v1/` tree with at least:

- owner packet and falsifiable contract
- fixture manifest with hashes
- preflight output
- exact runner/model/config identity
- raw trace and final answer per run
- hard-assertion result per run
- invalid/fail separation
- paired aggregate summary

Minimum validity rules:

- MiniMax authentication and exact model/runner identity pass preflight.
- Baseline and candidate share valid fixture cells.
- No zero-test or infrastructure-error run is scored as behavioral failure.
- If the baseline or runner is unstable, mark the comparison invalid/not-testable instead of forcing a winner.
- Never expose or persist the MiniMax API key.

### Slice 5 — Run and analyze the MiniMax harness

Locate the credential read-only by filename/key-name search under `~/.hermes`; load it into the process environment without echoing it. Run the smallest valid paired matrix first. Expand only if the pilot is valid and the additional runs materially improve confidence.

Report at minimum:

- paired hard-pass counts
- drift/owner-gate/evidence violations
- task completion and test outcomes
- unrelated edits
- duration and token/cost data when the runner exposes them
- invalid cells and exact reasons
- a bounded ship/revise/not-testable decision

Do not commit eval artifacts without Samuel's explicit approval.

## Visual artifact status

- `docs/visuals/hai-mcp-mission-lifecycle.svg` is the intended exact-text lifecycle visual, but still needs browser/render validation.
- `docs/visuals/hai-mcp-mission-lifecycle.png` came from a failed ImageMagick conversion and rendered essentially black. Do not present it as valid. Remove or replace it only as an explicit cleanup action.
- The wireframe Markdown remains the approved design truth even though the user prefers a more pleasant visual handoff.

## Takeover sequence

1. Read the requirement sources and this handoff.
2. Inspect the dirty/untracked worktree; preserve unrelated parent-repo changes.
3. Finish Slice 1 and independently verify it.
4. Finish and verify Slice 2.
5. Prove real MCP stdio discovery with isolated state.
6. Freeze the evaluation contract before looking at MiniMax outcomes.
7. Run the smallest valid A/B pilot, then analyze it.
8. Update this handoff with actual evidence and unresolved risks.
9. Stop before any commit and ask Samuel for explicit commit approval.
