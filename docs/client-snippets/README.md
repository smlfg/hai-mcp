# Client snippets

Model-agnostic: same server binary, different host config files.

1. Adjust absolute `--directory` path to your clone.
2. Set `HAI_HOME` if not `~/.hai`.
3. Restart the client and verify tools: `hai_health`, `hai_status`, …

Legacy Hermes bridge (`~/.config/hai-agent-mcp`) can coexist; prefer this server for control-plane tools.
