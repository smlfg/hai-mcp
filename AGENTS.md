# AGENTS.md — HAI-MCP

## Read first

1. `GOAL.md`
2. `docs/TOOL_CONTRACT.md`

## Rules

- One WIP slice at a time
- No LLM calls inside the server
- Owner gates stay fail-closed
- Do not mutate live `~/.hermes` or unrelated HAI apps
- Prefer tests over new docs
- English for code; German OK for owner-facing docs

## Layout

- `src/hai_mcp/` — server + control-plane logic
- `tests/` — unit + gate tests
- `docs/client-snippets/` — optional client config examples
