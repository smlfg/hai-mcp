# KORREKTUR-Slice: Fail-closed Härtung (unabhängiger Review FAIL)

> Der erste Umsetzungsversuch (MiniMax-M2.7) hat die Härtung NICHT umgesetzt.
> Independent-Review (Codex, read-only) Verdikt: FAIL. Die 53 grünen Tests bestätigen
> das alte, teils permissive Verhalten; mehrere Tests sind tautologisch.
> Diese Datei = exakte Gap-Liste mit Datei:Zeile. TDD, echte Assertions, keine Tautologien.

## Bewiesene Lücken (Gegenproben des Reviewers)

- `" M-20260722T010203-abcdef12 "` (mit Whitespace) wird als gültige ID akzeptiert.
- `/etc/passwd` als Activity-Pfad akzeptiert (ohne project_path permissiv).
- `pyproject.toml` als Evidence mit gültigem SHA-256-Beleg durchgewunken.

## Gap 1 — IDs nicht fail-closed

- `ids.py:5` Regex nur M/S/P/A — `I-`/`validate_intake_id` fehlt.
- `ids.py:15` `str(...).strip()` akzeptiert Whitespace/Nicht-String. Strikt: kein str(), kein strip(); Fullmatch; Nicht-String ablehnen.
- Keine strukturierte `IdentifierError` — `mission_dir()` wirft generisches `ValueError` (`mission.py:149`). Einführen: `IdentifierError(code, field, message)`.
- `load_contract()` (`mission.py:172`), `load_mission_meta()` (`mission.py:187`), `get_session()` (`mission.py:560`) tarnen ungültige IDs als `None`. Syntaktisch ungültig → Exception; nur nicht-existent → None.
- `get_session()` validiert gespeicherte `mission_id`/`session_id` nicht gegen Verzeichnis/Anfrage.
- Tautologischer Test `test_mission_lifecycle.py:646` fordert `None` bei `"../../escape"` — umdrehen auf `pytest.raises(IdentifierError)`.

## Gap 2 — Root-/Pfadhärtung unvollständig

- Zentrale `resolve_under_root(root, candidate, relative_to_root=True)` in `paths.py` fehlt — einführen; Null-Byte + Nicht-String vor jedem Path/OS-Aufruf ablehnen; relative Kandidaten unter Root hängen; real auflösen; `relative_to(real_root)` beweisen.
- `contracts/` (`mission.py:166`) und `sessions/` (`mission.py:169`) ohne Symlink-/Root-Prüfung angehängt.
- `get_session()` folgt `M-*`-Verzeichnis-Symlink (`mission.py:563`).
- Relative Activity-Pfade gegen CWD statt Projekt-Root (`mission.py:623`); Null-Byte → unstrukturiert (`paths.py:78`).
- Evidence: `exists()/is_dir()/is_file()` VOR Root-Prüfung + CWD-relativ (`mission.py:1050`). Root zuerst, dann Existenz.
- `state.py` Artefakt-Lesewege nutzen ungesichertes `artifact_dir()` und folgen externem Symlink (`state.py:99,112,134,352`). Auf `confined_artifact_dir()` umstellen.
- `.mission.lock` nicht zentral unter `HAI_HOME` aufgelöst (`locking.py:11`).

## Gap 3 — Recontract Owner-Gate falsch (Whitelist + immutable mission_id sind schon PASS)

- PASS: Top-Level-Whitelist 5 Felder (`mission.py:28,875`); `mission_id` aus altem Vertrag (`mission.py:916`).
- GAP: `if not owner_ack` akzeptiert `1`/`"true"`; ohne Ack sogar `ok: true` (`mission.py:978`). Fix: `owner_ack is True`; sonst `ok: false`, `error: owner_gate_required`, `status: pending_owner_confirmation` + `diff`.
- GAP: Kandidatenvalidierung normalisiert (`str()`, Listenfilter) statt abzulehnen; `allowed_paths` mit `../`/absolut/symlink nicht strikt abgelehnt (`mission.py:249,899`).
- Tautologische Tests verschärfen: `test_mission_lifecycle.py:361` (Owner-Gate prüft nichts), `:783` (Whitelist nur mission_id+ack=False, keine Versionsprüfung).

## Gap 4 — ohne Projekt-Root permissiv

- `_path_allowed()` gibt ohne project_path immer `(True, None)` (`mission.py:623`). Fix: Pfad übergeben aber kein Root → ablehnen (`invalid_args`).
- `_verify_evidence_path()` akzeptiert/hasht ohne Root jede Datei (`mission.py:1066`). Fix: ohne Root → `invalid_args`, kein Hash.
- Kein Test dafür — hinzufügen.

## Vorgehen (TDD, echte Assertions)

1. Dateien neu lesen.
2. Rote Tests ZUERST — jede Assertion prüft EXAKTEN Fehlercode UND unveränderten Zustand (Version, Lease, mission status, Sentinel-Bytes). Keine „crasht nicht"-Tautologien, kein `assert x is None` für ungültige IDs.
3. Bestätigen dass sie an den Lücken scheitern.
4. Fixes pro Gap.
5. Volle Suite grün + Compile-Smoke.

## Verifikation (Orchestrator prüft unabhängig nach)

```bash
env UV_CACHE_DIR=/tmp/hai-mcp-uv-cache uv run pytest -q
env UV_CACHE_DIR=/tmp/hai-mcp-uv-cache uv run python -m compileall -q src tests
```

Akzeptanz wie Ursprungsplan; zusätzlich: KEIN Test darf für eine ungültige ID `None` erwarten,
das Owner-Gate MUSS `ok:false`+`owner_gate_required` asserten, und es MUSS Tests geben, die
ohne project_root `/etc/passwd`-Activity und Fremd-Evidence als `invalid_args` ablehnen.
