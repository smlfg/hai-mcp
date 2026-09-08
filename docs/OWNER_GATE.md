# OWNER GATE — the owner is a separate principal

## Why

Before v0.1.1 an owner-gated action was passed by sending `owner_ack=true`.
That value comes from the MCP client — the agent — i.e. from the party the
gate exists to constrain. It was fail-closed against type coercion (`1` was
rejected) and fail-open against the agent (`true` was accepted). A drifting,
hallucinating or prompt-injected agent walked through.

HAI's thesis is that the human is an irreducible decision layer. A gate the
agent can open by itself is not a decision layer; it is a form.

## Mechanism (mode `nonce`, the default)

1. **Ask.** The agent calls the gated tool without `owner_code`. The server
   computes a *fingerprint* of the exact change (proposal body, recontract
   diff, abandon request) and issues a challenge: an 8-character one-time
   code (`K7F3-M9QW`), valid for `HAI_OWNER_CODE_TTL` seconds (default 600),
   three attempts.
2. **Deliver.** The plaintext code goes to the owner through the *owner
   channel* — never to the client. `HAI_HOME` stores only a salted SHA-256
   hash. The owner message states what is being approved (action, project,
   excerpt or diff), so the owner decides on the change, not on a captcha.
3. **Relay.** The owner gives the code to the agent (voice, chat, whatever).
4. **Present.** The agent calls the tool again with `owner_code`. The server
   verifies against the hash, checks the fingerprint, consumes the challenge
   (single use) and performs the action. Every step is audited; the code is
   never written to the audit stream.

Repeated asks for the same change return the same pending challenge (no
push-spam loop). A changed body or diff has a different fingerprint and
therefore needs a new code: what the owner approved is what gets written.

Gated actions: `hai_accept_next_step`, `hai_recontract`,
`hai_close_mission(closure="abandoned")`.

## Owner channels

| `HAI_OWNER_CHANNEL` | Delivery | Strength |
| --- | --- | --- |
| `file` (default) | Writes `<challenge_id>.txt` (mode 0600) into `HAI_OWNER_HOME` (default `~/.hai-owner`, dir mode 0700). Refuses to run if that directory is inside `HAI_HOME`. | Exactly as strong as the filesystem boundary. If the agent runs as the same user with unrestricted file access, it can read the code. Use this when the agent's file access is confined (sandbox, container, separate user) — or for tests. |
| `ntfy` | HTTP POST to `HAI_OWNER_NTFY_URL/HAI_OWNER_NTFY_TOPIC` (default server `https://ntfy.sh`; optional `HAI_OWNER_NTFY_TOKEN` as Bearer). The code lands on the owner's phone or desktop. | Off-machine: the agent cannot read it. The topic name is a secret — generate a random one, or self-host ntfy. Delivery failure keeps the gate closed (`owner_channel_unavailable`). |

Planned, not implemented: MCP elicitation (server asks the host UI directly,
bypassing the model) and a Touch ID / `LAContext` presence check on macOS.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `HAI_OWNER_GATE` | `nonce` | `nonce` or `ack_legacy`. Unknown values fall back to `nonce` and are reported in `hai_health.config_warnings`. |
| `HAI_OWNER_CHANNEL` | `file` | `file` or `ntfy`. |
| `HAI_OWNER_HOME` | `~/.hai-owner` | Owner directory for the `file` channel. Must not be inside `HAI_HOME`. |
| `HAI_OWNER_NTFY_URL` | `https://ntfy.sh` | ntfy server. |
| `HAI_OWNER_NTFY_TOPIC` | — | ntfy topic (required for `ntfy`). |
| `HAI_OWNER_NTFY_TOKEN` | — | Optional bearer token. |
| `HAI_OWNER_CODE_TTL` | `600` | Code lifetime in seconds (minimum 30). |

`hai_health` reports the active mode and channel; `hai_status` reports the
number of live challenges.

## Legacy mode `ack_legacy`

Keeps the pre-v0.1.1 behaviour (`owner_ack=true` + `reason`). It exists for
compatibility and for environments without any owner channel. `hai_health`
flags it as an honor system. Do not call a deployment "owner-gated" while it
runs in this mode.

## Result shapes

Pending (first call, or repeat before the owner answered):

```json
{"ok": false, "error": "owner_gate_required", "gate": "nonce",
 "status": "pending_owner_code", "challenge_id": "C-…", "expires_at": "…",
 "attempts_remaining": 3, "channel": "file", "fingerprint": "sha256:…"}
```

Rejected code: `detail: "invalid_owner_code"` with `attempts_remaining`;
after the third miss `status: "cancelled"`. No live challenge for the
presented change (used, expired, cancelled, or the change was swapped):
`detail: "no_pending_challenge"`. Malformed input: `detail:
"malformed_owner_code"` (does not count as an attempt).

Passed: the normal success result plus
`"owner_gate": {"gate": "nonce", "challenge_id": "C-…", "audit_event_id": "A-…"}`.

## What this gate measures (and why that matters)

Every challenge is an audited decision with a timestamp, a fingerprint and an
outcome: relayed, ignored until expiry, or rejected. That yields the numbers
HAI's thesis needs — approval latency, override/deny rate, and later the
calibration of owner predictions — instead of a click count.
