# HAI-MCP

Model-agnostic **Human-Agent Interface** control plane as an MCP server.

Any client (Claude Code, Codex, Cursor, Grok, OpenCode, Hermes, …) can use the same tools. The server never calls an LLM.

## Install / run

```bash
cd HAI-MCP
uv sync --all-extras
uv run hai-mcp
```

stdio MCP. Point your client at:

```json
{
  "command": "uv",
  "args": ["run", "--directory", "/absolute/path/to/HAI-MCP", "hai-mcp"],
  "env": {
    "HAI_HOME": "/home/you/.hai"
  }
}
```

## Tools (v1)

| Tool | Role |
|---|---|
| `hai_health` | Server + HAI_HOME health |
| `hai_status` | Active lanes + focus |
| `hai_get_next_step` | Canonical NEXT_STEP |
| `hai_read_artifacts` | Run-contract artifact summary |
| `hai_park` | Park a thought (no lane steal) |
| `hai_set_focus` | Set/switch focus (max 2 ACTIVE) |
| `hai_propose_next_step` | Write proposed next step |
| `hai_accept_next_step` | Promote proposed → canonical (**owner gate**) |
| `hai_checkpoint` | Snapshot context |
| `hai_recover` | Smallest recovery next action |

See `docs/TOOL_CONTRACT.md`.

## State

- Global: `$HAI_HOME` (default `~/.hai`)
- Per project: `<project>/Projek-Managment/`

## Legacy

`~/.config/hai-agent-mcp` is Hermes-coupled prior art. This repo replaces that role for control-plane work; coexistence is fine until you switch clients deliberately.

## Tests

```bash
uv run pytest
```
