from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from hai_mcp.paths import PathError, assert_under, real_path, require_project_path
from hai_mcp.storage import read_json, write_json

IDENT_RE = re.compile(r"^[a-z][a-z0-9-]{1,63}$")


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def validate_ident(value: str, field: str) -> tuple[bool, str]:
    raw = str(value or "").strip()
    if not raw:
        return False, f"{field} is required"
    if not IDENT_RE.match(raw):
        return (
            False,
            f"{field} must match ^[a-z][a-z0-9-]{{1,63}}$ (got {raw!r})",
        )
    return True, ""


class ProjectStore:
    """Logical project registry with per-device mount paths under HAI_HOME/core/projects.json."""

    def __init__(self, hai_home: Path) -> None:
        self.core_dir = hai_home / "core"
        self.projects_path = self.core_dir / "projects.json"

    def load(self) -> dict[str, Any]:
        return read_json(self.projects_path, {"version": 1, "projects": {}})

    def save(self, data: dict[str, Any]) -> None:
        self.core_dir.mkdir(parents=True, exist_ok=True)
        write_json(self.projects_path, data)

    def get_mount_path(self, project_id: str, device_id: str) -> Path | None:
        ok, _ = validate_ident(project_id, "project_id")
        if not ok:
            return None
        ok, _ = validate_ident(device_id, "device_id")
        if not ok:
            return None
        data = self.load()
        project = data.get("projects", {}).get(project_id)
        if not isinstance(project, dict):
            return None
        mount = project.get("mounts", {}).get(device_id)
        if not isinstance(mount, dict):
            return None
        raw_path = mount.get("path")
        if not raw_path:
            return None
        return Path(str(raw_path))

    def bind_mount(
        self,
        project_id: str,
        device_id: str,
        local_path: str | Path,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Validate and persist a device mount. Returns (mount_record, error_dict)."""
        ok, msg = validate_ident(project_id, "project_id")
        if not ok:
            return None, {"ok": False, "error": "invalid_args", "message": msg}
        ok, msg = validate_ident(device_id, "device_id")
        if not ok:
            return None, {"ok": False, "error": "invalid_args", "message": msg}

        try:
            resolved = require_project_path(str(local_path))
        except PathError as exc:
            return None, {"ok": False, "error": exc.code, "message": exc.message}

        raw = Path(str(local_path)).expanduser()
        if raw.is_symlink():
            try:
                assert_under(real_path(raw), real_path(raw.parent))
            except PathError as exc:
                return None, {"ok": False, "error": exc.code, "message": exc.message}

        data = self.load()
        projects = data.setdefault("projects", {})
        project = projects.setdefault(
            project_id,
            {"project_id": project_id, "mounts": {}},
        )
        mounts = project.setdefault("mounts", {})
        record = {
            "path": str(real_path(resolved)),
            "bound_at": _utc_now(),
        }
        mounts[device_id] = record
        self.save(data)
        return record, None

    def list_projects(self) -> list[dict[str, Any]]:
        data = self.load()
        out: list[dict[str, Any]] = []
        for pid in sorted(data.get("projects", {})):
            mounts = data["projects"][pid].get("mounts", {})
            out.append({"project_id": pid, "device_ids": sorted(mounts.keys())})
        return out
