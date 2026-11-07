# Client snippets

Model-agnostic: same server binary, different host config files.

1. Adjust absolute `--directory` path to your clone.
2. Set `HAI_HOME` if not `~/.hai`.
3. Restart the client and verify tools: `hai_health`, `hai_status`, …

Legacy Hermes bridge (`~/.config/hai-agent-mcp`) can coexist; prefer this server for control-plane tools.

## Enforcement hooks (`hai-guard`)

The MCP server only *offers* tools. `hai-guard` makes the active mission binding: a harness
hook runs it before every prompt (`UserPromptSubmit`) and every tool call (`PreToolUse`).
Exit 0 = allow, exit 2 = block (reason on stderr). Internal errors also exit 2 (fail-closed).

- Claude Code: merge `claude-code.hooks.json` into `~/.claude/settings.json` (user scope, so
  it applies in every repo, not just the mission repo).
- Codex: `codex.hooks.json` uses the same payload/exit-code protocol; place it where your
  Codex version reads hooks (e.g. `~/.codex/hooks.json`) and **verify once live** — the
  Codex hook schema was not tested against a real Codex binary here.
- Any other harness (OpenCode plugin, …): pipe `{"hook_event_name", "cwd", "prompt" | "tool_name", "tool_input"}` JSON into `hai-guard --harness <id>` and block on exit code 2.

All harnesses on one device must use the **same `HAI_HOME`** and the same `HAI_DEVICE_ID`
(required for `project_id` missions). Not synced across devices; see GOAL.md.

What it decides (deterministic, no LLM):

| Situation | Result |
|---|---|
| Read-only tool, or any `hai_*` tool | allow |
| No active mission | prompts allowed (to open one); acting tools blocked |
| cwd outside mission project (this device's mount) | block; prompt is parked via `hai_intake` |
| Prompt matches a contract non-goal | block + park |
| Write/Edit/apply_patch target outside `allowed_paths` | block |
| Shell `cd` outside project; `git commit/push`, `rm` without capability | block |
| Unknown / other MCP tool | allowed only with cwd inside the project (`HAI_GUARD_ALLOW_TOOLS=glob,…` to allowlist) |

Every block is written to the audit chain as `guard_denied`. A deliberate change of
direction stays possible and visible: `hai_recontract`, `hai_close_mission` or
`open_mission` with `supersede_orphan` — all owner-gated.
