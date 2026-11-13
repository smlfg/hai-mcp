# Handoff — Intent-Fragen beim Öffnen einer Mission (2026-11-13)

Status: **Stufe 1 (Anforderung)**, Ausgang noch nicht erfüllt (siehe Abschnitt 11).
Kein Code geschrieben. Noch kein WIP-Slice; `GOAL.md` muss vor Baubeginn
umgestellt werden.

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

## 3. Leitprinzip

HAI-MCP braucht intentionale Kommunikation des Menschen. Was der Owner nicht
ausdrücklich sagt, deutet der Agent. Der Vertrag verpflichtet deshalb beide
Parteien: den Agenten auf Lease, Vertragstreue und Beleg; den Owner darauf, zu
sagen, was er meint, und am Ende hinzusehen.

Je besser die Agenten Lücken füllen, desto seltener muss der Owner seine Absicht
ausformulieren, und desto seltener klärt er sie für sich selbst. HAI-MCP legt
Reibung gezielt dorthin zurück. Reibung ist ihren Preis nur wert, wenn sie
Aufmerksamkeit oder Fokus zurückgibt (Beispiel: der einzelne Slot), nicht wenn
sie nur kostet (Beispiel: Code-Eingabe für eine fremde verwaiste Mission).

## 3a. Leitgröße

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
2. ~~Antworten im Vertrags-Hash?~~ **Entschieden 2026-11-13: ja.** Die Absicht
   ist Teil der Zusage; eine Änderung ist ein Recontract mit Owner-Gate.
3. ~~Eigenes Tool für den Pool?~~ **Entschieden 2026-11-13: ja**, ein lesendes
   Tool. Die Antworten kommen als Parameter beim Öffnen mit.
4. Token-Verbrauch: nur Budget speichern, oder soll das Harness den Ist-Wert
   beim Schließen melden dürfen?
5. ~~Wo lebt der Pool?~~ **Entschieden 2026-11-13: im Code.** Versioniert,
   testbar, von keinem Agenten nebenbei änderbar. Kürzen des Pools per Release.
6. Phasenwechsel: Soll der Übergang von Planen zu Bauen eine ausdrückliche
   Freigabe des Owners brauchen (ein Wort wie „go", Gegenstück „halt")? Am
   2026-11-13 hat ein Agent ohne Freigabe Code gebaut, obwohl der Vertrag „kein
   Code" vorsah. Nicht Teil dieses Slices; hier nur als Entscheidung festgehalten.

## 9a. Idee, nicht Teil dieses Slices: Beanspruchen statt Abfragen

Beobachtung des Owners: Er arbeitet inzwischen an einem Thema, aber nicht mehr
tief genug. HAI-MCP soll den Kopf des Menschen beanspruchen, ohne fordernd und
lästig zu sein. Wie genau, ist offen. Drei Richtungen aus der Session, vom Owner
noch nicht gewählt:

1. **Im Leerlauf statt als Schranke.** Die Frage kommt, während der Agent
   arbeitet, nicht vor dem Start. In dieser Wartezeit entstehen heute die
   parallelen Sessions.
2. **Als Vorhersage statt als Formular.** Vor dem Ergebnis ein Satz: „Was
   erwartest du gleich zu sehen?" Die Abweichung zwischen Erwartung und Ergebnis
   ist das, was hängen bleibt.
3. **Gesprochen statt getippt.** Der Agent liest vor, was er getan hat; der Owner
   antwortet kurz. Der Owner hat das in früheren Sessions als hilfreich
   beschrieben.

Gemeinsamer Nenner: hineinziehen statt verlangen.

Offen und nicht geprüft: ob das in den Server gehört oder ins Harness (Zeitpunkt
und Sprachausgabe kennt nur das Harness), und ob es den Fragenpool ergänzt oder
ersetzt. Nicht bauen, bevor der Owner eine Richtung gewählt hat.

## 9b. Einordnung des Owners: mehr als die Textdimension

Der Owner versteht HAI als nächste Stufe nach CLI → TUI → GUI: eine
Schnittstelle, in die alles einfließen kann, was zwischen Mensch und Agent
Bedeutung trägt, nicht nur Text. Human in the loop heißt hier ausdrücklich
nicht: Der Mensch hat ein Meta-System und schaltet dann mental ab.

**Was davon schon existiert.** Das Owner-Gate per Touch ID ist eine körperliche
Handlung in einem Kanal, den der Agent nicht bespielt. Der Owner beschreibt es
als intentionaler als die Freigabe in jedem anderen Harness, das er benutzt hat:
Eine Auswahl „1, 2 oder 3" liegt im selben Tippfluss wie alles andere und wird
zum Reflex; der Finger auf dem Sensor nicht.

**Verlängerung derselben Linie** (Idee des Owners, nicht Teil dieses Slices):

- Sprechen: eine Plaud-Aufnahme als Absicht oder als Erklärung am Ende.
- Zeichnen: eine reMarkable-Skizze als Bild des Ergebnisses.

Ohne Code möglich: Als Beleg für ein Kriterium nimmt der Server jede Datei an
(Existenz und Hash). Ein Transkript oder eine exportierte Seite ist eine Datei.
Ein Kriterium wie „Owner hat in zwei Minuten gesprochen erklärt, was gebaut
wurde" lässt sich also schon heute in einen Vertrag schreiben. Erst an einer
echten Mission ausprobieren, dann entscheiden, ob etwas festgeschrieben wird.

**Zu schützen: Die Geste wirkt, weil sie selten ist.** In drei Monaten gab es 49
bestandene Gates. Mindestens 25 Abbrüche waren aber Aufräumen verwaister
Missionen. Muss der Owner den Finger für Hausmeisterarbeit auflegen, wird aus
der bewussten Entscheidung Routine, und sie verliert ihre Wirkung auch dort, wo
es zählt. Das ist das stärkste Argument, verwaiste Missionen ohne Owner-Gate aus
dem Slot zu nehmen (siehe `2026-11-13-h2a-guardrails.md`, Abschnitt 1b).

## 10. Vorschlag für den Missionsvertrag der Umsetzung

- `allowed_paths`: `src/hai_mcp/`, `tests/`, `docs/TOOL_CONTRACT.md`, `GOAL.md`
- Non-Goals als Pfade ausdrücken, nicht als Sätze.
- Erstes Hinsehen des Owners: nach dem Entwurf des Tool-Schemas, vor dem Code.
- Ein Kriterium verlangt einen echten Lauf: eine Mission mit Antworten über
  stdio geöffnet und per `hai_get_contract` zurückgelesen.

## 11. Stufen

Ein Handoff ist Doku, bis seine Abnahme maschinell prüfbar im Repo liegt. Absicht
allein schützt nicht vor Drift: Der Umsetzende legt Sätze aus.

| Stufe | Inhalt | Ausgang zur nächsten Stufe |
|---|---|---|
| 0 Idee | Prosa (hier: 9a, 9b) | Owner wählt eine Richtung |
| 1 Anforderung | dieser Handoff | alle Owner-Entscheidungen beantwortet; Start-Commit steht fest |
| 2 Spezifikation | Schema, rote Tests, Tripwires im Repo | „go" des Owners nach Blick aufs Schema |
| 3 Umsetzung | Mission mit Vertrag | Tests grün, Testdatei unverändert, Budget eingehalten |
| 4 Echter Lauf | über stdio, vom Owner angesehen | Owner schließt |

Keine Stufe wird übersprungen. Der Wechsel von 2 nach 3 ist der Phasenwechsel
von Planen zu Bauen und braucht eine ausdrückliche Freigabe.

**Stand:** Stufe 1. Offen für den Ausgang: Entscheidungen 1, 4 und 6 aus
Abschnitt 9 sowie der Start-Commit (Abschnitt 0). Entscheidungen 2, 3 und 5 sind
getroffen und reichen für das Schema; Entscheidung 1 (endgültige Fragenliste)
bestimmt nur den Inhalt des Pools, nicht seine Form.

## 12. Tripwires für Stufe 3

Jeder Punkt ist maschinell prüfbar. Sie werden in Stufe 2 angelegt, nicht erst
während der Umsetzung.

| Tripwire | Fängt |
|---|---|
| Start-Commit festgelegt; weicht HEAD ab, wird nicht begonnen | falscher Checkout |
| `allowed_paths` im Vertrag (Abschnitt 10) | Arbeit außerhalb des Auftrags |
| Abnahme (Abschnitt 7) als rote Tests, die vor dem Code im Repo liegen | Umdeuten von „fertig" |
| Hash der Testdatei im Vertrag; fertig heißt: Tests grün **und** Datei unverändert | Tests anpassen statt Code |
| Schema für Pool und Antworten als Datei, vom Owner freigegeben, Hash festgelegt | stilles Ändern der Schnittstelle |
| Zahl der MCP-Tools genau eins höher als vorher; keine neue Abhängigkeit | wachsender Scope |
| Obergrenze für geänderte Dateien und Zeilen (in Stufe 2 beziffert) | zu große Änderung |
| Alle bestehenden Tests bleiben; keiner gelöscht oder übersprungen | „grün" durch Weglassen |

Grenze: Tripwires fangen agentischen Drift (der Agent tut etwas anderes). Den
semantischen Drift (alles grün, aber nicht gemeint) fängt nur der Blick des
Owners. Deshalb bleiben zwei Stellen beim Owner: nach dem Schema (Ausgang
Stufe 2) und nach dem echten Lauf (Stufe 4).

