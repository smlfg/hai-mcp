# KORREKTUR-Slice für Composer: 2 Rest-Lücken der Flow-Tools

> Claude hat die additional-seven Flow-Tools gebaut (114 Tests grün). Unabhängiger Codex-Review:
> fast PASS — nur diese 2 kleinen echten Lücken bleiben. NICHT von Claude editiert (Token-Ersparnis).
> Composer setzt das um. TDD, echte Assertions, nicht committen.

## Gap 1 — neue Store-Verzeichnisse nicht confined (Sicherheit)

`state.py`: `intake()`, `distill()`, `stop_day()` schreiben nach `intake_dir` / `distill_dir` / `stop_dir`
(= `HAI_HOME/intake|distillations|stops`). `mkdir(exist_ok=True)` akzeptiert ein vorab platziertes
Symlink-Verzeichnis, das aus `HAI_HOME` herauszeigt → `write_json` schreibt dann außerhalb.

Fix (fail-closed, wie im Rest des Servers):
- In jeder der drei Methoden nach `mkdir(...)` und VOR dem `write_json`:
  `assert_under(self.<store>_dir, self.cfg.hai_home)` (aus `hai_mcp.paths` importieren, ist schon im Modul).
- Zusätzlich die konkrete Datei absichern: `assert_under(path, self.cfg.hai_home)` vor dem Write.
- Bei `PathError` strukturiert `{"ok": False, "error": "path_outside_root", ...}` zurückgeben.

Rote Tests zuerst (tests/test_flow_tools.py):
- Ersetze `HAI_HOME/intake` (bzw. distillations/stops) durch einen Symlink auf ein Verzeichnis außerhalb,
  rufe `intake()`/`distill()`/`stop_day()` und assert `ok False` + `error == "path_outside_root"`,
  und dass die Sentinel-Datei außerhalb byte-unverändert bleibt.

## Gap 2 — stop_day() Lease-Revocation nicht atomar (Korrektheit)

`state.py` `stop_day()`: `load_active_pointer()` + `revoke_all_sessions()` laufen ohne Lock.
Ein paralleles `authorize_session()` (das `mission_state_lock` hält) kann zwischen Auflistung und
Revocation eine neue aktive Lease erzeugen → Tag „gestoppt", aber lebende Lease.

Fix:
- Die Revocation-Sequenz in `with mission_state_lock(self.cfg.hai_home):` kapseln
  (Import: `from hai_mcp.locking import mission_state_lock`), analog zu `mission.py:471`/recontract/close.

Test:
- Optional (Race schwer deterministisch): mindestens ein Test, der stop_day() nach Autorisierung ausführt
  und assert, dass die Lease revoked ist (bereits vorhanden: test_stop_seals_day_and_revokes_leases).
  Der Lock ist die eigentliche Härtung; Regressionssicherung reicht.

## Verifikation
```bash
env UV_CACHE_DIR=/tmp/hai-mcp-uv-cache uv run pytest -q
env UV_CACHE_DIR=/tmp/hai-mcp-uv-cache uv run python -m compileall -q src tests
```
Erwartung: weiterhin alle grün + die 3 neuen Symlink-Ablehnungstests. NICHT committen.
Danach kann Claude den finalen Codex-Re-Review fahren.
