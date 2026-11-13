# GOAL — HAI-MCP

**Current WIP:** Slice — Single-Writer Central Core (logical projects + device mounts + streamable HTTP)

This is the canonical takeover document for the active slice. It does not replace `docs/TOOL_CONTRACT.md`.

## Thesis (this slice)

HAI-MCP remains a fail-closed **contract kernel**, not an adversarial security layer and not a semantic verifier. The gap to a distributed HAI is not another agent. It is one process that owns missions, contracts, leases, and audit events so two devices can share **commitments** without sharing local paths, cookies, or caches.

Do **not** sync `HAI_HOME` via Syncthing/Dropbox/Git. The store uses local absolute paths and `fcntl` locks; file-level mirroring would split-brain.

## In scope

1. **One writer process.** The existing `MissionEngine` + `fcntl` lock stay the mutation authority. Streamable HTTP is an additional transport so multiple clients can talk to that process. stdio remains the default.
2. **Logical `project_id`.** Canonical identity in the contract is `constraints.project_id`. Device-local directories live in a mount table, not as the contract’s identity.
3. **Device + harness identity on leases.** `hai_authorize_session` records `device_id` and `harness_id` when provided. Path/evidence checks for `project_id` missions resolve through that device’s mount.
4. **Append-only event chain.** Existing per-event audit files gain a monotonic `seq`, `prev_event_id`, and content hash, written under the mission lock.

## Out of scope (explicit)

- Typed / semantic evidence (file existence + SHA-256 stays)
- Authenticated owner identity or one-time approval receipts (`owner_ack=true` remains a literal boolean)
- Knowledge graph, vector memory, chat sync
- Merging draft PRs #1/#2/#3
- Changing live `~/.hai` or unrelated HAI apps
- LLM calls inside the server
- New repository

## Compatibility

Existing missions that only set `constraints.project_path` keep today’s behavior. All current happy paths and owner gates stay green. `project_id` is additive.

## Acceptance

A single isolated `HAI_HOME` with two fake device roots (no copy-paste of mission JSON):

1. Open a mission on device `macbook` with `project_id` + that device’s local path.
2. Bind device `thinkpad` to the same `project_id` (owner gate).
3. Authorize a session as `thinkpad` / some `harness_id`.
4. `hai_get_contract` returns the same `mission_id`, `contract_version`, `contract_hash`, and `project_id`.
5. Activity and completion evidence resolve against the ThinkPad mount, not the MacBook path.
6. The canonical contract does **not** treat a MacBook absolute path as identity.
7. HTTP: two clients against one process see the same active mission; non-loopback bind without `HAI_HTTP_TOKEN` is refused.

## Transport (fail-closed)

- Default: stdio (`uv run hai-mcp`)
- Optional: `--transport streamable-http` (env `HAI_TRANSPORT`)
- Default bind `127.0.0.1`. Non-loopback host requires non-empty `HAI_HTTP_TOKEN`. If a token is configured, HTTP requests must present it.
