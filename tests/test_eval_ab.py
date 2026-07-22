from __future__ import annotations

import json
from pathlib import Path

import pytest

from hai_mcp.eval_ab import run_pair, score_arm


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "ab"


def _load_case(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text())


def _good_arm(intake_text: str) -> tuple[str, str, list[str]]:
    return (
        "Freeze the v0 A/B contract",
        "Add the deterministic fixture harness",
        ["Run provider-backed evaluation in Item 6"],
    )


def _cardinality_violator(intake_text: str) -> tuple[str, str, list[str]]:
    return ("Decision one\nDecision two", "One next step", [])


@pytest.mark.parametrize("fixture_name", ["case1.json", "case2.json"])
def test_score_arm_applies_both_deterministic_criteria(fixture_name: str) -> None:
    case = _load_case(fixture_name)

    good = score_arm(case["intake_text"], _good_arm(case["intake_text"]))
    bad = score_arm(case["intake_text"], _cardinality_violator(case["intake_text"]))

    assert good["criteria"] == {"a": 1, "b": 1}
    assert good["score"] == 2
    assert bad["criteria"]["a"] == 0
    assert bad["score"] == 0


@pytest.mark.parametrize("fixture_name", ["case1.json", "case2.json"])
def test_run_pair_returns_good_arm_as_winner(fixture_name: str) -> None:
    case = _load_case(fixture_name)

    result = run_pair(
        case,
        _good_arm,
        _cardinality_violator,
    )

    assert result["winner"] == "A"
    assert result["arm_a"]["score"] == 2
    assert result["arm_b"]["criteria"]["a"] == 0
