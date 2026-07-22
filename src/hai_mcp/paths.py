from __future__ import annotations

import os
from pathlib import Path

from hai_mcp.config import Config


class PathError(ValueError):
    def __init__(self, code: str, message: str, *, path: str | None = None, root: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path
        self.root = root

    def as_dict(self) -> dict[str, str]:
        out: dict[str, str] = {"error": self.code, "message": self.message}
        if self.path is not None:
            out["path"] = self.path
        if self.root is not None:
            out["root"] = self.root
        return out


def resolve_project_path(project_path: str | None) -> Path | None:
    if project_path is None or not str(project_path).strip():
        return None
    return Path(project_path).expanduser().resolve()


def require_project_path(project_path: str | None) -> Path:
    path = resolve_project_path(project_path)
    if path is None:
        raise PathError("invalid_args", "project_path is required")
    if not path.exists():
        raise PathError("missing_project", f"project path does not exist: {path}", path=str(path))
    if not path.is_dir():
        raise PathError("missing_project", f"project path is not a directory: {path}", path=str(path))
    return path


def artifact_dir(cfg: Config, project: Path) -> Path:
    return project / cfg.artifact_dir_name


def confined_artifact_dir(cfg: Config, project: Path) -> Path:
    """Resolve artifact dir under *project*; reject symlink escape outside project root."""
    project_r = real_path(project)
    ad = project / cfg.artifact_dir_name
    if ad.is_symlink():
        target = real_path(ad)
        try:
            target.relative_to(project_r)
        except ValueError as exc:
            raise PathError(
                "path_outside_root",
                "artifact directory symlink escapes project root",
                path=str(target),
                root=str(project_r),
            ) from exc
        return target
    return assert_under(ad, project_r)


def ensure_artifact_dir(cfg: Config, project: Path) -> Path:
    d = confined_artifact_dir(cfg, project)
    d.mkdir(parents=True, exist_ok=True)
    return d


def real_path(path: Path) -> Path:
    return Path(os.path.realpath(path))


def assert_under(path: Path, root: Path) -> Path:
    """Resolve *path* and ensure its real path stays under *root* (symlink-safe)."""
    resolved = real_path(path.expanduser())
    root_r = real_path(root)
    try:
        resolved.relative_to(root_r)
    except ValueError as exc:
        raise PathError(
            "path_outside_root",
            f"path {resolved} is outside allowed root {root_r}",
            path=str(resolved),
            root=str(root_r),
        ) from exc
    return resolved


def resolve_under_root(root: Path, candidate: str | Path, *, relative_to_root: bool = True) -> Path:
    """Central fail-closed resolver.

    Rejects null bytes / invalid types before any filesystem call, interprets a
    relative *candidate* under *root* (never the process CWD), then proves the
    real path stays under *root* via symlink-safe resolution. Raises PathError on
    escape. The caller checks existence/type AFTER this returns.
    """
    if candidate is None or not isinstance(candidate, (str, Path)):
        raise PathError("invalid_args", "path must be a string")
    raw = str(candidate)
    if "\x00" in raw:
        raise PathError("invalid_args", "path contains a null byte")
    p = Path(raw).expanduser()
    if relative_to_root and not p.is_absolute():
        p = Path(root) / p
    return assert_under(p, root)


def assert_relative_allowed(path: Path, project_root: Path, allowed_paths: list[str]) -> Path:
    """Ensure *path* is under *project_root* and matches one allowed relative prefix."""
    confined = assert_under(path, project_root)
    rel = confined.relative_to(real_path(project_root))
    rel_posix = rel.as_posix()
    if not allowed_paths:
        return confined
    for prefix in allowed_paths:
        norm = prefix.strip().strip("/")
        if not norm:
            continue
        if rel_posix == norm or rel_posix.startswith(norm + "/"):
            return confined
    raise PathError(
        "path_outside_root",
        f"path {confined} is outside allowed project prefixes {allowed_paths}",
        path=str(confined),
        root=str(real_path(project_root)),
    )
