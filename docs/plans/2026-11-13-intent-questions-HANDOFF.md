# Handoff — Intent-Fragen beim Öffnen einer Mission (2026-11-13)

Status: Anforderungen. Kein Code geschrieben. Noch kein WIP-Slice; `GOAL.md` muss
vor Baubeginn umgestellt werden.

Hintergrund: `docs/plans/2026-11-13-h2a-guardrails.md`.

## 0. Vor allem anderen klären

Der installierte Stand auf dem Mac weicht von `main` dieses Repos ab. Die
Audit-Daten enthalten `mission_parked`, `mission_blocked`, `capacity_revoked` und
`verified.json`; nichts davon existiert hier. Erste Aufgabe des Umsetzenden:
feststellen, auf welches Verzeichnis und welchen Commit der MCP-Eintrag `hai-mcp`
zeigt, und gegen welchen Stand gebaut wird. Ohne diese Antwort nicht anfangen.

## 1. Problem

Das Audit-Log weiß, wie eine Mission endete. Es weiß nicht, was der Owner
gemeint hat. Deshalb ließ sich am 2026-11-13 aus 315 Missionen nicht ableiten,
welche Vorgaben zu guten Ergebnissen führen. Die Absicht steht heute nur in
Prosa (Objective, Non-Goals) und ist nicht vergleichbar.

Zwei belegte Folgen:

- Kriterien schwächer als die Absicht: „Statuswechsel ist möglich" wurde mit
  einem Dokument belegt; der Live-Test steht bis heute aus
  (`M-20261113T134459-0acf71ab`).
- Späte Fehlererkennung: `M-20260820T230243` lief fünf Tage und acht
  Vertragsversionen bis zum Abbruch.

## 2. Idee

Ein fester Pool kurzer Fragen. Der Agent wählt vor dem Öffnen **null bis drei**
davon aus, stellt sie dem Owner und übergibt die Antworten beim Öffnen. Der
Server speichert sie strukturiert an der Mission.

Der Server wählt nichts aus und deutet nichts: kein LLM im Server.

## 3. Leitgröße

Die knappe Ressource ist die Aufmerksamkeit des Owners. Jede Entscheidung in
diesem Slice wird daran gemessen: Kostet sie den Owner mehr, als sie zurückgibt?

## 4. Anforderungen

1. **Pool abrufbar.** Ein lesendes Tool liefert den Pool: `id`, Fragetext,
   Antwortform, erlaubte Werte, Pool-Version.
2. **Null bis drei.** Der Server nimmt höchstens drei Antworten an. Null ist
   gültig; es gibt keine Pflichtfrage.
3. **Geschlossene Antworten.** Jede Antwort ist ein Wert aus einer festen
   Auswahl. Ausnahme: die zwei Freitextfragen (Q1, Q2), mit harter Längengrenze.
4. **Validierung fail-closed.** Unbekannte Frage-ID, Wert außerhalb der Auswahl
   oder mehr als drei Antworten → das Öffnen wird abgelehnt, wie heute bei
   fehlender Kriterien-Beschreibung.
5. **Gespeichert an der Mission**, mit Pool-Version, und im Audit-Event des
   Öffnens enthalten.
6. **Lesbar für jede Session.** `hai_get_contract` gibt die Antworten zurück,
   damit jedes Harness dieselbe Absicht sieht.
7. **Abwärtskompatibel.** Missionen ohne Antworten verhalten sich wie heute.

## 5. Pool v0

Q8 und Q9 hat der Owner als wichtig benannt. Q1–Q7 sind ein Vorschlag aus der
Session und vom Owner noch nicht bestätigt. Ein Platz ist frei.

| ID | Frage | Antwortform |
|---|---|---|
| Q1 | Woran siehst du selbst, dass es fertig ist? | ein Satz |
| Q2 | Was darf ich nicht anfassen? | Pfade oder „nichts" |
| Q3 | Wann willst du zum ersten Mal draufschauen? | nach Plan / nach erstem Schritt / am Ende |
| Q4 | Wer benutzt das Ergebnis? | Kunde / Owner / niemand (Versuch) |
| Q5 | Muss es einmal echt durchgeführt werden? | ja / nein |
| Q6 | Erledigen oder dabei lernen? | erledigen / lernen |
| Q7 | Wie viel Zeit ist das wert? | 10 min / 1 h / halber Tag |
| Q8 | **Kosten:** Wie viel Token-Budget ist das wert? | klein / mittel / groß |
| Q9 | **Fokus-Level:** Wie präsent bist du dabei? | voll dabei / nebenbei / abwesend |
| Q10 | frei | — |

Hinweise:

- Q8: Der Server kann Token-Verbrauch nicht messen. Die Antwort ist ein
  genanntes Budget. Ein Soll-Ist-Vergleich geht nur, wenn das Harness den
  Verbrauch meldet (siehe offene Entscheidungen).
- Q9 überschneidet sich mit Q3. Beide bleiben vorerst; die Daten zeigen, welche
  trägt.

## 6. Nicht-Ziele

- Kein LLM im Server; keine Auswahl oder Bewertung der Fragen durch den Server.
- Keine Pflichtfragen, keine Mindestzahl.
- Kein Verhalten des Servers, das von den Antworten abhängt (kein Gate, keine
  Lease-Änderung). Dieser Slice speichert nur.
- Keine Auswertung, kein Dashboard, keine Metriken im Server.
- Keine Änderung an Owner-Gates.
- Kein Eingriff in das Live-`~/.hai`.

## 7. Abnahme (isoliertes `HAI_HOME`)

1. Der Pool-Abruf liefert jede Frage mit ID, Antwortform und Pool-Version.
2. Eine Mission mit drei gültigen Antworten öffnet; die Antworten stehen an der
   Mission, im Öffnen-Event und in `hai_get_contract`.
3. Eine Mission ohne Antworten öffnet wie heute.
4. Vier Antworten, eine unbekannte ID oder ein Wert außerhalb der Auswahl
   lehnen das Öffnen ab; es entsteht keine Mission.
5. Alle bestehenden Tests bleiben grün.

## 8. Messung

Nach zwei bis vier Wochen, einmalig, lesend über das Audit-Log:

- pro Frage: wie oft gestellt, Verteilung der Antworten
- pro Antwort: Ausgang der Mission (abgeschlossen, abgebrochen, Neuverträge)
- Fragen, die nie gestellt werden oder deren Antwort am Ausgang nichts ändert,
  fliegen aus dem Pool

Bekannte Schwäche: Der Agent wählt die Fragen, und ein bequemer Agent wählt die
bequemen. Das zeigt sich an Fragen, die nie gestellt werden.

## 9. Offene Entscheidungen des Owners

Diese Punkte legt der Umsetzende **nicht** selbst fest:

1. Welche der Fragen Q1–Q7 bleiben, und was kommt auf Q10?
2. Gehören die Antworten in den Vertrags-Hash? Wenn ja, braucht jede Änderung
   einen Recontract mit Owner-Gate. Wenn nein, sind sie nachträglich änderbar.
3. Eigenes Tool für den Pool oder Teil einer bestehenden Antwort?
4. Token-Verbrauch: nur Budget speichern, oder soll das Harness den Ist-Wert
   beim Schließen melden dürfen?
5. Wo lebt der Pool: fest im Code (Änderung per Release) oder als Datei in
   `HAI_HOME` (Änderung durch den Owner)?

## 10. Vorschlag für den Missionsvertrag der Umsetzung

- `allowed_paths`: `src/hai_mcp/`, `tests/`, `docs/TOOL_CONTRACT.md`, `GOAL.md`
- Non-Goals als Pfade ausdrücken, nicht als Sätze.
- Erstes Hinsehen des Owners: nach dem Entwurf des Tool-Schemas, vor dem Code.
- Ein Kriterium verlangt einen echten Lauf: eine Mission mit Antworten über
  stdio geöffnet und per `hai_get_contract` zurückgelesen.
