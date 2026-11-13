# GOAL — HAI-MCP

**Current WIP:** Slice — Lease guard for writes (`hai-guard`)

This is the canonical takeover document for the active slice. It does not replace `docs/TOOL_CONTRACT.md`. Background and evidence: `docs/plans/2026-11-13-h2a-guardrails.md`.

Previous slice (single-writer central core: logical projects, device mounts, streamable HTTP) shipped in #5.

## Thesis (this slice)

HAI-MCP only sees what an agent reports. A session that never calls `hai_check_activity` has no rules. The gap is not another server gate; it is a check the **harness** runs before a write, so the contract applies whether or not the agent cooperates.

HAI-MCP remains a fail-closed contract kernel, not an adversarial security layer. The guard covers file-write tools that a hook can intercept. It does not see shell commands.

## In scope

1. **`hai-guard` CLI.** Read-only. Exit 0 allows, exit 2 blocks, one JSON line on stdout, reason on stderr.
2. **Governed roots only.** A path is governed when it lies under a device mount in `core/projects.json` or under the active mission's project root. Everything else is always allowed.
3. **Inside a governed root** a write needs: an active mission whose root contains the path, at least one valid session lease, and a match against `constraints.allowed_paths`.
4. **Hook example** for Claude Code in `docs/client-snippets/`.

## Out of scope (explicit)

- New MCP tools, new contract fields
- Identifying *which* session is writing (any valid lease on the active mission passes)
- Shell-command inspection
- Audit events from the guard (it never writes to `HAI_HOME`)
- Context seal (branch/HEAD on the lease) and a drain for parked items: later slices, see the plan
- LLM calls inside the server
- Changing live `~/.hai` or unrelated HAI apps

## Compatibility

No server behaviour changes. Without the hook installed nothing changes for any client.

## Acceptance

In an isolated `HAI_HOME` (`tests/test_guard.py`):

1. A path outside every mount is allowed, with or without an active mission.
2. A path in a mounted project is blocked when no mission is active, when the active mission belongs to another project, when no valid lease exists (none, expired, or from an older contract version), and when it is outside `allowed_paths`.
3. The same path is allowed with a valid lease inside `allowed_paths`.
4. Unreadable control-plane state blocks (fail-closed).
5. The guard leaves `HAI_HOME` byte-identical.

## Measure

Two weeks after the hook is installed on one harness: count of blocked writes per reason. Zero blocks means the guard is either unnecessary or not wired in.
