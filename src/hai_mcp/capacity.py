"""Owner-managed, bounded capacity exception; invalid policy always means baseline."""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT = "/Users/samuelfleig/Projects/01_WORK/Jobs_Selbständigkeit_Geldverdienen/BRUEGGEN"
PREZERO = "/Users/samuelfleig/Projects/01_WORK/HackathonGreenTechHeilbronn/Participants - Future City Hackathon/prezero-routing-hackathon-2026"
# Owner-approved projects for a capacity window (exact canonical paths only).
PROJECTS = frozenset({PROJECT, PREZERO})


def allowed_projects() -> frozenset[str]:
    """Hardcoded allowlist unless HAI_CAPACITY_PROJECTS overrides (OS pathsep, absolute paths)."""
    raw = os.environ.get("HAI_CAPACITY_PROJECTS", "").strip()
    if not raw:
        return PROJECTS
    paths: set[str] = set()
    for part in raw.split(os.pathsep):
        part = part.strip()
        if not part:
            continue
        path = Path(part)
        if not path.is_absolute():
            raise ValueError("HAI_CAPACITY_PROJECTS entries must be absolute paths")
        paths.add(str(path.resolve()))
    if not paths:
        return PROJECTS
    return frozenset(paths)


def capacity_window(home: Path) -> dict[str, Any]:
    path = home / "CAPACITY_WINDOW.json"
    try:
        if path.is_symlink():
            raise ValueError("policy symlink")
        policy = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(policy, dict) or policy.get("enabled") is not True:
            raise ValueError("not enabled")
        for key in ("max_active_lanes", "max_codex_writers"):
            if type(policy.get(key)) is not int or not 3 <= policy[key] <= 5:
                raise ValueError("capacity must be between three and five")
        if policy.get("project_path") not in allowed_projects():
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
                "project_path": policy["project_path"], "max_active_lanes": policy["max_active_lanes"],
                "max_codex_writers": policy["max_codex_writers"]}
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        return {"active": False, "path": str(path)}
