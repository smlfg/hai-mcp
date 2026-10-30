"""Owner-managed, bounded capacity exception; invalid policy always means baseline."""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT = "/Users/samuelfleig/Projects/01_WORK/Jobs_Selbständigkeit_Geldverdienen/BRUEGGEN"


def capacity_window(home: Path) -> dict[str, Any]:
    path = home / "CAPACITY_WINDOW.json"
    try:
        if path.is_symlink():
            raise ValueError("policy symlink")
        policy = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(policy, dict) or policy.get("enabled") is not True:
            raise ValueError("not enabled")
        for key in ("max_active_lanes", "max_codex_writers"):
            if type(policy.get(key)) is not int or policy[key] != 5:
                raise ValueError("capacity must be exactly five")
        if policy.get("project_path") != PROJECT:
            raise ValueError("wrong project")
        stamps = []
        for key in ("starts_at", "expires_at"):
            value = policy[key]
            parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
                raise ValueError("noncanonical UTC timestamp")
            stamps.append(parsed.timestamp())
        start, end = stamps
        if not 0 < end - start <= 3 * 60 * 60 or not start <= time.time() < end:
            raise ValueError("outside bounded window")
        fingerprint = hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return {"active": True, "path": str(path), "fingerprint": fingerprint,
                "starts_at": policy["starts_at"], "expires_at": policy["expires_at"],
                "project_path": PROJECT, "max_active_lanes": 5, "max_codex_writers": 5}
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        return {"active": False, "path": str(path)}
