"""Learning budget: writer capacity derived from durable learning, never raising on a broken ledger."""

from __future__ import annotations

import json
import time
from pathlib import Path

from hai_mcp.config import Config, LEARNING_POLICY
from hai_mcp.learning_budget import LearningBudget

FLOOR = int(LEARNING_POLICY["worker_capacity_floor"])
CAP = int(LEARNING_POLICY["worker_capacity_cap"])
INITIAL = int(LEARNING_POLICY["initial_writer_credits"])


def _utc(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _budget(tmp_path: Path, *, now: list[float] | None = None) -> tuple[LearningBudget, Path]:
    study = tmp_path / "02_STUDIUM"
    study.mkdir()
    clock = (lambda: now[0]) if now is not None else None
    cfg = Config(
        hai_home=tmp_path / "hai-home",
        learning_evidence_root=study,
        clock=clock or time.time,
    )
    budget = LearningBudget(cfg)
    budget.initialize()
    return budget, study


def _write_suspension(hai_home: Path, *, reason: str, until_epoch: float) -> None:
    logbook = hai_home / "logbook" / "limits.jsonl"
    logbook.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "record_type": "limits_suspended",
        "at": _utc(until_epoch - 3600),
        "reason": reason,
        "until": _utc(until_epoch),
    }
    with logbook.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def test_capacity_without_learning_is_the_floor(tmp_path: Path) -> None:
    budget, _ = _budget(tmp_path)
    capacity = budget.worker_capacity()
    assert capacity.capacity == FLOOR
    assert capacity.completed_blocks == 0


def test_capacity_survives_a_destroyed_ledger(tmp_path: Path) -> None:
    budget, _ = _budget(tmp_path)
    blocks = budget.blocks_dir
    bad = blocks / "LB-20260809T095643-7751613b"
    bad.mkdir(exist_ok=True)
    (bad / "started.json").write_text("{ this is not json", encoding="utf-8")

    capacity = budget.worker_capacity()
    assert capacity.capacity == FLOOR
    assert capacity.degraded_reason is not None


def test_active_suspension_lifts_capacity_to_cap(tmp_path: Path) -> None:
    budget, _ = _budget(tmp_path)
    _write_suspension(budget.cfg.hai_home, reason="Habe heute Zeit", until_epoch=1_800_000_000.0)
    capacity = budget.worker_capacity(now=1_799_900_000.0)
    assert capacity.capacity == CAP
    assert capacity.suspension is not None


def test_start_allows_exactly_one_active_block(tmp_path: Path) -> None:
    now = [1_800_000_000.0]
    budget, study = _budget(tmp_path, now=now)

    started = budget.start("Python exceptions", str(study / "exceptions.md"))
    denied = budget.start("Python modules", str(study / "modules.md"))

    assert started["ok"] is True
    assert started["block_id"].startswith("LB-")
    assert denied["ok"] is False
    assert denied["error"] == "learning_block_active"


def test_complete_enforces_server_time_and_mints_credits(tmp_path: Path) -> None:
    now = [1_800_000_000.0]
    budget, study = _budget(tmp_path, now=now)
    evidence = study / "exceptions.md"
    started = budget.start("Python exceptions", str(evidence))

    reflection = {
        "block_id": started["block_id"],
        "own_activity": "I wrote and ran examples with try/except/finally.",
        "learned": "A finally block runs even when an exception propagates.",
        "open_question": "When should custom exceptions carry structured fields?",
        "evidence_paths": [str(evidence)],
        "owner_ack": True,
    }

    too_early = budget.complete(**reflection)
    assert too_early["ok"] is False
    assert too_early["error"] == "learning_time_required"

    evidence.write_text("My exception experiments and explanation.\n", encoding="utf-8")
    now[0] += 25 * 60
    completed = budget.complete(**reflection)
    duplicate = budget.complete(**reflection)

    assert completed["ok"] is True
    assert completed["credits_minted"] == 2
    assert completed["credit_balance"] == INITIAL + 2
    assert duplicate["ok"] is False
    assert duplicate["error"] == "learning_block_already_completed"
    assert budget.snapshot().completed_blocks == 1


def test_abandon_requires_owner_ack_and_mints_nothing(tmp_path: Path) -> None:
    now = [1_800_000_000.0]
    budget, study = _budget(tmp_path, now=now)
    started = budget.start("Python exceptions", str(study / "exceptions.md"))

    denied = budget.abandon(started["block_id"], "changed my mind", owner_ack=False)
    assert denied["ok"] is False
    assert denied["error"] == "owner_gate_required"

    abandoned = budget.abandon(started["block_id"], "changed my mind", owner_ack=True)
    assert abandoned["ok"] is True
    assert abandoned["credits_minted"] == 0
    assert budget.snapshot().completed_blocks == 0
