from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_ARTIFACT_DIR = "Projek-Managment"
MAX_ACTIVE_LANES = 2
SERVER_NAME = "hai-mcp"
SERVER_VERSION = "0.1.0"

# Owner gate: the owner is a separate principal from the agent (see owner_gate.py).
DEFAULT_OWNER_GATE = "nonce"  # "nonce" (one-time code via owner channel) | "ack_legacy" (honor system)
DEFAULT_OWNER_CHANNEL = "file"  # "file" (HAI_OWNER_HOME) | "ntfy" (push notification)
DEFAULT_OWNER_NTFY_URL = "https://ntfy.sh"
DEFAULT_OWNER_CODE_TTL_SECONDS = 600
_OWNER_GATE_ALIASES = {"nonce": "nonce", "ack": "ack_legacy", "ack_legacy": "ack_legacy"}


@dataclass(frozen=True)
class Config:
    hai_home: Path
    artifact_dir_name: str = DEFAULT_ARTIFACT_DIR
    max_active_lanes: int = MAX_ACTIVE_LANES
    owner_gate: str = DEFAULT_OWNER_GATE
    owner_channel: str = DEFAULT_OWNER_CHANNEL
    owner_home: Path | None = None  # None → ~/.hai-owner (must NOT be inside hai_home)
    owner_ntfy_url: str = DEFAULT_OWNER_NTFY_URL
    owner_ntfy_topic: str | None = None
    owner_ntfy_token: str | None = None
    owner_code_ttl_seconds: int = DEFAULT_OWNER_CODE_TTL_SECONDS
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_env(cls) -> Config:
        raw = os.environ.get("HAI_HOME", "").strip()
        home = Path(raw).expanduser().resolve() if raw else (Path.home() / ".hai").resolve()
        warnings: list[str] = []

        gate_raw = os.environ.get("HAI_OWNER_GATE", DEFAULT_OWNER_GATE).strip().lower() or DEFAULT_OWNER_GATE
        gate = _OWNER_GATE_ALIASES.get(gate_raw)
        if gate is None:
            # Unknown value → fail closed to the secure mode, but say so in hai_health.
            warnings.append(f"HAI_OWNER_GATE={gate_raw!r} unknown; using 'nonce'")
            gate = DEFAULT_OWNER_GATE

        channel = os.environ.get("HAI_OWNER_CHANNEL", DEFAULT_OWNER_CHANNEL).strip().lower() or DEFAULT_OWNER_CHANNEL
        if channel not in {"file", "ntfy"}:
            warnings.append(f"HAI_OWNER_CHANNEL={channel!r} unknown; using 'file'")
            channel = DEFAULT_OWNER_CHANNEL

        owner_home_raw = os.environ.get("HAI_OWNER_HOME", "").strip()
        owner_home = Path(owner_home_raw).expanduser().resolve() if owner_home_raw else None

        ttl_raw = os.environ.get("HAI_OWNER_CODE_TTL", "").strip()
        ttl = DEFAULT_OWNER_CODE_TTL_SECONDS
        if ttl_raw:
            try:
                ttl = int(ttl_raw)
            except ValueError:
                warnings.append(f"HAI_OWNER_CODE_TTL={ttl_raw!r} is not an integer; using {ttl}")
            else:
                if ttl < 30:
                    warnings.append(f"HAI_OWNER_CODE_TTL={ttl} too small; using 30")
                    ttl = 30

        return cls(
            hai_home=home,
            owner_gate=gate,
            owner_channel=channel,
            owner_home=owner_home,
            owner_ntfy_url=os.environ.get("HAI_OWNER_NTFY_URL", DEFAULT_OWNER_NTFY_URL).strip() or DEFAULT_OWNER_NTFY_URL,
            owner_ntfy_topic=os.environ.get("HAI_OWNER_NTFY_TOPIC", "").strip() or None,
            owner_ntfy_token=os.environ.get("HAI_OWNER_NTFY_TOKEN", "").strip() or None,
            owner_code_ttl_seconds=ttl,
            warnings=tuple(warnings),
        )

    def resolved_owner_home(self) -> Path:
        return (self.owner_home or (Path.home() / ".hai-owner")).expanduser().resolve()


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
