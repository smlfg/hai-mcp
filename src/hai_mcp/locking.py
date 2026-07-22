from __future__ import annotations

import contextlib
import fcntl
from pathlib import Path

from hai_mcp.paths import assert_under


@contextlib.contextmanager
def mission_state_lock(home: Path):
    """Serialize mission open/authorize/recontract/close mutations (Unix/macOS)."""
    lock_path = home / ".mission.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    assert_under(lock_path, home)  # reject a symlinked .mission.lock escaping HAI_HOME
    with open(lock_path, "a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
