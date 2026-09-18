<!--
README for hai-mcp — polished for the public GitHub repo (smlfg/hai-mcp).
Banner + architecture overview (MCP control-plane explained) + features +
sample integration snippets + limitations + license.
-->

<p align="center">
  <img alt="HAI-MCP banner" src="docs/visuals/hai-mcp-mission-lifecycle.svg" width="100%">
</p>

<p align="center">
  <strong>HAI-MCP</strong> · Model-agnostic <a href="https://modelcontextprotocol.io">MCP</a> control-plane
  for the <strong>Human Agent Interface (HAI)</strong> approach
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue.svg"></a>
  <a href="pyproject.toml"><img alt="Python ≥ 3.12" src="https://img.shields.io/badge/python-≥3.12-3776AB.svg"></a>
  <a href="https://modelcontextprotocol.io"><img alt="MCP" src="https://img.shields.io/badge/MCP-1.9%20|%20%3C2-8A2BE2.svg"></a>
  <a href="https://github.com/smlfg/hai-mcp/issues"><img alt="Issues" src="https://img.shields.io/github/issues/smlfg/hai-mcp"></a>
</p>

---

## Why this repo exists

Agents are now driving shells, editors, browsers, and CI. Most of them work
without a human in the loop long enough for the human to lose the thread.
HAI — the **Human Agent Interface** — is an opinionated, evidence-based way
to keep agentic work **observable, bounded, owner-gated, and recoverable**
so a human can still *own* the work even when an agent is doing it.

**HAI-MCP** is the open-source, model-agnostic implementation of that approach.
It is one of the pieces of Samuel Fleig's HAI stack — the part that any
Model-Context-Protocol-speaking client can talk to, regardless of which LLM
sits behind it.

> Canonical site: <https://www.human-agent-interface.com/> ·
> About Samuel: <https://www.human-agent-interface.com/samuel/>

---

## What "MCP control-plane" means here

**MCP** — the [Model Context Protocol](https://modelcontextprotocol.io) — is a
standard JSON-RPC interface that lets a tool server expose **tools,
resources, and prompts** to an LLM-backed client (Claude Code, Codex, Cursor,
Grok, OpenCode, Hermes, …). The client discovers the server's tools and
calls them during a conversation; the server stays stateless from the
model's point of view.

A **control-plane** server is one that does **not do the work itself** — it
governs how work is authorised, scoped, recorded, and handed back to the
human. Concretely, an MCP control-plane:

| Concern | What HAI-MCP exposes for it |
|---|---|
| **Mission contract** | `hai_open_mission`, `hai_get_contract`, `hai_recontract` |
| **Session leasing** | `hai_authorize_session` — time-bounded, contract-versioned |
| **Activity classification** | `hai_check_activity` / `hai_drift_check` — on-contract vs. off-contract |
| **Focus & lanes** | `hai_set_focus`, `hai_status` — at most 2 ACTIVE lanes |
| **Owner gate** | one-time nonces out-of-band (`HAI_OWNER_HOME` or ntfy) for `accept` / `recontract` / `abandon` |
| **Evidence & closure** | `hai_proof`, `hai_close_mission`, `hai_stop` |
| **Inbox & intake** | `hai_intake`, `hai_distill`, `hai_park`, `hai_park_item` |
| **Recovery** | `hai_checkpoint`, `hai_recover` |

HAI-MCP is deliberately **not** an agent runtime. It never calls an LLM,
never runs shell, never pushes to git, and never deletes files. It is the
judge — agents call it to ask *"may I?"* and to record *"I did"*.

---

## Features

- **One state engine, 23 tools** — three surfaces (legacy control plane,
  mission lifecycle, daily-loop wrappers) sharing one hardened core.
- **Strict fail-closed boundary** — inputs are typed and rejected, not
  coerced. A `String "2"` is refused for an `Integer` field; the server
  stays reachable instead of silently mangling types.
- **Versioned mission contracts** — every mission has a contract with a
  monotonic version. Recontracting produces a visible field-level diff and
  revokes every existing lease.
- **Time-bounded session leases** — `hai_authorize_session` binds a lease to
  a mission id, a contract version, a duration, a role, a contribution, and
  a claimed criterion set.
- **Deterministic drift check** — `hai_check_activity` classifies planned or
  observed activity against the contract without ever calling an LLM.
- **Two-principal owner gate**:
  - **Default (nonce):** the server delivers a one-time code to the owner
    channel (`HAI_OWNER_HOME/file` or ntfy). The client never sees the code.
    The code is bound to the exact change, single-use, expiring; only the
    hash is stored in `HAI_HOME`.
  - **`ack_legacy`:** the old self-asserted `owner_ack` flag — an honor
    system, surfaced as such by `hai_health`.
- **Mount table** — `hai_bind_project` joins a logical `project_id` to a
  device-local directory behind `owner_ack + reason`.
- **Model-agnostic** — same binary, same tools, same contract. Switch LLMs
  without rewriting governance.
- **Two transports** — stdio (default) and `streamable-http` with optional
  bearer-token binding (loopback only by default).
- **Checkpoint & recover** — `hai_checkpoint` snapshots context; `hai_recover`
  returns the smallest recovery next action.
- **139 tests green** — the engine is covered; the slice remains open
  for the learning-budget and owner-instrument follow-ups.

---

## Install & run

Requires **Python ≥ 3.12** and [`uv`](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/smlfg/hai-mcp.git
cd hai-mcp
uv sync --all-extras
uv run hai-mcp          # stdio transport, default
```

Streamable-HTTP transport:

```bash
HAI_TRANSPORT=streamable-http \
HAI_HTTP_HOST=127.0.0.1 \
HAI_HTTP_PORT=8765 \
HAI_HTTP_TOKEN="$(openssl rand -hex 32)" \
uv run hai-mcp
```

> HTTP binding to non-loopback hosts is refused unless `HAI_HTTP_TOKEN` is
> set. See `docs/OWNER_GATE.md` and the `--help` output.

---

## Sample usage — wiring the server into a client

The server is **transport-agnostic**; every MCP client gets the same 23
tools. Drop the snippet into the client's MCP config and restart.

### Claude Code / Claude Desktop

```json
{
  "mcpServers": {
    "hai": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/absolute/path/to/hai-mcp",
        "hai-mcp"
      ],
      "env": {
        "HAI_HOME": "/home/you/.hai",
        "HAI_OWNER_HOME": "/home/you/.hai-owner"
      }
    }
  }
}
```

### Cursor

```json
{
  "mcpServers": {
    "hai": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/absolute/path/to/hai-mcp",
        "hai-mcp"
      ],
      "env": {
        "HAI_HOME": "/home/you/.hai",
        "HAI_OWNER_HOME": "/home/you/.hai-owner"
      }
    }
  }
}
```

### Codex / Hermes / any MCP stdio client

Same shape — point at the `uv run --directory … hai-mcp` command and pass
the two env vars. More snippets live in `docs/client-snippets/`.

---

## Sample usage — driving the protocol

A typical agent loop calls HAI-MCP **before it acts** and **after it acts**.

### 1. Open a bounded mission

```text
hai_mission_start(
  problem   = "Refactor the evaluator harness to be deterministic.",
  artifact  = "Pull request titled 'evaluator: deterministic harness'.",
  done_criteria = [
    {"id": "C1", "description": "Two A/B cases produce identical verdicts on three reruns."},
    {"id": "C2", "description": "Harness runs offline; no live network calls."},
  ],
  owner             = "samuel",
  time_limit_hours  = 6,
  non_goals         = ["Do not touch the MCP server.", "Do not change contract shapes."],
  constraints       = {"python": ">=3.12", "branch": "slice/eval-determinism"},
)
```

### 2. Authorise a session lease

```text
hai_authorize_session(
  mission_id         = "<from open>",
  contract_version   = 1,
  agent_identity     = "codex-cli",
  role               = "implementer",
  contribution       = "Deterministic A/B harness",
  expected_result    = "Green pytest with frozen inputs.",
  duration_minutes   = 90,
  criterion_ids      = ["C1", "C2"],
  device_id          = "mbp-mirgraatin",
  harness_id         = "codex-cli/0.45",
)
```

### 3. Check an activity before running it

```text
hai_check_activity(
  session_id     = "<from lease>",
  activity_step  = "Replace random.seed with a seeded fixture in tests/test_eval_ab.py",
  criterion_id   = "C1",
  affected_paths = ["tests/test_eval_ab.py", "tests/fixtures/ab/case1.json"],
)
# → {"verdict": "on_contract", ...}
```

### 4. Close with evidence

```text
hai_proof(
  mission_id        = "<from open>",
  contract_version  = 1,
  outcome_summary   = "A/B cases produce identical verdicts on three reruns; harness offline.",
  evidence          = {
    "C1": {"command": "pytest tests/test_eval_ab.py -q", "exit_code": 0,
           "reruns": [0, 0, 0]},
    "C2": {"command": "pytest tests/test_eval_ab.py -q --offline", "exit_code": 0},
  },
)
```

### 5. Owner-gated promotion

For `hai_accept_next_step`, `hai_recontract`, or abandoning a mission, the
first call **without** `owner_code` delivers a one-time code to the owner's
channel (the `file` sink in `HAI_OWNER_HOME`, or an ntfy topic). Relaying
that code back as `owner_code=<code>` completes the action. The code is
bound to the exact change, single-use, and only its hash is persisted in
`HAI_HOME`. Detail: [`docs/OWNER_GATE.md`](docs/OWNER_GATE.md).

---

## State layout

- **Global:** `$HAI_HOME` — default `~/.hai`
- **Owner channel:** `$HAI_OWNER_HOME` — default `~/.hai-owner` (deliberately
  outside `HAI_HOME` so the agent cannot read it)
- **Per-project:** `<project_path>/Projek-Managment/` (legacy) or device
  mount + relative prefixes when a `project_id` is bound
- **Mount table:** `HAI_HOME/core/projects.json`

All writes are resolved and confined to these roots.

---

## Project layout

```
hai-mcp/
├── docs/
│   ├── TOOL_CONTRACT.md      # normative tool contract (gates, errors, schemas)
│   ├── OWNER_GATE.md         # nonce + ack_legacy owner-gate spec
│   ├── BUILD_HANDOFF.md
│   ├── client-snippets/      # ready-to-paste mcp.json snippets
│   ├── eval/                 # A/B harness contract
│   ├── plans/                # dated design slices
│   └── visuals/              # mission-lifecycle banner assets
├── scripts/                  # stdio smoke + impact-eval runner
├── src/hai_mcp/              # the server
└── tests/                    # 139 tests
```

---

## Limitations

HAI-MCP is intentionally narrow. **It does not:**

- **Call any LLM.** The server is dumb on purpose — governance is a separate
  concern from generation.
- **Execute code, run shells, push git, or delete files.** It refuses tools
  like *"commit & push"*, *"run pytest"*, *"rm -rf"*. Agents are expected to
  do those things and report back via `hai_check_activity` / `hai_proof`.
- **Merge with `hai-intake` packages.** The intake pipeline is a separate
  stack; coexistence is fine, fusion is not.
- **Provide a UI.** There is no dashboard. The control plane speaks MCP and
  writes JSON; visualisation is the client's job.
- **Multi-mission concurrency.** Exactly **one** active mission globally.
  Open a new one only after the previous is closed (completed or abandoned).
- **Cross-device writes.** Each device sees only its own mounts. The mount
  table is per-device; the contract is global.
- **Forward compatibility for major MCP bumps.** The server pins
  `mcp>=1.9.0,<2`; a v2 of MCP will require a separate migration slice.
- **No revocation list / no kill-switch API.** Lease invalidation happens
  via recontract, mission close, or `hai_stop`; there is no out-of-band
  *"revoke everything right now"* tool.
- **No automatic recovery from a corrupted `HAI_HOME`.** If the global
  state directory is corrupted, the server fails closed — by design, but
  recovery is manual (see `hai_recover`).

See [`docs/TOOL_CONTRACT.md`](docs/TOOL_CONTRACT.md) for the full
gate matrix and [`docs/OWNER_GATE.md`](docs/OWNER_GATE.md) for the
owner-gate contract.

---

## Tests

```bash
uv run pytest                       # full suite
uv run python scripts/stdio_smoke.py  # stdio round-trip smoke
```

The repo carries **139 green tests** as of the `0.1.0` slice. CI status
lives on the GitHub Actions page of the repository.

---

## Contributing

The control plane is small on purpose — additions go through a contract
slice:

1. Read [`docs/TOOL_CONTRACT.md`](docs/TOOL_CONTRACT.md).
2. Open an issue describing the tool / gate / state change.
3. Land it behind a `fail-closed` slice with tests + plan in `docs/plans/`.

Pull requests that change gates, schemas, or the mount table without a
matching plan will be asked to follow the same path.

---

## License

MIT — see [`LICENSE`](LICENSE). Copyright © 2026 Samuel Fleig.

---

## Acknowledgements

HAI-MCP is one piece of Samuel Fleig's HAI stack. It stands on:

- the [Model Context Protocol](https://modelcontextprotocol.io) specification,
- the [`mcp` Python SDK](https://pypi.org/project/mcp/) (`mcp>=1.9.0,<2`),
- and the legacy `~/.config/hai-agent-mcp` Hermes bridge that this server
  replaces for control-plane work. Coexistence is supported; the bridge is
  not required.

> *Canonical HAI site:* <https://www.human-agent-interface.com/> ·
> *About Samuel Fleig:* <https://www.human-agent-interface.com/samuel/>
