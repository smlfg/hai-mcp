# Wireframe Packet: HAI-MCP Mission Contract Lifecycle

## 1. Owner Summary

HAI-MCP soll agentische Arbeit an einen aktiven, versionierten und pruefbaren Missionsvertrag binden.
`7coreFunctions` definiert den kanonischen Missionslebenszyklus; `7Functions` ergaenzt Intake, Verdichtung, vereinfachten Start, Proof und Tagesabschluss.
Beide Siebener-Sets bleiben als sichtbare MCP-Funktionen erhalten, teilen aber genau einen Zustand, eine Vertragsgeschichte und einen Audit-Kanal.
Der Server bleibt deterministisch und modellagnostisch: Cursor Composer 2.5 formuliert Inhalte, HAI-MCP erzwingt Kardinalitaet, Rechte, Leases, Pfade und Belege.
Produktionscode darf erst nach Owner-Review dieses Zustands- und Gate-Modells beginnen.

## 2. Assumptions

- `SAFE DEFAULT` — `7coreFunctions` ist die kanonische State Machine. Die zusaetzlichen sieben Funktionen sind schmale Eingangs- oder Convenience-Flaechen auf denselben Stores.
- `SAFE DEFAULT` — Alle zusaetzlichen Toolnamen erhalten den bestehenden Namensraum: `hai_intake`, `hai_distill`, `hai_mission_start`, `hai_park`, `hai_drift_check`, `hai_proof`, `hai_stop`.
- `SAFE DEFAULT` — `hai_mission_start` delegiert an `hai_open_mission`; `hai_drift_check` an `hai_check_activity`; `hai_proof` an `hai_close_mission`. Die Wrapper erzeugen keine zweite Wahrheit.
- `SAFE DEFAULT` — Weil im Server keine LLM-Aufrufe erlaubt sind, liefert der Client bei `hai_distill` genau eine vorgeschlagene Entscheidung, genau einen naechsten Schritt und eine Parkliste. Der Server erzeugt diese Semantik nicht, sondern validiert und speichert sie.
- `SAFE DEFAULT` — Eine aktive Mission und standardmaessig eine aktive Session-Lease. Explizite Parallelitaet darf nur der Missionsvertrag erlauben.
- `SAFE DEFAULT` — Legacy-Tools wie `hai_set_focus` bleiben kompatibel, verleihen aber kein Missions- oder Session-Arbeitsrecht.
- `SAFE DEFAULT` — Unverifizierbare oder semantisch nicht sicher klassifizierbare Aktivitaet ergibt `unclear + pause`, nie kreative Fortsetzung.
- `SAFE DEFAULT` — Belege werden V1 fail-closed als existente, projektgebundene Dateien oder bereits gespeicherte Audit-/Trace-Artefakte geprueft. Eine freie Fertigmeldung zaehlt nicht.
- `SAFE DEFAULT` — Jede Mutation schreibt einen neuen, unveraenderlichen Audit-Eintrag. Vertragsanpassungen erzeugen neue Versionsdateien; alte Dateien werden nicht ueberschrieben.
- `NEEDS OWNER DECISION` — Die vereinfachte Signatur `mission_start(problem, artifact, time_limit)` hat keinen Owner-Parameter. Default im Packet: Der Wrapper verlangt zusaetzlich `owner`, statt eine Identitaet zu erfinden.
- `NEEDS OWNER DECISION` — `hai_stop` beendet den Arbeitstag und invalidiert aktive Session-Leases, schliesst aber keine Mission ohne Proof. Die Mission bleibt pausiert und kann spaeter neu autorisiert werden.
- `RISKY ASSUMPTION` — Ein deterministischer Drift-Check kann semantische Zielabweichung nur ueber Criterion-IDs, erlaubte Pfade, Non-Goals, Capabilities und Trace-Aktionen erkennen. Alles darueber hinaus muss `unclear` bleiben.

## 3. Roles

| Role | Goal | Permissions | Risks |
|---|---|---|---|
| Owner | Mission bewusst setzen, aendern oder abbrechen | Open, Recontract mit Ack, Break-glass, Abandon, Tagesabschluss | Unbemerkter Scope-Wechsel, reflexartiges Override |
| Cursor Composer 2.5 / Client Model | Einen begrenzten Coding-Beitrag formulieren und ausfuehren | Intake verdichten, Session beantragen, Vertrag lesen, Arbeit/Belege melden | Modell erfindet Scope oder behauptet Evidenz |
| Authorized Session Agent | Genau den genehmigten Beitrag innerhalb der Lease leisten | Vertrag lesen; erlaubte Dateien/Faehigkeiten; Close/Proof anfragen | Lease veraltet, Parallelziel, sensitive Side Effects |
| Sidecar Observer / Automation | Deklarierte Mission gegen geplante oder beobachtete Aktivitaet pruefen | `check_activity` / `drift_check`; Audit schreiben | Wird selbst zum beratenden Scope-Erzeuger |
| HAI-MCP | Vertrag und Rechte deterministisch erzwingen | Validieren, speichern, hashen, klassifizieren, Leases invalidieren | Zwei Wahrheiten, partielle Writes, Pfad-Escape |
| Evidence Verifier | Belege pro Done-Kriterium pruefen | Existenz, Root-Confinement, Hash/Metadaten kontrollieren | Fertigmeldung wird mit echtem Beleg verwechselt |

## 4. View Map

| View | Purpose | Primary user | Main decision/action | States needed |
|---|---|---|---|---|
| Intake Valve | Gedanken verlustfrei erfassen, auf 1/1 verdichten und Rest parken | Owner + Client Model | Welche eine Entscheidung und welcher eine Schritt duerfen weiter? | empty, captured, needs_distillation, distilled, invalid, parked, day_closed |
| Mission Contract | Mission oeffnen, Session autorisieren, Vertrag erneut injizieren und Aktivitaet pruefen | Owner + Session Agent + Observer | Hat diese Session fuer diesen Schritt Arbeitsrecht? | review_required, active, lease_granted, lease_denied, stale, in_scope, blocker, park_candidate, drift, unclear, paused |
| Proof / Recontract / Stop | Scope sichtbar aendern, Proof pruefen, Mission oder Tag sauber beenden | Owner + Evidence Verifier | Recontract, complete, abandon oder nur fuer heute stoppen? | pending_owner_confirmation, break_glass, incomplete, completed, abandoned, day_stopped, audit_only |

## 5. State Map

| State | Visible signifier | Allowed actions | Blocked actions | Data/evidence needed |
|---|---|---|---|---|
| default / no mission | `active_mission: null` | intake, distill, open mission | authorize, work, proof | none |
| empty intake | `intake_not_found` | create intake | distill unknown ID | raw intake |
| loading / call in flight | client-owned spinner; no committed state promised | cancel/wait client-side | assume success | final MCP result or later audit lookup |
| captured | `intake_id`, `actionable: false` | distill later | start agent from raw intake | immutable raw payload |
| needs review | `status: review_required`, issue list | Owner revises exact fields | activate mission | artifact + finite Done criteria + owner |
| active mission | `mission_id`, version, hash, status `active` | authorize one session, park, check, recontract, proof | open second mission | canonical contract |
| lease granted | `session_id`, expiry, contract version/hash | get contract, bounded work, activity checks | unrelated goal, expired actions | contribution + criterion IDs + capabilities |
| permission denied | `denied`, exact clause/code | revise request or ask Owner | start work | current mission/version/session limits |
| stale lease | `contract_version_mismatch` or `lease_expired` | pause, request new authorization | continue work | current contract version + clock |
| read-only observer | `observer_mode: true` | classify and append audit | alter contract, suggest architecture | activity, affected paths, trace events |
| in scope | `classification: in_scope`, `required_action: continue` | continue authorized step | expand scope | referenced Done criterion + allowed path/capability |
| blocker | `classification: blocker`, `required_action: pause` | report blocker, request blocker recontract | optional improvement work | criterion blocked + causal evidence |
| park candidate | `classification: park_candidate`, `required_action: park` | park item | elaborate or execute it now | origin session/event + out-of-scope reason |
| drift | `classification: drift`, `required_action: stop|request_recontract` | stop or Owner recontract | continue current action | violated clause/path/capability/trace |
| unclear | `classification: unclear`, `required_action: pause` | Owner review | creative continuation | missing criterion/path/trace context |
| missing provider sync | `provider_required: false` | continue with local deterministic state | wait for model/provider inside server | none; HAI-MCP has no provider dependency |
| error / partial failure | `ok: false`, stable error code; no new active right | retry safe read or inspect audit | assume mutation committed | atomic state/audit evidence |
| pending owner confirmation | exact contract diff + `owner_ack_required` | approve or deny | apply hidden change | old/new values, reason, mode |
| destructive confirmation | `break_glass: true` or `abandon` warning | Owner Ack + reason | silent override | explicit confirmation and audit entry |
| incomplete proof | missing/invalid evidence listed per criterion | attach evidence, keep mission active | close as completed | verified evidence map |
| completed | criteria + evidence hashes + close audit | read history, later review parked items | retain session rights, auto-start next mission | complete evidence set |
| abandoned | reason + Owner Ack + close audit | read history | retain session rights, auto-start next mission | explicit abandonment reason |
| paused / day stopped | `day_closed`, leases invalidated, `next_mission: null` | read/audit; resume on later authorization | auto-plan tomorrow, surface park as work | stop record and three agency answers |
| audit/history | immutable event IDs and hashes | read/filter | edit/delete historical entries | append-only event files |

## 6. Low-Fidelity Mockups

### View A — Intake Valve

```text
┌──────────────────────────────────────────────────────────────────┐
│ HAI INTAKE VALVE                         Mission: none            │
├──────────────────────────────────────────────────────────────────┤
│ Raw thought                                                     │
│ ┌──────────────────────────────────────────────────────────────┐ │
│ │ "Maybe rebuild HAI, move to Mac, deploy router, ..."        │ │
│ └──────────────────────────────────────────────────────────────┘ │
│ [Capture only]  -> intake_id=I-...  actionable=false             │
│                                                                  │
│ Client-proposed distillation                                     │
│ Decision (exactly 1): [_______________________________________]  │
│ Next step (exactly 1): [______________________________________]  │
│ Park list:  [idea A] [idea B] [idea C]                           │
│ [Validate 1/1 + park]                                            │
│                                                                  │
│ State note: HAI-MCP does not generate the distillation.          │
│ It refuses 0 or >1 decisions/next steps.                         │
└──────────────────────────────────────────────────────────────────┘
```

### View B — Mission Contract and Session Lease

```text
┌──────────────────────────────────────────────────────────────────┐
│ ACTIVE MISSION M-...  v3  hash: 91ab...      [Audit history]     │
├──────────────────────────────────────────────────────────────────┤
│ Objective: one observable result                                 │
│ Artifact:  project/path/output                                   │
│ Done: [dc-1] ...   [dc-2] ...                                   │
│ Non-goals: ...        Allowed roots: ...                         │
│                                                                  │
│ SESSION REQUEST                                                  │
│ Agent/role: Composer 2.5 / builder                               │
│ Contribution -> [dc-1]     Expected result: ...                  │
│ Duration: 45 min            Capabilities: read, write, test       │
│ [Authorize]  [Deny]                                             │
│                                                                  │
│ Lease S-... | v3 | expires 01:40 | ACTIVE                        │
│ [Get canonical contract] [Check planned activity]                │
│                                                                  │
│ DRIFT RESULT: unclear -> [PAUSE]                                 │
│ Clause: missing Done-criterion reference                         │
│ No alternative architecture suggested.                           │
└──────────────────────────────────────────────────────────────────┘
```

### View C — Recontract, Proof, and Stop

```text
┌──────────────────────────────────────────────────────────────────┐
│ MISSION M-... v3                    Status: ACTIVE               │
├──────────────────────────────────────────────────────────────────┤
│ CONTRACT DIFF                                                    │
│ - remove: allowed path tests/                                    │
│ + add:    allowed path src/hai_mcp/                              │
│ Reason: blocker ...       Mode: normal | blocker | BREAK GLASS   │
│ [Owner approve new v4] [Deny]                                    │
│ Note: approval invalidates every v3 lease.                        │
│                                                                  │
│ PROOF                                                            │
│ dc-1 [verified file + sha256]                                    │
│ dc-2 [MISSING]                                                   │
│ [Complete mission] DISABLED — one criterion lacks evidence       │
│ [Abandon...] OWNER GATE                                          │
│                                                                  │
│ END DAY                                                          │
│ Loop closed? yes  Clearer? yes/no/unclear  Agency gained? ...    │
│ [Stop day] -> leases revoked, mission paused, no next plan        │
└──────────────────────────────────────────────────────────────────┘
```

## 7. Affordances & Signifiers

| UI element | What user thinks it does | Actual effect | Enabled when | Disabled/gated when | Risk |
| ---------- | ------------------------ | ------------- | ------------ | ------------------- | ---- |
| `hai_intake` | Thought is safely captured | Writes immutable raw intake; returns ID only | non-empty raw text | blank input or day policy rejects | Raw thought accidentally treated as work |
| `hai_distill` | Select one decision and one next step | Validates 1/1 cardinality; parks all remaining items | known intake + exact proposal | missing/multiple decision or step | Client model smuggles a plan bundle into one string |
| `hai_open_mission` | Creates binding contract | Validates and activates version 1 or returns review_required | no active mission; finite contract | second mission or invalid contract | Vague objective gains work rights |
| `hai_mission_start` | Fast start from problem/artifact/time | Builds the same canonical mission contract via wrapper | owner supplied; no active mission | missing Owner or unverifiable artifact | Wrapper bypasses core checks |
| `hai_authorize_session` | Lets one agent work | Issues bounded lease tied to mission version and criteria | active contract; capacity; matching criteria | stale version, parallel goal, missing access proof | Lease interpreted as broad autonomy |
| `hai_get_contract` | Refreshes the true goal | Returns exact canonical contract, hash and lease remainder | current live lease | expired/stale lease | Chat summary replaces contract |
| `hai_check_activity` | Checks a concrete step | Deterministic classification + audit only | valid session and structured observations | none; uncertainty returns pause | Observer becomes creative adviser |
| `hai_drift_check` | Lightweight automated mismatch check | Calls same classifier with reduced input and logs mismatch | active mission/session resolvable | ambiguous current session | Agent invokes its own gate selectively |
| `hai_park` | Saves a standalone thought | Writes non-actionable inbox item | non-empty text | never grants execution | Inbox becomes hidden backlog |
| `hai_park_item` | Removes mission-adjacent idea from work | Stores origin, trigger, mission link and rationale | active/known origin | missing out-of-scope reason | Item keeps consuming attention |
| `hai_recontract` | Changes scope visibly | Shows diff; on Ack writes new version and revokes leases | active mission + current version | no Owner Ack; stale version | Break-glass becomes default workflow |
| `hai_proof` | Tests whether mission can close | Delegates to canonical close validator | current mission + evidence | evidence missing/invalid | Evidence text is accepted as proof |
| `hai_close_mission` | Completes or abandons mission | Verifies each criterion or records Owner-approved abandonment; revokes leases | current version | incomplete proof or missing abandon Ack | False Done or silent discard |
| `hai_stop` | Ends the workday | Writes terminal record and revokes leases; no next mission | explicit day and answers | never auto-completes mission | Stop accidentally destroys mission state |
| status chips | Explain current authority | Reflect stored state only | always readable | never mutate | Client displays stale cached status |
| proof links | Show why Done is valid | Resolve confined evidence and hashes | evidence exists under allowed root | path escape/missing evidence | Symlink/path escape |

## 8. Data & Integration Surface

| Source | Data used | Freshness requirement | Failure state | Fallback UI |
| ------ | --------- | --------------------- | ------------- | ----------- |
| `7coreFunctions` | canonical lifecycle and invariants | static source for this version | missing/changed after review | stop implementation and re-review diff |
| `7Functions` | intake/reduction/proof/stop behavior | static source for this version | ambiguous overlap | preserve both public concepts on one engine |
| Existing `GOAL.md` / Tool Contract | no LLM, path roots, current tools and gates | current repo state | contract conflict | fail closed; Owner review |
| Cursor Composer 2.5 | semantic proposal and coding execution | current session + fresh contract injection | unavailable/model switched | any MCP-capable client may replace it |
| `HAI_HOME` | missions, sessions, intake, parking, audit, stop records | authoritative on every call | corrupt/missing record | structured error; no silent defaults for rights |
| Project filesystem | artifacts and Done evidence | verify at proof time | missing, stale, outside root, symlink escape | incomplete proof / pause |
| Trace/tool events | actual activity and sensitive actions | same session and contract version | absent or stale | `unclear + pause` |
| External provider sync | none required inside server | not applicable | provider unavailable | local contract tools remain usable |

## 9. Permission / Risk Gate

| Action | Risk level | Who may trigger it | Required confirmation | Audit needed |
| ------ | ---------- | ------------------ | --------------------- | ------------ |
| Capture intake | low | Owner/client | none | yes |
| Distill intake | medium | Client model on Owner input | exact 1/1 validation | yes |
| Open mission | medium | Owner/client acting for Owner | explicit owner field + valid contract | yes |
| Authorize ordinary session | medium | Owner/control client | current contract version | yes |
| Authorize commit/push/deploy/secrets/delete | high | Owner only | existing Owner gate; explicit reason | yes |
| Check activity | low/read-only to contract | Observer/client | none; valid lease context | mismatch audit only |
| Park item | low | any valid caller | out-of-scope rationale for mission-linked item | yes |
| Normal/blocker recontract | high | Owner only | `owner_ack=true` + reason + visible diff | yes + preserved old version |
| Break-glass recontract | critical | Owner only | Owner Ack + reason + explicit break-glass marker | prominent immutable audit |
| Close completed | high | Authorized session may request | verified evidence for every criterion | yes |
| Abandon mission | high | Owner only | Owner Ack + reason | yes |
| Stop day | medium | Owner | explicit terminal call; no next plan | yes |
| Delete/alter audit history | forbidden | nobody via MCP | unavailable | attempted action logged if surfaced |

## 10. Implementation Handoff

### Public tool mapping

| Source request | Public MCP tool | Canonical implementation |
|---|---|---|
| `open_mission` | `hai_open_mission` | Mission store + contract validator |
| `authorize_session` | `hai_authorize_session` | Lease store + capability/criterion gate |
| `get_contract` | `hai_get_contract` | Exact version/hash reader |
| `check_activity` | `hai_check_activity` | Deterministic classifier + audit |
| `park_item` | `hai_park_item` | Mission-linked parking store |
| `recontract` | `hai_recontract` | Versioned diff + lease invalidation |
| `close_mission` | `hai_close_mission` | Evidence gate + terminal mission state |
| `intake` | `hai_intake` | Raw immutable intake store |
| `verdichten` | `hai_distill` | Client proposal + 1/1/park enforcement |
| `mission_start` | `hai_mission_start` | Thin wrapper over `open_mission` |
| `park` | `hai_park` | Existing general inbox, hardened/audited |
| `drift_check` | `hai_drift_check` | Thin wrapper over `check_activity` |
| `proof` | `hai_proof` | Thin wrapper over `close_mission` |
| `stop` | `hai_stop` | Day terminal + lease invalidation |

| Slice | Files likely affected | Acceptance criteria | Test/check |
| ----- | --------------------- | ------------------- | ---------- |
| M1 — State and audit foundation | `src/hai_mcp/state.py`, `paths.py`, possibly new focused modules, unit tests | single active mission; immutable versions/audit; atomic writes; confined paths; no regression to existing gates | focused storage/path/contract tests |
| M2 — Core seven lifecycle | state modules, `server.py`, core lifecycle tests | all seven core tools callable; stale leases pause; recontract revokes; proof fail-closed | state-transition table tests |
| M3 — Additional seven surface | state modules, `server.py`, additional-flow tests | all seven additional public names present; 1/1 cardinality; aliases share canonical IDs/state; stop makes no next mission | intake-to-stop end-to-end unit test |
| M4 — Client and protocol proof | `.cursor/mcp.json`, README/Tool Contract, stdio integration tests | Cursor config discovers server; MCP initialize/list/call works with temp `HAI_HOME`; no live state mutation in tests | `agent mcp list-tools hai`; `uv run pytest -q` |
| M5 — Requirement audit | no new scope unless defect found | every named input/output/rule mapped to code + test or explicitly marked unsupported | checklist against both source files |

Each slice is sequential. There is never more than one implementation WIP slice.

## 11. Parked Scope

- LLM-based semantic drift classification inside the MCP server.
- Automatic OCR, PDF extraction or provider calls inside the server.
- Dynamic macOS plugin host or full HAI plugin catalog.
- Automatic agent launch from intake or parked items.
- Automatic next-mission selection after Proof or Stop.
- Multi-user authentication, remote network service and enterprise RBAC.
- Deleting, compacting or rewriting audit history.
- Polished UI; this packet describes an MCP/workflow surface only.
- A separate `hai_log_incident` tool unless the audit/check-activity records prove insufficient during dogfood.

## 12. Owner Review Gate

**Owner decision needed:** Should implementation proceed from this wireframe packet, or should one view/state be changed first?

Options:
A. Approve wireframe packet for implementation planning.
B. Revise specific view/state before implementation.
C. Stop implementation and keep this as product exploration only.
