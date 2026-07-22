"""Fail-closed validation for MCP JSON boundary values (no silent coercion)."""

from __future__ import annotations

import math
from typing import Any


def _invalid(name: str, detail: str) -> dict[str, Any]:
    return {"ok": False, "error": "invalid_args", "message": f"{name} {detail}"}


def strict_int(value: Any, name: str, *, min_value: int | None = None) -> tuple[int | None, dict[str, Any] | None]:
    if not isinstance(value, int) or isinstance(value, bool):
        return None, _invalid(name, "must be a literal integer")
    if min_value is not None and value < min_value:
        return None, _invalid(name, f"must be >= {min_value}")
    return value, None


def strict_optional_time_limit_hours(value: Any) -> tuple[float | None, dict[str, Any] | None]:
    if value is None:
        return None, None
    if isinstance(value, bool):
        return None, _invalid("time_limit_hours", "must be a literal number")
    if isinstance(value, int):
        if value < 1:
            return None, _invalid("time_limit_hours", "must be >= 1")
        return float(value), None
    if isinstance(value, float):
        if value < 1 or not math.isfinite(value):
            return None, _invalid("time_limit_hours", "must be a finite number >= 1")
        return value, None
    return None, _invalid("time_limit_hours", "must be a literal number")


def strict_constraint_max_parallel(value: Any) -> tuple[int | None, dict[str, Any] | None]:
    if value is None:
        return 1, None
    return strict_int(value, "max_parallel_sessions", min_value=1)
