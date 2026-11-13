# H2A-Guardrails — Plan (2026-11-13)

Status: S1 umgesetzt (`hai-guard`, `tests/test_guard.py`); S2 und S3 offen. Ersetzt weder `GOAL.md` noch `docs/TOOL_CONTRACT.md`.

Frage: Welcher H2A-Fehler wird **wo** gestoppt, und was fehlt dafür?

Grundlage:

- Report „Die zehn wichtigsten Fehlermuster" (Momentaufnahme, 2026-11-13)
- Belegsammlung B001–B051 (90 Tage, fünf Quellen, 40 Fehler / 11 Gegenbelege)
- Code-Stand `205e15c`

Noch offen: die Retro-Zeitleiste (frühere Optimizer-Versuche). Ohne sie bleibt
die Spalte „Klasse" leer.

## 1. Was die Belege zeigen

**a) HAI-MCP sieht nur angemeldete Sessions.** Die schwersten Parallelitäts-Belege
liegen außerhalb des Servers: zwei Sessions auf demselben Port (B026), fünf Bots
mit zehn Richtungswechseln (B048), zehn Agenten auf eine Nachricht (B049). Kein
Gate im Server kann das verhindern, weil diese Sessions ihn nie fragen.

**b) Die Gates haben eigene Kosten.** Sechs der dreizehn HAI-Belege betreffen
Reibung durch das Gate selbst: verwaiste aktive Mission blockiert alles (B006,
B007, B022), ein fertiger Agent kann nicht schließen, weil inzwischen eine andere
Mission aktiv ist (B008). `supersede_orphan` (#15) und die Pfad-Validierung beim
Öffnen haben den Lockout entschärft. Jeder neue Guardrail muss gegen dieses
Risiko geprüft werden.

**c) Parken hat keinen Abfluss.** Am 2026-09-18 lagen 67 offene Posten vor: 17
beschlossene Next-Steps, 25 geparkte Items (ältestes 41 Tage), 25 nie triagierte
Intakes (B007). `hai_park`, `hai_park_item` und `hai_intake` schreiben nur; kein
Tool schließt einen Posten.

**d) Der Owner ist selbst die Quelle der Themenwechsel.** B003 und B005 liefen
korrekt durch das Owner-Gate. Das Gate macht den Wechsel sichtbar und teuer, es
verhindert ihn nicht, und das soll es auch nicht.

**e) B010/B011 sind ungeprüft.** Die Sammlung meldet „completed ohne
`verified.json`". Dieser Code schreibt keine `verified.json`; verifizierte
Kriterien stehen im Audit-Event `mission_completed` (`mission.py:1741`).
`close_mission` kann ohne Evidence pro Kriterium nicht `completed` setzen
(`mission.py:1683-1730`). Wahrscheinlicher ist: Die Kriterien waren so
formuliert, dass eine Datei sie erfüllt, obwohl der Live-Test fehlte. Zu prüfen
am Audit-Event beider Missionen, bevor daraus ein Slice wird.

Grenzen der Sammlung: 51 Einträge sind eine Stichprobe, die Zählungen sind keine
Häufigkeiten. B001 und B002 sind als Fehler geführt, zeigen aber ein Gate, das
funktioniert. B015, B016 und B021 sind Verlierer eines absichtlichen
Modellvergleichs, keine H2A-Fehler.

## 2. Zuordnung

Ort: **Server** = dieser Code · **Harness** = Hook im Client · **GitHub** =
Repo-Einstellung · **Mensch** = nicht technisch lösbar.

| # | Muster | Belege | Ort | Existiert heute | Fehlt | Klasse |
|---|---|---|---|---|---|---|
| 1 | Parallele Pfade | B008, B026, B048, B049 | Harness | eine aktive Mission; `max_parallel_sessions=1` (`mission.py:836`) | Schreibzugriff ohne Lease wird nirgends blockiert | |
| 2 | Meta-Engineering | B009, B050 | Mensch | — | vom Server nicht erkennbar (semantisch) | |
| 3 | Agent baut allein | B019, B031 | Mensch | Owner-Gate bei `accept_next_step`, `recontract` | Designentscheidungen laufen durch kein Gate | |
| 4 | Falsches „done" | B010, B011, B033 | Server + Mensch | Evidence pro Kriterium, Datei + SHA-256 (`mission.py:1498`) | prüft Existenz, nicht Wahrheit; siehe 1e | |
| 5 | Scope-Creep | B004, B005, B032 | Harness | `allowed_paths`, `non_goals`, `check_activity` → `park_candidate` (`mission.py:1050`) | Agent meldet selbst; kein Pfad-Check vor dem Edit | |
| 6 | Falscher Kontext | B003, B048 | Server | `project_id`, `device_id`, `harness_id`, Mounts | Branch und HEAD werden nicht festgehalten | |
| 7 | Provenance | — | Server | Audit-Kette mit `seq` und Hash | kein Beleg in der Sammlung; zurückgestellt | |
| 8 | Fähigkeit fehlt | B018, B045 | Mensch | — | ohne Code: Smoke-Test als erstes Done-Kriterium | |
| 9 | Review durch Agent | — | GitHub | CI (#12) | Branch Protection; nicht Aufgabe von HAI-MCP | |
| 10 | Projektproliferation | B050 | Mensch | `projects.json` | kein Zwang, vor „neu" nachzusehen | |
| 11 | Parken ohne Abfluss | B007 | Server | `hai_park`, `hai_park_item`, `hai_intake` | Posten können nicht geschlossen werden | |
| 12 | Kontext neu erklären | B001, B028, B047, B051 | außerhalb | — | Out of scope laut `GOAL.md` (kein Memory, kein Chat-Sync) | |

Muster 11 und 12 stehen nicht im Report, aber in den Belegen.

## 3. Slice-Kandidaten

Höchstens drei, in dieser Reihenfolge. Jeweils ein WIP-Slice.

### S1 — Lease-Guard für Schreibzugriffe (Muster 1, 5)

Ein read-only CLI-Befehl, den ein Harness-Hook vor jedem Schreibzugriff aufruft.
Er liest `HAI_HOME` und entscheidet: gültige Lease vorhanden, Pfad innerhalb
`allowed_paths`?

- Wo: neuer Unterbefehl in `__main__.py`, Logik aus `_session_valid`
  (`mission.py:698`) und `_path_allowed` (`mission.py:1021`); Hook-Beispiel in
  `docs/client-snippets/`
- Grenze: greift nur in Verzeichnissen, die in `projects.json` gemountet sind.
  Außerhalb lässt er alles durch, sonst sperrt er den Rechner (siehe 1b).
- Kein LLM, keine Mutation, kein neues MCP-Tool.
- **Abnahme:** In einem isolierten `HAI_HOME` endet der Befehl für einen Pfad im
  gemounteten Projekt mit Exit 2 ohne gültige Lease oder außerhalb
  `allowed_paths`, mit Exit 0 innerhalb, und mit Exit 0 für jeden Pfad außerhalb
  aller Mounts.

### S2 — Kontext-Siegel auf der Lease (Muster 6)

`hai_authorize_session` hält Branch und HEAD des Mounts fest; `hai_check_activity`
meldet Abweichung.

- Wo: `authorize_session` (`mission.py:753`), `check_activity` (`mission.py:1050`)
- Additiv; Projekte ohne Git bleiben unverändert.
- **Abnahme:** Nach einem Branch-Wechsel im Mount liefert `hai_check_activity`
  für dieselbe Session `drift` mit Clause `constraints.context`.

### S3 — Abfluss für geparkte Posten (Muster 11) — bedingt

Nur bauen, wenn die Zeitleiste zeigt, dass der Rückstau nicht schon einmal
sichtbar gemacht wurde und trotzdem liegen blieb. B007 ist selbst ein
Wiedervorlage-Befund: Die Sichtbarkeit gab es also mindestens einmal.

- **Abnahme (falls gebaut):** Ein geparkter Posten lässt sich mit genau einem
  Ausgang (`done`, `dropped`, `promoted`) schließen und erscheint danach nicht
  mehr in der offenen Zählung.

## 4. Nicht bauen

- Metriken und Indizes aus dem Report (`meta_work_ratio`, Attention Budget,
  Qualitätsindex): kein Beleg, dass eine Zahl hier Verhalten ändert.
- Semantische Evidence-Prüfung: Out of scope laut `GOAL.md`.
- Review-Regeln im Server: gehört nach GitHub.
- Irgendetwas für Muster 2, 3, 8, 10: Die Gegenmaßnahme ist die Formulierung des
  Vertrags beim Öffnen, kein Code.

## 5. Offen, bevor S1 startet

1. Retro-Zeitleiste → Spalte „Klasse" füllen. Fällt Muster 1 oder 5 in „bekannt,
   klappt nicht", prüfen, ob die frühere Maßnahme ebenfalls freiwillig war. Wenn
   nein, trägt die These hinter S1 nicht.
2. B010/B011 am Audit-Event `mission_completed` prüfen: Welche Datei wurde als
   Evidence akzeptiert?
3. Läuft auf dem Mac dieser Code-Stand? Die `verified.json`-Dateien in B012/B013
   stammen nicht aus diesem Repo.
4. `GOAL.md` nennt noch den Central-Core-Slice als WIP. Vor S1 umstellen.
