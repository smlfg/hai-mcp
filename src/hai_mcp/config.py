from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ARTIFACT_DIR = "Projek-Managment"
MAX_ACTIVE_LANES = 2
SERVER_NAME = "hai-mcp"
SERVER_VERSION = "0.1.0"


@dataclass(frozen=True)
class Config:
    hai_home: Path
    artifact_dir_name: str = DEFAULT_ARTIFACT_DIR
    max_active_lanes: int = MAX_ACTIVE_LANES

    @classmethod
    def from_env(cls) -> Config:
        raw = os.environ.get("HAI_HOME", "").strip()
        home = Path(raw).expanduser().resolve() if raw else (Path.home() / ".hai").resolve()
        return cls(hai_home=home)


def ensure_hai_home(cfg: Config) -> Path:
    home = cfg.hai_home
    home.mkdir(parents=True, exist_ok=True)
    (home / "inbox").mkdir(exist_ok=True)
    (home / "history" / "checkpoints").mkdir(parents=True, exist_ok=True)
    active = home / "ACTIVE_CONTEXT.json"
    if not active.exists():
        active.write_text(
            '{\n  "version": 1,\n  "focus_id": null,\n  "active": []\n}\n',
            encoding="utf-8",
        )
    owner = home / "OWNER_CONTRACT.json"
    if not owner.exists():
        owner.write_text(
            "{\n"
            '  "version": 1,\n'
            '  "max_active_lanes": 2,\n'
            '  "require_owner_ack_for": ["accept_next_step"]\n'
            "}\n",
            encoding="utf-8",
        )
    return home
