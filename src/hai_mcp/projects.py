from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from hai_mcp.config import Config
from hai_mcp.locking import mission_state_lock
from hai_mcp.paths import PathError, assert_under, real_path, require_project_path
from hai_mcp.storage import read_json, write_json

_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
_REGISTRY_VERSION = 1


def validate_slug(value: object, field: str) -> tuple[bool, str]:
    """Fail-closed slug: ^[a-z][a-z0-9-]{1,63}$ — no strip, no coercion."""
    if not isinstance(value, str):
        return False, f"{field} must be a string"
    raw = value
    if not raw:
        return False, f"{field} is required"
    if "\x00" in raw or "/" in raw or "\\" in raw or ".." in raw:
        return False, f"invalid {field}: path traversal or unsafe characters rejected"
    if not _SLUG_RE.match(raw):
        return False, f"invalid {field}: must match ^[a-z][a-z0-9-]{{1,63}}$"
    return True, ""


def registry_path(cfg: Config) -> Path:
    path = cfg.hai_home / "projects.json"
    assert_under(path, cfg.hai_home)
    return path


def load_registry(cfg: Config) -> dict[str, Any]:
    data = read_json(registry_path(cfg), {"version": _REGISTRY_VERSION, "projects": {}})
    if not isinstance(data.get("projects"), dict):
        data["projects"] = {}
    data["version"] = _REGISTRY_VERSION
    return data


def save_registry(cfg: Config, data: dict[str, Any]) -> None:
    path = registry_path(cfg)
    write_json(path, data)


def list_projects(cfg: Config) -> list[dict[str, Any]]:
    reg = load_registry(cfg)
    out: list[dict[str, Any]] = []
    for pid in sorted(reg.get("projects", {})):
        mounts = reg["projects"][pid].get("mounts", {})
        out.append({"project_id": pid, "device_ids": sorted(mounts.keys())})
    return out


def has_mounts(cfg: Config, project_id: str) -> bool:
    reg = load_registry(cfg)
    proj = reg.get("projects", {}).get(project_id)
    if not proj:
        return False
    return bool(proj.get("mounts"))


def resolve_mount(cfg: Config, project_id: str, device_id: str) -> Path:
    ok, msg = validate_slug(project_id, "project_id")
    if not ok:
        raise PathError("invalid_args", msg)
    ok, msg = validate_slug(device_id, "device_id")
    if not ok:
        raise PathError("invalid_args", msg)
    reg = load_registry(cfg)
    proj = reg.get("projects", {}).get(project_id)
    if not proj:
        raise PathError("invalid_args", f"unknown project_id: {project_id}")
    mount = (proj.get("mounts") or {}).get(device_id)
    if not mount:
        raise PathError("invalid_args", f"no mount for device_id {device_id} on project {project_id}")
    root = mount.get("root")
    if not root:
        raise PathError("invalid_args", f"mount root missing for {project_id}/{device_id}")
    return Path(root)


def register_mount(
    cfg: Config,
    project_id: str,
    device_id: str,
    root_path: str,
    owner_ack: Any,
    reason: str,
) -> dict[str, Any]:
    ok, msg = validate_slug(project_id, "project_id")
    if not ok:
        return {"ok": False, "error": "invalid_args", "message": msg}
    ok, msg = validate_slug(device_id, "device_id")
    if not ok:
        return {"ok": False, "error": "invalid_args", "message": msg}

    if owner_ack is not True:
        return {
            "ok": False,
            "error": "owner_gate_required",
            "message": "register_mount requires owner_ack=true (literal boolean)",
        }

    reason = str(reason or "")
    if not reason.strip():
        return {"ok": False, "error": "invalid_args", "message": "reason is required"}

    try:
        resolved = real_path(require_project_path(root_path))
    except PathError as exc:
        return {"ok": False, "error": exc.code, "message": exc.message}

    updated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    with mission_state_lock(cfg.hai_home):
        reg = load_registry(cfg)
        projects = reg.setdefault("projects", {})
        proj = projects.setdefault(
            project_id,
            {"project_id": project_id, "mounts": {}},
        )
        mounts = proj.setdefault("mounts", {})
        is_update = device_id in mounts
        mounts[device_id] = {
            "root": str(resolved),
            "updated_at": updated_at,
        }
        save_registry(cfg, reg)

    return {
        "ok": True,
        "project_id": project_id,
        "device_id": device_id,
        "root": str(resolved),
        "updated": is_update,
    }
