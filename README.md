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

## Tools (v0.1 — 23 tools, one state engine)

Control plane (legacy surface):

| Tool | Role |
| --- | --- |
| `hai_health` | Server + HAI\_HOME health (incl. owner-gate mode) |
| `hai_status` | Active lanes, focus, inbox, pending owner challenges |
| `hai_get_next_step` | Canonical NEXT\_STEP |
| `hai_read_artifacts` | Run-contract artifact summary |
| `hai_park` | Park a thought (no lane steal) |
| `hai_set_focus` | Set/switch focus (max 2 ACTIVE) |
| `hai_propose_next_step` | Write proposed next step |
| `hai_accept_next_step` | Promote proposed → canonical (**owner gate**) |
| `hai_checkpoint` | Snapshot context |
| `hai_recover` | Smallest recovery next action |

Mission lifecycle (canonical engine):

| Tool | Role |
| --- | --- |
| `hai_open_mission` | Open a bounded mission with a versioned contract |
| `hai_authorize_session` | Time-bounded session lease on an exact contract version |
| `hai_get_contract` | Exact contract for a valid lease |
| `hai_check_activity` | Deterministic drift classification |
| `hai_park_item` | Mission-linked parking, no execution right |
| `hai_recontract` | Visible field-level diff, revokes leases (**owner gate**) |
| `hai_close_mission` | Complete with evidence, or abandon (**owner gate**) |

Daily loop (thin wrappers over the engine):

| Tool | Role |
| --- | --- |
| `hai_intake` | Capture a raw thought immutably |
| `hai_distill` | Exactly one decision + one next step; the rest is parked |
| `hai_mission_start` | Fast start → `hai_open_mission` |
| `hai_drift_check` | → `hai_check_activity` |
| `hai_proof` | → `hai_close_mission(completed)` |
| `hai_stop` | Hard day terminal; revokes leases, no next-day plan |

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
