# A/B Harness Contract — v0

## Zweck

Dieser Contract friert den kleinsten validen A/B-Eval für den
`hai_distill`-Kardinalitäts-Gate ein. Zwei Modelle erhalten denselben rohen
Intake-Text und schlagen jeweils genau ein `decision`, genau einen
`next_step` und eine `parklist` vor.

v0 misst nur die deterministische Formtreue und das Parken von Restideen. Es
gibt keine Provider-Integration, keinen LLM-Judge und kein großes
Harness-Framework.

## Arme

- **Arm A — Modell A:** erhält den Intake-Text und liefert
  `(decision, next_step, parklist)`.
- **Arm B — Modell B:** erhält denselben Intake-Text und liefert dasselbe
  Proposal-Format.

Die beiden Callables werden von `run_pair` nur als Funktionsargumente
injiziert. `src/hai_mcp/eval_ab.py` macht keine Netzwerkaufrufe und verwendet
keine Provider-SDKs.

## Input und Fixtures

Der Input besteht aus genau **N=2 Intake-Fixtures**:

- `tests/fixtures/ab/case1.json`
- `tests/fixtures/ab/case2.json`

Jede Fixture enthält `intake_text` und `min_parklist_length`. Für v0 steht
jeweils eine Idee in einer nicht-leeren Zeile; mehr als eine solche Zeile
markiert einen Intake mit mehreren Ideen. Die erwartete Mindestlänge der
Parklist ist in beiden Fixtures `1`.

## Deterministische Bewertung

`score_arm(intake_text, proposal)` liefert zwei binäre Kriterien und ihre
Summe:

| Kriterium | Bestanden, wenn | Wert |
|---|---|---:|
| **(a)** Kardinalität | `decision` und `next_step` sind jeweils genau ein nicht-leerer String; keiner enthält ein Newline-Bundle | 1 / 0 |
| **(b)** Parken | Bei einem Input mit mehr als einer Idee ist `parklist` nicht leer | 1 / 0 |

Der Score ist die Zahl der bestandenen Kriterien: `score = a + b` (0 bis 2).
Das Ergebnisformat ist:

```json
{
  "criteria": {"a": 1, "b": 1},
  "score": 2
}
```

`run_pair(case, arm_a_fn, arm_b_fn)` bewertet beide Arme mit demselben
`case["intake_text"]`. Der Sieger ist der Arm mit dem höheren Score;
`"tie"` bezeichnet Gleichstand.

> „live-Ausführung gegen Provider ist Item 6 und pending, weil der
> MiniMax/opencode-Kanal in dieser Umgebung leere Antworten liefert."
