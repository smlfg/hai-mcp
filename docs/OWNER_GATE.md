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
   diff, abandon request).
2. **Deliver or prompt.** What happens next depends on `HAI_OWNER_CHANNEL`.
   `file` and `ntfy` issue an 8-character one-time code (`K7F3-M9QW`), valid
   for `HAI_OWNER_CODE_TTL` seconds (default 600), three attempts. The
   plaintext code goes to the owner — never to the client. `HAI_HOME` stores
   only a salted SHA-256 hash. `touchid` does not mint a code; the server
   process shows a Touch ID prompt for that fingerprint (see below).
3. **Relay or follow up.** For `file` / `ntfy`, the owner gives the code to
   the agent. For `touchid`, the agent calls the same tool again with the
   same arguments and no code.
4. **Apply.** The server checks the fingerprint, consumes the challenge
   (single use) and performs the action. Every step is audited. A code, when
   one exists, is never written to the audit stream or the client result.

Repeated asks for the same unchanged code-challenge return the same pending
challenge (no push-spam loop). A changed body or diff has a different
fingerprint and therefore needs a new code or a new Touch ID prompt: what
the owner approved is what gets written.

Gated actions: `hai_accept_next_step`, `hai_recontract`,
`hai_close_mission(closure="abandoned")`.

## Owner channels

| `HAI_OWNER_CHANNEL` | Delivery | Strength |
| --- | --- | --- |
| `file` (default) | Writes `<challenge_id>.txt` (mode 0600) into `HAI_OWNER_HOME` (default `~/.hai-owner`, dir mode 0700). Refuses to run if that directory is inside `HAI_HOME`. | Exactly as strong as the filesystem boundary. If the agent runs as the same user with unrestricted file access, it can read the code. Use this when the agent's file access is confined (sandbox, container, separate user) — or for tests. |
| `ntfy` | HTTP POST to `HAI_OWNER_NTFY_URL/HAI_OWNER_NTFY_TOPIC` (default server `https://ntfy.sh`; optional `HAI_OWNER_NTFY_TOKEN` as Bearer). The code lands on the owner's phone or desktop. | Off-machine: the agent cannot read it. The topic name is a secret — generate a random one, or self-host ntfy. Delivery failure keeps the gate closed (`owner_channel_unavailable`). |
| `touchid` | The server process spawns `touchid_prompt.swift` (or `HAI_OWNER_TOUCHID_HELPER`). macOS shows a Touch ID / device-owner prompt whose reason text names the action, project, a short excerpt or diff, and the fingerprint prefix. | The MCP client has no code and no argument that means "approved". Only this server process, after the OS accepts the biometric or device password for that exact prompt, records the approval. See the threat model below. |

Planned, not implemented: MCP elicitation (server asks the host UI directly,
bypassing the model).

## Touch ID

### Why this shape

The owner should approve with a finger, not by typing an 8-character code.
Two designs were possible: show the code only after Touch ID, or have the
server record the approval and let the agent's follow-up call apply it.
The second one is what ships. A code the owner must still read aloud does
not remove the code. A flag the client sends (`touchid_ok=true`) is the old
`owner_ack` bug with a new name.

So the prompt is not a tool and not something the client can imitate. On
`HAI_OWNER_CHANNEL=touchid` the **server process** runs a small Swift
program (`src/hai_mcp/touchid_prompt.swift`) that calls
`LAContext.evaluatePolicy(.deviceOwnerAuthentication)`. That policy is
Touch ID or Face ID when the Mac has it, and the device password otherwise
— a biometric or device-owner check, not a presence guess. The helper reads
the reason, the fingerprint, and a timeout on stdin. It writes one JSON
attestation to stdout and exits 0 only when the OS says authentication
succeeded. It is not given a code and it does not print one.
`touchIDAuthenticationAllowableReuseDuration` is 0, so a recent unlock is
not reused for this prompt.

The server accepts the attestation only when all of these are true:

- it spawned the helper itself (the path is the packaged script, or
  `HAI_OWNER_TOUCHID_HELPER` from the server environment — never a tool
  argument);
- the process exit status is 0 (a stdout that says `ok: true` with any
  other exit status does not approve);
- the echoed fingerprint and reason match the challenge the server just
  built.

It then stores the approval **in the server process**. The tool result is
`status: owner_approved` and does not apply the change yet. The agent's
next call with the same arguments applies that exact change and consumes
the approval. A changed body or diff misses the fingerprint, so it needs a
new prompt. The approval expires after `HAI_OWNER_CODE_TTL` (the same TTL
as a code). A challenge file marked `approved` is not a credential: another
process, or a rewritten file under `HAI_HOME`, cannot redeem it.

The first `swift touchid_prompt.swift` run compiles the helper, so the
dialog can take a few seconds to appear. Compile it once and point
`HAI_OWNER_TOUCHID_HELPER` at the binary if you want the prompt immediately.
The dialog's app name follows the helper process (`swift` or `hai-touchid`);
the reason text is the decision.

### Threat model

Held:

- The MCP client alone cannot pass the gate. `owner_ack` and `owner_code`
  do not approve a Touch ID challenge.
- Cancel, authentication failure, and timeout leave the gate closed
  (`owner_denied`). They do not fall back to a code.
- Not macOS, a missing helper, a helper path inside `HAI_HOME`, or a helper
  that will not start leave the gate closed (`owner_channel_unavailable`),
  unless `HAI_OWNER_TOUCHID_FALLBACK=file` is set. That fallback is the
  file channel, and only for those unavailable cases.
- A helper attestation for a different fingerprint or a different reason
  does not approve.
- One successful prompt authorizes one apply of that fingerprint. Replay,
  including copying an `approved` record into a new process, does not pass.
- No code is created, so none appears in the audit stream or the client
  result. Audit events are the same family as the other channels
  (`owner_challenge_issued`, `owner_gate_passed`, `owner_challenge_cancelled`,
  `owner_challenge_expired`) plus the channel name. The helper's method
  string is kept only if it is a short token (`touchid`, `faceid`,
  `device_owner`); anything else is stored as `device_owner`.

Not held:

- Someone who can rewrite the hai-mcp install or replace the helper binary
  can skip the prompt. That is the same user replacing the control plane,
  not the agent calling tools.
- Someone who can write `HAI_HOME` still cannot redeem an approval in this
  process, because the credential lives in process memory. They can of
  course break the server in other ways if they can write its code.
- Touch ID does not prove which human touched the sensor beyond "someone
  who can unlock this Mac". It is a device-owner check, not an account
  directory.
- The prompt blocks the owner-gate lock until it finishes, so a second
  gated call waits. Run one `hai-mcp` process. A second process does not
  share the approval and will prompt again.

### Fail closed

| Situation | Result | File fallback |
| --- | --- | --- |
| Not macOS | `owner_channel_unavailable` / `not_macos` | only if `HAI_OWNER_TOUCHID_FALLBACK=file` |
| Helper missing, `swift` missing, helper not executable, helper inside `HAI_HOME` | `owner_channel_unavailable` / `helper_missing` or `helper_untrusted` | only if fallback is `file` |
| Biometry unavailable or locked out | `owner_channel_unavailable` / `biometry_unavailable` | only if fallback is `file` |
| Owner cancels or authentication fails | `owner_denied` / `user_cancel` or `auth_failed` | never |
| Prompt times out | `owner_denied` / `timeout` | never |
| Attestation fingerprint or reason does not match | `owner_denied` / `fingerprint_mismatch` or `reason_mismatch` | never |

`HAI_OWNER_TOUCHID_FALLBACK` accepts only `file`. Any other value is ignored
and reported in `hai_health.config_warnings`. User cancel and timeout never
use it: those are decisions, not a broken channel.

### Try it on the owner's Mac

CI mocks the helper. A real sensor check is:

```bash
# optional, so the dialog appears without a compile pause
swiftc -O -o "$HOME/.local/bin/hai-touchid" src/hai_mcp/touchid_prompt.swift
export HAI_OWNER_TOUCHID_HELPER="$HOME/.local/bin/hai-touchid"

uv run python scripts/manual_touchid.py prompt   # one dialog; exit 0 only on success
uv run python scripts/manual_touchid.py gate     # temp HAI_HOME; prompt, then follow-up
```

`gate` does not read or write `~/.hai`. Cancel the dialog and the script
exits non-zero with the canonical file unwritten.

To run the server this way, set the env vars on the **server** process
(the MCP client's `env` block), then restart it:

```bash
HAI_OWNER_CHANNEL=touchid
HAI_OWNER_TOUCHID_TIMEOUT=60
# HAI_OWNER_TOUCHID_HELPER=$HOME/.local/bin/hai-touchid
# HAI_OWNER_TOUCHID_FALLBACK=file    # optional; unavailable only
```

The first gated call blocks on the prompt and returns `owner_approved`.
The next call with the same arguments applies the change. Sending
`owner_code` does not grant access and is not required.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `HAI_OWNER_GATE` | `nonce` | `nonce` or `ack_legacy`. Unknown values fall back to `nonce` and are reported in `hai_health.config_warnings`. |
| `HAI_OWNER_CHANNEL` | `file` | `file`, `ntfy`, or `touchid`. Unknown values fall back to `file`. |
| `HAI_OWNER_HOME` | `~/.hai-owner` | Owner directory for the `file` channel (and for Touch ID's optional file fallback). Must not be inside `HAI_HOME`. |
| `HAI_OWNER_NTFY_URL` | `https://ntfy.sh` | ntfy server. |
| `HAI_OWNER_NTFY_TOPIC` | — | ntfy topic (required for `ntfy`). |
| `HAI_OWNER_NTFY_TOKEN` | — | Optional bearer token. |
| `HAI_OWNER_CODE_TTL` | `600` | Code lifetime, and Touch ID approval lifetime, in seconds (minimum 30). |
| `HAI_OWNER_TOUCHID_TIMEOUT` | `60` | How long the Touch ID prompt waits, in seconds (minimum 5, maximum 180). |
| `HAI_OWNER_TOUCHID_HELPER` | packaged `touchid_prompt.swift` | Executable, or a `.swift` file run with `swift`. Refused if the path is inside `HAI_HOME`. |
| `HAI_OWNER_TOUCHID_FALLBACK` | unset | `file` to deliver a one-time code when Touch ID cannot run. Unset or any other value: no fallback. Ignored unless the channel is `touchid`. |

`hai_health` reports the active mode and channel (and, for `touchid`, the
timeout, fallback, helper path, and whether this process is on macOS).
`hai_status` reports the number of live challenges, including a Touch ID
approval that has not been applied yet. `hai_health` never shows the prompt.

## Legacy mode `ack_legacy`

Keeps the pre-v0.1.1 behaviour (`owner_ack=true` + `reason`). It exists for
compatibility and for environments without any owner channel. `hai_health`
flags it as an honor system. Do not call a deployment "owner-gated" while it
runs in this mode.

## Result shapes

Pending code (`file`, `ntfy`, or Touch ID's file fallback):

```json
{"ok": false, "error": "owner_gate_required", "gate": "nonce",
 "status": "pending_owner_code", "challenge_id": "C-…", "expires_at": "…",
 "attempts_remaining": 3, "channel": "file", "fingerprint": "sha256:…"}
```

Touch ID approved, not yet applied:

```json
{"ok": false, "error": "owner_gate_required", "gate": "nonce",
 "status": "owner_approved", "challenge_id": "C-…", "expires_at": "…",
 "channel": "touchid", "fingerprint": "sha256:…"}
```

The follow-up call returns the normal success result plus
`"owner_gate": {"gate": "nonce", "channel": "touchid", "challenge_id": "C-…", "audit_event_id": "A-…"}`.

Rejected code: `detail: "invalid_owner_code"` with `attempts_remaining`;
after the third miss `status: "cancelled"`. No live challenge for the
presented change (used, expired, cancelled, or the change was swapped):
`detail: "no_pending_challenge"`. Malformed input on a code channel:
`detail: "malformed_owner_code"` (does not count as an attempt).

Touch ID did not authenticate: `error: "owner_denied"` with `detail`
`user_cancel`, `timeout`, `auth_failed`, `fingerprint_mismatch`, or
`reason_mismatch`. The channel could not run: `error:
"owner_channel_unavailable"` with `detail` `not_macos`, `helper_missing`,
`helper_untrusted`, or `biometry_unavailable`.

Passed (code channels): the normal success result plus
`"owner_gate": {"gate": "nonce", "challenge_id": "C-…", "audit_event_id": "A-…"}`.

## What this gate measures (and why that matters)

Every challenge is an audited decision with a timestamp, a fingerprint and an
outcome: relayed, authenticated, ignored until expiry, or rejected. That
yields the numbers HAI's thesis needs — approval latency, override/deny
rate, and later the calibration of owner predictions — instead of a click
count.
