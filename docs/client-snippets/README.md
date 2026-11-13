# Client snippets

Model-agnostic: same server binary, different host config files.

1. Adjust absolute `--directory` path to your clone.
2. Set `HAI_HOME` if not `~/.hai`.
3. Restart the client and verify tools: `hai_health`, `hai_status`, …

Legacy Hermes bridge (`~/.config/hai-agent-mcp`) can coexist; prefer this server for control-plane tools.

## Lease guard hook (optional)

`claude-code.guard-hook.json` goes into `~/.claude/settings.json` (or a project `.claude/settings.json`). Before every file write the hook asks `hai-guard` whether the path is covered by the active mission:

- outside every mounted project: always allowed
- inside a mounted project: needs an active mission, a valid lease and a match against `allowed_paths`

Exit 2 blocks the write and shows the reason to the agent. The guard is read-only and does not see shell commands. Try it by hand:

```bash
uv run hai-guard --path /some/project/src/file.py
```
