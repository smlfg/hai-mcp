# HAI-MCP

HAI-MCP is a **fail-closed contract kernel** for cooperative or instrumented agents.
It is not the full Human Agent Interface, not an adversarial security boundary,
and not a semantic verifier. Any client (Claude Code, Codex, Cursor, Grok, OpenCode, Hermes, …)
can share one deterministic contract: missions, versioned contracts, session leases,
path confinement, parking, recontracting, recovery, and append-only audit. The server never calls an LLM.

## Install / run

```bash
cd HAI-MCP
uv sync --all-extras
uv run hai-mcp
```

Default transport is **stdio** MCP. Point your client at:

```json
{
  "command": "uv",
  "args": ["run", "--directory", "/absolute/path/to/HAI-MCP", "hai-mcp"],
  "env": {
    "HAI_HOME": "/home/you/.hai"
  }
}
```

Opt-in **loopback Streamable HTTP** (same trust as stdio; no transport auth):

```bash
HAI_MCP_TRANSPORT=streamable-http \
HAI_MCP_HTTP_HOST=127.0.0.1 \
HAI_MCP_HTTP_PORT=8765 \
uv run hai-mcp
```

Only loopback hosts (`127.0.0.1`, `localhost`, `::1`) are accepted.

## Tools (v1)

| Tool | Role |
|---|---|
| `hai_health` | Server + HAI_HOME health |
| `hai_register_mount` | Register per-device project mount (**owner gate**) |
| `hai_status` | Active lanes + focus |
| `hai_get_next_step` | Canonical NEXT_STEP |
| `hai_read_artifacts` | Run-contract artifact summary |
| `hai_park` | Park a thought (no lane steal) |
| `hai_set_focus` | Set/switch focus (max 2 ACTIVE) |
| `hai_propose_next_step` | Write proposed next step |
| `hai_accept_next_step` | Promote proposed → canonical (**owner_ack gate**) |
| `hai_checkpoint` | Snapshot context |
| `hai_recover` | Smallest recovery next action |

See `docs/TOOL_CONTRACT.md` for mission lifecycle tools and kernel limits.

## State

- Global: `$HAI_HOME` (default `~/.hai`)
- Logical projects: `$HAI_HOME/projects.json` (device mounts)
- Per project: `<mount_root>/Projek-Managment/`

## Legacy

`~/.config/hai-agent-mcp` is Hermes-coupled prior art. This repo replaces that role for control-plane work; coexistence is fine until you switch clients deliberately.

## Tests

```bash
uv run pytest
```
