from __future__ import annotations

import calendar
import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hai_mcp import limits_logbook
from hai_mcp.config import Config, LEARNING_POLICY
from hai_mcp.paths import PathError, assert_under
from hai_mcp.storage import read_json, write_json

LEARNING_BLOCK_ID = re.compile(r"^LB-\d{8}T\d{6}-[0-9a-f]{8}$")


class LearningStateError(ValueError):
    """The persisted learning ledger cannot be trusted."""


@dataclass(frozen=True)
class BudgetSnapshot:
    credit_balance: int
    initial_credits: int
    completed_blocks: int
    writer_credits_earned: int
    writer_credits_spent: int

    def as_dict(self) -> dict[str, int]:
        return {
            "credit_balance": self.credit_balance,
            "initial_credits": self.initial_credits,
            "completed_learning_blocks": self.completed_blocks,
            "writer_credits_earned": self.writer_credits_earned,
            "writer_credits_spent": self.writer_credits_spent,
        }


@dataclass(frozen=True)
class WorkerCapacity:
    """Concurrent writer slots: floor + one per completed Learning Block, capped.

    An active limit suspension lifts capacity straight to the cap for its bounded duration —
    the owner's escape hatch, paid for with a written reason rather than with permission.
    """

    capacity: int
    floor: int
    cap: int
    earned_slots: int
    completed_blocks: int
    blocks_until_next_slot: int | None
    suspension: dict[str, Any] | None
    degraded_reason: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "worker_capacity": self.capacity,
            "worker_capacity_floor": self.floor,
            "worker_capacity_cap": self.cap,
            "worker_slots_earned": self.earned_slots,
            "completed_learning_blocks": self.completed_blocks,
            "learning_blocks_until_next_slot": self.blocks_until_next_slot,
            "limits_suspension": self.suspension,
            "capacity_degraded_reason": self.degraded_reason,
        }


@dataclass
class LearningBudget:
    """Derive Writer Credits from durable learning and session records."""

    cfg: Config

    @property
    def root(self) -> Path:
        return self.cfg.hai_home / "learning"

    @property
    def activation_path(self) -> Path:
        return self.root / "activation.json"

    @property
    def blocks_dir(self) -> Path:
        return self.root / "blocks"

    def initialize(self) -> None:
        try:
            root = assert_under(self.root, self.cfg.hai_home)
            blocks = assert_under(self.blocks_dir, self.cfg.hai_home)
            root.mkdir(parents=True, exist_ok=True)
            blocks.mkdir(parents=True, exist_ok=True)
            activation = assert_under(self.activation_path, self.cfg.hai_home)
            if not activation.exists():
                write_json(
                    activation,
                    {
                        "record_type": "learning_budget_activated",
                        "policy_version": 2,
                        "activated_at": self._format_utc(self.cfg.clock()),
                        "credit_delta": LEARNING_POLICY["initial_writer_credits"],
                    },
                )
        except (OSError, PathError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise LearningStateError(str(exc)) from exc

    @staticmethod
    def _format_utc(epoch: float) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))

    @staticmethod
    def _parse_utc(raw: Any, field: str) -> int:
        try:
            return calendar.timegm(time.strptime(str(raw), "%Y-%m-%dT%H:%M:%SZ"))
        except (TypeError, ValueError) as exc:
            raise LearningStateError(f"{field} is invalid") from exc

    def _read_optional_record(self, path: Path) -> dict[str, Any] | None:
        if path.is_symlink():
            raise LearningStateError(f"learning record must not be a symlink: {path.name}")
        if not path.exists():
            return None
        if not path.is_file():
            raise LearningStateError(f"learning record is not a file: {path.name}")
        record = read_json(assert_under(path, self.cfg.hai_home), None)
        if not isinstance(record, dict):
            raise LearningStateError(f"learning record is invalid: {path.name}")
        return record

    def _read_block(
        self, block_dir: Path
    ) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]:
        if block_dir.is_symlink() or not block_dir.is_dir():
            raise LearningStateError("learning block store contains an invalid entry")
        if not LEARNING_BLOCK_ID.fullmatch(block_dir.name):
            raise LearningStateError("learning block store contains an invalid block id")

        started = self._read_optional_record(block_dir / "started.json")
        if started is None or started.get("record_type") != "learning_block_started":
            raise LearningStateError("learning block start record is missing or invalid")
        if started.get("block_id") != block_dir.name:
            raise LearningStateError("learning block start id does not match its directory")
        for field in ("topic", "intended_output_path", "started_at", "minimum_completion_at"):
            if not isinstance(started.get(field), str) or not started[field].strip():
                raise LearningStateError(f"learning block start field is invalid: {field}")
        started_epoch = self._parse_utc(started["started_at"], "learning block start time")
        minimum_epoch = self._parse_utc(
            started["minimum_completion_at"], "learning block minimum completion time"
        )
        minimum_seconds = int(LEARNING_POLICY["minimum_learning_minutes"]) * 60
        if minimum_epoch - started_epoch < minimum_seconds:
            raise LearningStateError("learning block minimum duration is invalid")

        completed = self._read_optional_record(block_dir / "completed.json")
        abandoned = self._read_optional_record(block_dir / "abandoned.json")
        if completed is not None and abandoned is not None:
            raise LearningStateError("learning block has conflicting terminal records")
        if completed is not None:
            if completed.get("record_type") != "learning_block_completed":
                raise LearningStateError("completed learning block record is invalid")
            if completed.get("block_id") != block_dir.name:
                raise LearningStateError("completed learning block id does not match its directory")
            if completed.get("owner_ack") is not True:
                raise LearningStateError("completed learning block lacks owner acknowledgement")
            for field in ("topic", "own_activity", "learned", "open_question", "completed_at"):
                if not isinstance(completed.get(field), str) or not completed[field].strip():
                    raise LearningStateError(f"completed learning block field is invalid: {field}")
            if completed.get("topic") != started.get("topic"):
                raise LearningStateError("completed learning block topic does not match its start")
            if self._parse_utc(completed["completed_at"], "learning completion time") < minimum_epoch:
                raise LearningStateError("learning completion predates the minimum duration")
            evidence = completed.get("evidence")
            if not isinstance(evidence, list) or not evidence:
                raise LearningStateError("completed learning block evidence is invalid")
            for item in evidence:
                if not isinstance(item, dict):
                    raise LearningStateError("completed learning block evidence is invalid")
                if not isinstance(item.get("path"), str) or not item["path"].strip():
                    raise LearningStateError("completed learning block evidence path is invalid")
                if not isinstance(item.get("sha256"), str) or not item["sha256"].startswith("sha256:"):
                    raise LearningStateError("completed learning block evidence digest is invalid")
                if type(item.get("bytes")) is not int or item["bytes"] < 1:
                    raise LearningStateError("completed learning block evidence size is invalid")
            delta = completed.get("credit_delta")
            if type(delta) is not int or delta != LEARNING_POLICY["writer_credits_per_learning_block"]:
                raise LearningStateError("completed learning block credit delta is invalid")
        if abandoned is not None:
            if abandoned.get("record_type") != "learning_block_abandoned":
                raise LearningStateError("abandoned learning block record is invalid")
            if abandoned.get("block_id") != block_dir.name:
                raise LearningStateError("abandoned learning block id does not match its directory")
            if abandoned.get("owner_ack") is not True:
                raise LearningStateError("abandoned learning block lacks owner acknowledgement")
            if not isinstance(abandoned.get("reason"), str) or not abandoned["reason"].strip():
                raise LearningStateError("abandoned learning block reason is invalid")
            if abandoned.get("credit_delta") != 0:
                raise LearningStateError("abandoned learning block must not mint credits")
        return started, completed, abandoned

    def _blocks(
        self,
    ) -> list[tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]]:
        blocks_dir = assert_under(self.blocks_dir, self.cfg.hai_home)
        records = []
        for block_dir in sorted(blocks_dir.iterdir()):
            records.append(self._read_block(block_dir))
        return records

    def _active_block(self) -> dict[str, Any] | None:
        active: list[dict[str, Any]] = []
        for started, completed, abandoned in self._blocks():
            if completed is None and abandoned is None:
                active.append(started)
        if len(active) > 1:
            raise LearningStateError("multiple active learning blocks found")
        return active[0] if active else None

    def _intended_path(self, raw_path: str) -> Path:
        raw = str(raw_path or "").strip()
        if not raw or "\x00" in raw:
            raise ValueError("intended_output_path is required")
        root = self.cfg.resolved_learning_evidence_root
        if not root.is_dir():
            raise ValueError(f"learning evidence root does not exist: {root}")
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        if candidate.is_symlink():
            raise ValueError("intended output path must not be a symlink")
        confined = assert_under(candidate, root)
        if confined.exists() and confined.is_dir():
            raise ValueError("intended output path must be a file path")
        return confined

    def _block_dir(self, block_id: str) -> Path:
        block_id = str(block_id or "").strip()
        if not LEARNING_BLOCK_ID.fullmatch(block_id):
            raise ValueError("block_id is invalid")
        return assert_under(self.blocks_dir / block_id, self.cfg.hai_home)

    def _evidence(self, raw_paths: list[str] | None) -> list[dict[str, Any]]:
        if not isinstance(raw_paths, list) or not raw_paths:
            raise ValueError("at least one evidence path is required")
        root = self.cfg.resolved_learning_evidence_root
        if not root.is_dir():
            raise ValueError(f"learning evidence root does not exist: {root}")
        verified: list[dict[str, Any]] = []
        for raw_path in raw_paths:
            raw = str(raw_path or "").strip()
            if not raw or "\x00" in raw:
                raise ValueError("evidence paths must be non-empty")
            path = Path(raw).expanduser()
            if not path.is_absolute():
                path = root / path
            if path.is_symlink():
                raise ValueError("evidence path must not be a symlink")
            confined = assert_under(path, root)
            if not confined.exists():
                raise ValueError(f"evidence path does not exist: {raw}")
            if not confined.is_file():
                raise ValueError(f"evidence path is not a file: {raw}")
            content = confined.read_bytes()
            if not content:
                raise ValueError(f"evidence file is empty: {raw}")
            verified.append(
                {
                    "path": str(confined),
                    "sha256": f"sha256:{hashlib.sha256(content).hexdigest()}",
                    "bytes": len(content),
                }
            )
        return verified

    def start(self, topic: str, intended_output_path: str) -> dict[str, Any]:
        topic = str(topic or "").strip()
        if not topic:
            return {"ok": False, "error": "invalid_args", "message": "topic is required"}
        try:
            active = self._active_block()
            if active is not None:
                return {
                    "ok": False,
                    "error": "learning_block_active",
                    "message": "only one Learning Block may be active globally",
                    "active_block_id": active["block_id"],
                }
            intended = self._intended_path(intended_output_path)
            now = self.cfg.clock()
            block_id = f"LB-{time.strftime('%Y%m%dT%H%M%S', time.gmtime(now))}-{uuid.uuid4().hex[:8]}"
            minimum_epoch = now + int(LEARNING_POLICY["minimum_learning_minutes"]) * 60
            record = {
                "record_type": "learning_block_started",
                "block_id": block_id,
                "topic": topic,
                "intended_output_path": str(intended),
                "started_at": self._format_utc(now),
                "minimum_completion_at": self._format_utc(minimum_epoch),
                "minimum_learning_minutes": LEARNING_POLICY["minimum_learning_minutes"],
            }
            block_dir = assert_under(self.blocks_dir / block_id, self.cfg.hai_home)
            block_dir.mkdir(parents=False, exist_ok=False)
            write_json(block_dir / "started.json", record)
            return {
                "ok": True,
                "status": "active",
                **record,
                "minimum_learning_minutes": LEARNING_POLICY["minimum_learning_minutes"],
            }
        except LearningStateError as exc:
            return {"ok": False, "error": "learning_state_invalid", "message": str(exc)}
        except (OSError, PathError, ValueError, TypeError) as exc:
            return {"ok": False, "error": "invalid_args", "message": str(exc)}

    def complete(
        self,
        block_id: str,
        own_activity: str,
        learned: str,
        open_question: str,
        evidence_paths: list[str] | None,
        owner_ack: Any,
    ) -> dict[str, Any]:
        if owner_ack is not True:
            return {
                "ok": False,
                "error": "owner_gate_required",
                "message": "learning completion requires owner_ack=true (literal boolean)",
            }
        reflection = {
            "own_activity": str(own_activity or "").strip(),
            "learned": str(learned or "").strip(),
            "open_question": str(open_question or "").strip(),
        }
        missing = [name for name, value in reflection.items() if not value]
        if missing:
            return {
                "ok": False,
                "error": "invalid_args",
                "message": f"structured reflection fields are required: {', '.join(missing)}",
            }
        try:
            block_dir = self._block_dir(block_id)
            completed_path = block_dir / "completed.json"
            abandoned_path = block_dir / "abandoned.json"
            if completed_path.exists():
                return {
                    "ok": False,
                    "error": "learning_block_already_completed",
                    "message": "Learning Block is already completed",
                }
            if abandoned_path.exists():
                return {
                    "ok": False,
                    "error": "learning_block_abandoned",
                    "message": "Learning Block was abandoned and cannot be completed",
                }
            started_path = block_dir / "started.json"
            if not started_path.is_file() or started_path.is_symlink():
                return {"ok": False, "error": "invalid_args", "message": "Learning Block not found"}
            started = read_json(assert_under(started_path, self.cfg.hai_home), None)
            if not isinstance(started, dict) or started.get("record_type") != "learning_block_started":
                raise LearningStateError("learning block start record is invalid")

            minimum_raw = started.get("minimum_completion_at")
            minimum_epoch = self._parse_utc(minimum_raw, "learning block deadline")
            now = self.cfg.clock()
            if now < minimum_epoch:
                return {
                    "ok": False,
                    "error": "learning_time_required",
                    "message": "Learning Block has not reached its minimum server-timed duration",
                    "minimum_completion_at": minimum_raw,
                    "remaining_seconds": int(minimum_epoch - now),
                }

            # Validate the existing ledger before appending the immutable credit event.
            self.snapshot()
            verified_evidence = self._evidence(evidence_paths)
            completed = {
                "record_type": "learning_block_completed",
                "block_id": started["block_id"],
                "topic": started["topic"],
                **reflection,
                "evidence": verified_evidence,
                "completed_at": self._format_utc(now),
                "credit_delta": LEARNING_POLICY["writer_credits_per_learning_block"],
                "owner_ack": True,
            }
            write_json(completed_path, completed)
            snapshot = self.snapshot()
            return {
                "ok": True,
                "status": "completed",
                "block_id": started["block_id"],
                "credits_minted": completed["credit_delta"],
                "credit_balance": snapshot.credit_balance,
                "record": completed,
            }
        except LearningStateError as exc:
            return {"ok": False, "error": "learning_state_invalid", "message": str(exc)}
        except (OSError, PathError, ValueError, TypeError, json.JSONDecodeError) as exc:
            return {"ok": False, "error": "invalid_args", "message": str(exc)}

    def abandon(self, block_id: str, reason: str, owner_ack: Any) -> dict[str, Any]:
        if owner_ack is not True:
            return {
                "ok": False,
                "error": "owner_gate_required",
                "message": "learning abandon requires owner_ack=true (literal boolean)",
            }
        reason = str(reason or "").strip()
        if not reason:
            return {"ok": False, "error": "invalid_args", "message": "reason is required"}
        try:
            block_dir = self._block_dir(block_id)
            completed_path = block_dir / "completed.json"
            abandoned_path = block_dir / "abandoned.json"
            if completed_path.exists():
                return {
                    "ok": False,
                    "error": "learning_block_already_completed",
                    "message": "completed Learning Block cannot be abandoned",
                }
            if abandoned_path.exists():
                return {
                    "ok": False,
                    "error": "learning_block_already_abandoned",
                    "message": "Learning Block is already abandoned",
                }
            started_path = block_dir / "started.json"
            if not started_path.is_file() or started_path.is_symlink():
                return {"ok": False, "error": "invalid_args", "message": "Learning Block not found"}
            started = read_json(assert_under(started_path, self.cfg.hai_home), None)
            if not isinstance(started, dict) or started.get("record_type") != "learning_block_started":
                raise LearningStateError("learning block start record is invalid")
            self.snapshot()
            record = {
                "record_type": "learning_block_abandoned",
                "block_id": started["block_id"],
                "topic": started["topic"],
                "reason": reason,
                "abandoned_at": self._format_utc(self.cfg.clock()),
                "credit_delta": 0,
                "owner_ack": True,
            }
            write_json(abandoned_path, record)
            return {
                "ok": True,
                "status": "abandoned",
                "block_id": started["block_id"],
                "credits_minted": 0,
                "record": record,
            }
        except LearningStateError as exc:
            return {"ok": False, "error": "learning_state_invalid", "message": str(exc)}
        except (OSError, PathError, ValueError, TypeError, json.JSONDecodeError) as exc:
            return {"ok": False, "error": "invalid_args", "message": str(exc)}

    def snapshot(self) -> BudgetSnapshot:
        try:
            activation_path = assert_under(self.activation_path, self.cfg.hai_home)
            activation = read_json(activation_path, None)
            if not isinstance(activation, dict):
                raise LearningStateError("learning activation record is missing or invalid")
            if activation.get("record_type") != "learning_budget_activated":
                raise LearningStateError("learning activation record has an invalid type")
            policy_version = activation.get("policy_version")
            if type(policy_version) is not int or policy_version < 2:
                raise LearningStateError("learning activation policy version is invalid")
            self._parse_utc(activation.get("activated_at"), "learning activation time")
            # The durable activation record is the source of truth for the opening balance.
            # Comparing it to the current policy constant would brick every writer lease the
            # moment the owner retunes the policy, because writer leases fail closed on
            # LearningStateError. Tamper detection stays: it must be a non-negative int.
            initial = activation.get("credit_delta")
            if type(initial) is not int or initial < 0:
                raise LearningStateError("learning activation credit delta is invalid")

            completed = 0
            earned = 0
            for _, record, _ in self._blocks():
                if record is not None:
                    delta = record["credit_delta"]
                    completed += 1
                    earned += delta

            spent = 0
            balance = initial + earned - spent
            if balance < 0:
                raise LearningStateError("writer credit ledger has a negative balance")
            return BudgetSnapshot(
                credit_balance=balance,
                initial_credits=initial,
                completed_blocks=completed,
                writer_credits_earned=earned,
                writer_credits_spent=spent,
            )
        except LearningStateError:
            raise
        except (OSError, PathError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise LearningStateError(str(exc)) from exc

    def worker_capacity(self, now: float | None = None) -> WorkerCapacity:
        """How many writer leases may be active at once.

        **This must never raise.** Writer admission fails closed on exceptions, so a throwing
        capacity function would lock the owner out of their own system — which is exactly what
        happened on 2026-08-09 when the credit ledger hit zero (03_HAI/fails/0004). A broken
        ledger therefore narrows capacity to the floor rather than shutting the door.
        """
        floor = int(LEARNING_POLICY["worker_capacity_floor"])
        cap = max(floor, int(LEARNING_POLICY["worker_capacity_cap"]))
        per_block = int(LEARNING_POLICY["worker_slots_per_learning_block"])

        degraded: str | None = None
        completed = 0
        try:
            completed = sum(1 for _, record, _ in self._blocks() if record is not None)
        except (LearningStateError, OSError, PathError, ValueError, TypeError) as exc:
            degraded = str(exc)

        earned = min(completed * per_block, cap - floor)
        base = floor + earned

        suspension = limits_logbook.active_suspension(
            self.cfg.hai_home, now if now is not None else self.cfg.clock()
        )
        capacity = cap if suspension is not None else base

        return WorkerCapacity(
            capacity=capacity,
            floor=floor,
            cap=cap,
            earned_slots=earned,
            completed_blocks=completed,
            blocks_until_next_slot=None if base >= cap else 1,
            suspension=suspension.as_dict() if suspension is not None else None,
            degraded_reason=degraded,
        )

    def status(self, now: float | None = None) -> dict[str, Any]:
        capacity = self.worker_capacity(now).as_dict()
        try:
            snapshot = self.snapshot()
            active = self._active_block()
        except LearningStateError as exc:
            return {
                "ok": False,
                "error": "learning_state_invalid",
                "message": str(exc),
                "minimum_learning_minutes": LEARNING_POLICY["minimum_learning_minutes"],
                "active_learning_block": None,
                "earliest_completion_at": None,
                # Capacity survives an invalid ledger on purpose; see worker_capacity().
                **capacity,
            }
        return {
            "ok": True,
            **snapshot.as_dict(),
            **capacity,
            "minimum_learning_minutes": LEARNING_POLICY["minimum_learning_minutes"],
            "active_learning_block": active,
            "earliest_completion_at": active.get("minimum_completion_at") if active else None,
        }
