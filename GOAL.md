# GOAL — HAI-MCP

## What this is

HAI-MCP is a **fail-closed contract kernel** for cooperative or instrumented agents.
It is **not** the full Human Agent Interface, **not** an adversarial security boundary,
and **not** a semantic verifier.

The server never calls a model. Clients (Claude Code, Codex, Cursor, Hermes, …)
share one deterministic contract: missions, versioned contracts, session leases,
path confinement, parking, recontracting, recovery, and append-only audit.

Honest limits of v0.1 (unchanged by this slice, only named):

- `owner_ack=true` is a caller-supplied boolean, not an authenticated human identity.
- Activity checks classify declared criterion IDs, paths, and supplied traces.
  Missing traces means the kernel trusts the caller.
- Completion evidence checks that a file exists under the project root and hashes
  SHA-256. It does **not** prove the file contents satisfy the criterion.

## What this is not (do not build in this slice)

- Approval receipts / owner authentication
- Typed or semantic evidence (tests, browser snapshots, human review types)
- Knowledge graph, embeddings, or chat vectorization
- Syncing `~/.hai` via Git, Syncthing, or Dropbox (split-brain)
- A second repo, a second agent, or MiniMax live A/B
- Binding HTTP to a non-loopback address (no transport auth yet)

## Active WIP slice — Central commitment core

**Thesis:** do not synchronize brains. Synchronize commitments.

One process owns missions, contracts, leases, decisions, and audit events.
Devices keep local paths, caches, cookies, and harness quirks. They share
`project_id`, contract version, decisions, parked items, evidence hashes,
and session capsules.

### In scope

1. **Logical projects + device mounts**
   - Owner-gated `hai_register_mount(project_id, device_id, root_path, owner_ack, reason)`.
   - Registry: `$HAI_HOME/projects.json` (atomic write, confined to HAI_HOME).
   - `project_id` / `device_id` / `harness_id` are fail-closed slugs
     (`^[a-z][a-z0-9-]{1,63}$`). Never interpolated as paths.
   - Mission `constraints.project_id` is the shared root identity.
   - Absolute `constraints.project_path` remains the **legacy local** fallback.
     When `project_id` is set, path/evidence resolution uses the **session's
     `device_id` mount**, never another device's absolute path.

2. **Session device/harness identity**
   - `hai_authorize_session` accepts optional `device_id` and `harness_id`.
   - If the contract has `project_id`, both are **required** and the device
     must already have a mount. Otherwise deny (`invalid_args` / `denied`).
   - Legacy missions without `project_id` stay compatible (fields optional).

3. **Single-writer event log**
   - Keep per-event `$HAI_HOME/audit/A-*.json`.
   - Also append the same record as one JSON line to
     `$HAI_HOME/audit/events.jsonl` under an exclusive lock (no torn lines).
   - Never rewrite or delete historical events.

4. **Loopback Streamable HTTP (opt-in)**
   - Default transport remains **stdio**.
   - `HAI_MCP_TRANSPORT=streamable-http` uses the existing MCP 1.x FastMCP
     transport (`mcp==1.28.x` — do not upgrade the SDK).
   - Host default `127.0.0.1`, port default `8765`.
   - Reject any non-loopback host fail-closed. HTTP is a same-trust
     single-writer socket, not a public API.

5. **Naming the kernel** in README + `docs/TOOL_CONTRACT.md`.

### Acceptance test (simulated two devices, one HAI_HOME)

1. Register mounts `macbook` and `thinkpad` for `project_id=hai-mcp` with
   different roots.
2. Open a mission with `project_id` (no shared absolute path).
3. Authorize a session on `macbook`; continue on `thinkpad` with its own
   session (`max_parallel_sessions>=2`) and no copy-paste of contract JSON.
4. Write evidence under the ThinkPad mount; close with that `device_id`.
5. MacBook sees the same completed mission, contract version, and audit/event
   log. A MacBook-absolute path used from the ThinkPad session is `drift/stop`.

A device without a mount cannot authorize, check paths, or close.

### Out of slice (next, not now)

- Authenticated approval receipts (`actor_id`, action hash, expiry, nonce)
- Typed evidence verifiers
- Cross-machine networking beyond loopback + SSH tunnel
- Merging draft PRs #1/#2/#3

## Rules

- One WIP slice at a time
- No LLM calls inside the server
- Owner gates stay fail-closed (`owner_ack is True`)
- Do not mutate live `~/.hermes` or live `~/.hai`
- Prefer tests over new docs
- English for code; German OK for owner-facing docs
