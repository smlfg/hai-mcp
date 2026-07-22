# Build-Plan: Fail-closed Input-Härtung (Slice 1)

> Erstellt 2026-07-22 nachts. Planner: Codex 5.6 (read-only). Orchestrator: Claude.
> Coder-Ausführung: OpenCode (Cursor/Composer war headless nicht ansteuerbar).
> Status: Work-Order für die Härtung der core-seven Invarianten.

## Ausgangslage

Teilweise Umsetzung im Repo vorhanden (`ids.py`, Recontract-Konstanten, erste adversariale Tests),
aber noch nicht ausreichend fail-closed. Offene Lücken:

- `intake_id`/Präfix `I-` fehlt.
- Ungültige IDs werden intern teils als `None` behandelt statt als strukturierter Validierungsfehler.
- Unterverzeichnisse (`contracts/`, `sessions/`) können via Symlink aus `HAI_HOME` herauszeigen.
- `get_session()` folgt potenziell einem `M-*`-Symlink.
- Relative Datei-/Evidence-Pfade werden gegen Prozess-CWD statt `constraints.project_path` interpretiert.
- Ohne `project_path` werden Datei-/Evidence-Pfade derzeit erlaubt (permissiver Default).
- Evidence wird vor der Root-Prüfung mit `exists()` untersucht.
- `recontract(owner_ack=False)` liefert weiterhin `ok: true`.
- Truthy, aber nicht-boolesches `owner_ack` würde akzeptiert.
- Verschachtelte Vertragsfelder/Pfadlisten werden teils normalisiert statt strikt abgelehnt.
- `state.py` nutzt bei mehreren Lesewegen das ungesicherte `artifact_dir()`.

## Sichere Defaults

- ID-Format exakt: `M-YYYYMMDDTHHMMSS-xxxxxxxx` (ebenso `S-`, `I-`), `xxxxxxxx` = 8 kleine Hex. Kein `str()`, kein `strip()`; Nicht-Strings und Whitespace ablehnen.
- Fehlercodes: ungültige ID/Schema → `invalid_args`; Root-/Symlink-Ausbruch → `path_outside_root`; fehlendes/nicht exakt `True` `owner_ack` → `owner_gate_required`.
- Roots: Missions/Session/Audit/Parking/Lock → `Config.hai_home`; Activity/Evidence → kanonisches `contract.constraints.project_path`; Projektartefakte → `require_project_path()`.
- Relative Activity-/Evidence-Pfade relativ zum Projekt-Root, nie zum Server-CWD.
- Pfad übergeben aber kein Projekt-Root → ablehnen, kein permissiver Default.
- `recontract` Top-Level nur: `objective, artifact, done_criteria, non_goals, constraints`. `mission_id`/Version/Hash/Status/Recontract-Meta bleiben systemverwaltet.
- `owner_ack` nur bei `is True`. Preview weiterhin `status: pending_owner_confirmation` + `diff`, aber `ok: false`, `error: owner_gate_required`.

## Betroffene Module

- `ids.py`: `I`-Präfix + `validate_intake_id`; strikte Typ-/Fullmatch-Prüfung; strukturierte interne ID-Exception statt bloßem `(False, message)`.
- `paths.py`: zentrale Root-Auflösung `resolve_under_root(root, candidate, relative_to_root=True)`; Null-Bytes vor FS-Aufrufen ablehnen; relative Inputs unter Root hängen; Symlink-Ziel + finalen Leaf prüfen.
- `mission.py`: ID-/Root-Prüfung in alle internen Speicherpfade; ungültige IDs nicht als `None` tarnen; gespeicherte IDs auf Format + Zugehörigkeit prüfen; `_path_allowed`/`_verify_evidence_path` root-zuerst; recontract-Whitelist + vollständige Kandidaten-Revalidierung + immutable `mission_id` + exaktes Owner-Gate, kein Write/Revoke bei Ablehnung.
- `state.py`: `IdentifierError`/`PathError` → `{"ok": false, ...}`; alle Artefakt-Lesewege auf `confined_artifact_dir()`.
- `locking.py`: `.mission.lock` über dieselbe HAI-Home-Rootauflösung.
- Tests: rote Härtungstests zuerst (siehe unten), zwei schwache bestehende Assertions verschärfen.

`server.py`, `storage.py`, Config-Keys, öffentliche Toolnamen: unverändert.

## Rote Tests zuerst (nur tmp_path-HAI_HOME; kein erfolgreicher Out-of-Root-Zugriff wird gezeigt, nur Ablehnung + unveränderter Zustand)

IDs: malformed mission/session/intake (falsches Präfix, `/`, `\`, `..`, absolut, Null-Byte, Whitespace, falsche Länge, verbotene Zeichen, Nicht-String) → `code == invalid_args`.
Interner Loader: `load_mission_meta("../../escape")` → `IdentifierError(code=invalid_args)` statt `None`.
Authorize/Session-Entrypoints (`get_contract`, `check_activity`, `park_item`): malformed → `ok: false`, `error: invalid_args`, kein Lease/kein `continue`.
Recontract/close mit malformed mission_id → keine Mutation.
Symlink-Escapes: mission-dir, `contracts/`, `sessions/`, artifact-dir (read+write) → `path_outside_root`, Sentinel unverändert.
Activity/Evidence: relative + absolute + symlink escapes → `path_outside_root`/`drift`/`stop`; fehlender Projekt-Root → `invalid_args`; Null-Byte → strukturierter Fehler ohne Raise.
Recontract: jedes nicht-gewhitelistete Feld (inkl. `mission_id`, `contract_version`, `status`, `recontract_*`) → `invalid_args`, keine v2; `mission_id`-Change auch mit ack → abgelehnt; `owner_ack` nur literal `True` (False/None/0/1/"true" → `owner_gate_required`); vollständige Kandidaten-Revalidierung (leere `done_criteria`, falsche Typen, `allowed_paths=["../"]`/absolut/symlink) → `invalid_args`/`path_outside_root`, keine v2.

## Reihenfolge (ein WIP-Slice)

1. Workspace erneut lesen (externe Änderungen während Planung sichtbar).
2. Nur die roten Tests schreiben und bestätigen, dass sie an den Lücken scheitern.
3. `ids.py` vervollständigen.
4. Zentrale Root-Auflösung in `paths.py`.
5. Speicherpfade + öffentliche ID-Aufrufstellen in `mission.py`.
6. Activity-/Evidence-Pfade.
7. Recontract-Whitelist + Kandidatenvalidierung + Owner-Gate.
8. `state.py`-Fehlergrenze + Artefaktpfade; Lockpfad absichern.
9. Erst Härtungstests, dann volle Suite, dann Compile-Smoke.
10. NICHT committen; Ergebnis + Restrisiken Samuel vorlegen.

## Verifikation

```bash
env UV_CACHE_DIR=/tmp/hai-mcp-uv-cache uv run pytest -q tests/test_mission_lifecycle.py tests/test_control_plane.py
env UV_CACHE_DIR=/tmp/hai-mcp-uv-cache uv run pytest -q
env UV_CACHE_DIR=/tmp/hai-mcp-uv-cache uv run python -m compileall -q src tests
```

## Akzeptanzkriterien

Jede ungültige ID eindeutig abgelehnt; kein ungültiger Identifier als „nicht gefunden" verschleiert;
kein user-kontrollierter Pfad verlässt via Traversal/absolut/Null-Byte/Symlink den Root; ohne Projekt-Root
kein Datei-/Evidence-Pfad akzeptiert; keine abgelehnte Anfrage erzeugt Lease/Version/Parking/Abschluss/Grant;
recontract nur die 5 Top-Level-Felder; `mission_id` unverändert; `owner_ack` exakt `True`; alle bisherigen
Happy Paths + Owner-Gates bleiben grün; keine neuen Config-Keys/Toolnamen/Dependencies.
