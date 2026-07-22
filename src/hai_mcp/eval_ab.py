"""Minimal provider-agnostic A/B scoring for the v0 distill contract."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TypeAlias


Proposal: TypeAlias = tuple[str, str, list[str]]
ArmFn: TypeAlias = Callable[[str], Proposal]


def _contains_multiple_ideas(intake_text: str) -> bool:
    """Use the v0 fixture convention: one idea per non-empty line."""
    return len([line for line in intake_text.splitlines() if line.strip()]) > 1


def score_arm(intake_text: str, proposal: Proposal) -> dict[str, object]:
    """Score one proposal against the two frozen deterministic criteria."""
    try:
        decision, next_step, parklist = proposal
    except (TypeError, ValueError):
        decision, next_step, parklist = None, None, None

    criterion_a = int(
        isinstance(decision, str)
        and bool(decision.strip())
        and "\n" not in decision
        and "\r" not in decision
        and isinstance(next_step, str)
        and bool(next_step.strip())
        and "\n" not in next_step
        and "\r" not in next_step
    )
    criterion_b = int(
        isinstance(parklist, list)
        and (not _contains_multiple_ideas(intake_text) or bool(parklist))
    )
    criteria = {"a": criterion_a, "b": criterion_b}
    return {"criteria": criteria, "score": criterion_a + criterion_b}


def run_pair(
    case: Mapping[str, object],
    arm_a_fn: ArmFn,
    arm_b_fn: ArmFn,
) -> dict[str, object]:
    """Run and compare two arms on the same fixture case."""
    intake_text = case["intake_text"]
    if not isinstance(intake_text, str):
        raise TypeError("case['intake_text'] must be a string")

    arm_a = score_arm(intake_text, arm_a_fn(intake_text))
    arm_b = score_arm(intake_text, arm_b_fn(intake_text))
    score_a = arm_a["score"]
    score_b = arm_b["score"]
    if score_a > score_b:
        winner = "A"
    elif score_b > score_a:
        winner = "B"
    else:
        winner = "tie"
    return {"arm_a": arm_a, "arm_b": arm_b, "winner": winner}

