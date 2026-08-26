# HAI-MCP Build Handoff

**Updated:** 2026-08-26  
**Owner:** Samuel Fleig  
**Canonical purpose:** This is the single takeover document for the active HAI-MCP build. It records verified state, unresolved findings, and the remaining implementation/evaluation plan. It does not replace `GOAL.md`, `docs/TOOL_CONTRACT.md`, `7coreFunctions`, or `7Functions` as requirement sources when those files exist.

## Non-negotiable boundaries

- Canonical published repo: `https://github.com/smlfg/hai-mcp` (work in the checked-out tree).
- Read `AGENTS.md`, `docs/TOOL_CONTRACT.md`, and this handoff before editing. Also read `GOAL.md`, `7coreFunctions`, and `7Functions` when present.
- One WIP slice at a time.
- No LLM calls inside the MCP server; models remain client-side.
- Owner gates remain fail-closed.
- Do not mutate live `~/.hermes`, live `~/.hai`, unrelated HAI apps, or parent repositories.
- The MiniMax credential may be located read-only under `~/.hermes`, but its value must never be printed, copied into the repo, logs, prompts, or artifacts.
- Do not commit without Samuel's explicit approval (cloud-agent draft PRs are the exception for review, not merge).
- Prefer tests and runnable evidence over new prose.

## Requirement sources

1. `7coreFunctions` (when present) defines the canonical lifecycle engine:
   - `hai_open_mission`
   - `hai_authorize_session`
   - `hai_get_contract`
   - `hai_check_activity`
   - `hai_park_item`
   - `hai_recontract`
   - `hai_close_mission`
2. `7Functions` (when present) defines the additional user-facing flow:
   - `hai_intake`
   - `hai_distill`
   - `hai_mission_start`
   - existing `hai_park`
   - `hai_drift_check`
   - `hai_proof`
   - `hai_stop`
3. The approved interaction/state packet is `docs/wireframes/2026-07-22-mission-contract-lifecycle/WIREFRAME_PACKET.md` (when present).
4. The current public gate matrix is `docs/TOOL_CONTRACT.md`.

**Unresolved risk:** `GOAL.md`, `7coreFunctions`, and `7Functions` are **absent** from the published tree at `7300f0f`. Do not invent them. Continue from `docs/TOOL_CONTRACT.md`, this handoff, `docs/plans/*`, `docs/eval/AB_HARNESS_CONTRACT.md`, and the existing code/tests.

The additional seven must be thin orchestration/adaptation over the canonical mission engine. They must not create a second truth store or weaken any gate.

## Verified current state (2026-08-26)

Base commit: `7300f0f` — *HAI-MCP 0.1.0 — mission-contract control plane (fail-closed, 139 tests green)*.

Independent cloud-agent verification (isolated `HAI_HOME`, no live `~/.hai`):

```text
env UV_CACHE_DIR=/tmp/hai-mcp-uv-cache uv sync --all-extras
env UV_CACHE_DIR=/tmp/hai-mcp-uv-cache uv run pytest -q
139 passed in 7.02s

env UV_CACHE_DIR=/tmp/hai-mcp-uv-cache uv run python -m compileall -q src tests
# exit 0

env UV_CACHE_DIR=/tmp/hai-mcp-uv-cache uv run python scripts/stdio_smoke.py --hai-home /tmp/hai-mcp-stdio-verify
# ok=true exit_code=0; tool_count=23; touches_live_dot_hai=false
```

Live `~/.hai` did not exist before or after verification (`LIVE_DOT_HAI_TOUCHED=false`).

Registered tools (23):  
`hai_accept_next_step`, `hai_authorize_session`, `hai_check_activity`, `hai_checkpoint`, `hai_close_mission`, `hai_distill`, `hai_drift_check`, `hai_get_contract`, `hai_get_next_step`, `hai_health`, `hai_intake`, `hai_mission_start`, `hai_open_mission`, `hai_park`, `hai_park_item`, `hai_proof`, `hai_propose_next_step`, `hai_read_artifacts`, `hai_recontract`, `hai_recover`, `hai_set_focus`, `hai_status`, `hai_stop`.

### Slice status

| Slice | Status | Evidence |
|---|---|---|
| 1 — Harden core seven | **done** (on main) | Fail-closed IDs/paths/recontract/owner gates covered by suite |
| 2 — Additional seven flow tools | **done** (on main) | Flow tools + symlink store confinement tests |
| 3 — Cursor MCP stdio proof | **done** | `scripts/stdio_smoke.py` + `evals/stdio_smoke/latest.json` |
| 4 — Freeze MiniMax impact harness | **deterministic freeze complete** | `evals/hai_mcp_impact_v1/` owner packet, contract, fixtures+hashes, preflight, runner identity, per-cell traces/assertions, aggregate summary; `provider=null` reference run 6/6 hard_pass |
| 5 — Run/analyze live MiniMax A/B | **blocked / pending** | Not started; do not mark MiniMax done |

Discarded hypothesis: intake/distill/stop `mkdir`-before-`assert_under` gaps are already covered by `tests/test_flow_tools.py` symlink cases (3 passed).

Prior handoff text that claimed “35 passed” / “untracked tree” is **stale** and superseded by this section.

## Historical defects (Slice 1 — addressed on main)

The following were confirmed on 2026-07-22 and are treated as closed by the green 0.1.0 suite; re-open only with a failing regression:

1. Identifier path traversal on mission/session IDs.
2. Hidden recontract mutation of system-managed fields (e.g. `mission_id`).
3. Duplicate import in `state.py`.
4. Concurrent mission creation / lease-capacity races.
5. Blocker precedence bypassing path/capability drift.
6. Tests mutating live default `~/.hai` via server import without isolated `HAI_HOME`.

## Remaining build plan

### Slice 4 — Freeze the MiniMax impact harness (deterministic portion)

Primary falsifiable claim (unchanged; **not yet tested by MiniMax**):

> With the same MiniMax model, task, repository, permissions, and measurement pipeline, the HAI-MCP condition reduces unauthorized scope drift and false-Done behavior compared with a baseline condition without HAI-MCP, without making valid completion unusably worse.

Deterministic six-cell harness (already in tree):

- `src/hai_mcp/eval_impact.py`
- `tests/test_impact_eval_v1.py`
- `scripts/run_impact_eval_v1.py`

Frozen project-local tree: `evals/hai_mcp_impact_v1/` with owner packet, falsifiable contract, fixture manifest hashes, preflight, runner identity, raw-trace placeholders, hard-assertion results, invalid/fail separation, and paired aggregate summary (`minimax_ab=not_run`).

Do **not** copy FirstRealHarnessEvaluation_KarpathiesMD wholesale. Do **not** add MiniMax live runs in this slice.

### Slice 5 — Run and analyze the MiniMax harness (pending)

Locate the credential read-only by filename/key-name search under `~/.hermes`; load it into the process environment without echoing it. Run the smallest valid paired matrix first.

Report at minimum:

- paired hard-pass counts
- drift/owner-gate/evidence violations
- task completion and test outcomes
- unrelated edits
- duration and token/cost data when the runner exposes them
- invalid cells and exact reasons
- a bounded ship/revise/not-testable decision

Do not commit eval artifacts without Samuel's explicit approval. Never expose the MiniMax API key.

## Visual artifact status

- `docs/visuals/hai-mcp-mission-lifecycle.svg` is the intended exact-text lifecycle visual, but still needs browser/render validation when present.
- `docs/visuals/hai-mcp-mission-lifecycle.png` historically came from a failed ImageMagick conversion; do not present a black render as valid.
- The wireframe Markdown remains the approved design truth when present.

## Takeover sequence

1. Read the requirement sources and this handoff.
2. Independently verify `pytest` / `compileall` / stdio smoke with isolated `HAI_HOME`.
3. Treat Slices 1–3 as done on `7300f0f` unless verification fails.
4. Keep Slice 4 deterministic freeze artifacts coherent with the harness.
5. Do not start Slice 5 MiniMax scoring until the freeze packet is accepted.
6. Update this handoff with actual evidence and unresolved risks.
7. Stop before merge; ask Samuel for explicit commit/merge approval on non-draft work.
