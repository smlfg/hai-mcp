from __future__ import annotations

import contextlib
import fcntl
import threading
from pathlib import Path

from hai_mcp.paths import assert_under

_lock_depth = threading.local()


@contextlib.contextmanager
def mission_state_lock(home: Path):
    """Serialize mission mutations (Unix/macOS). Reentrant within the same process."""
    depth = getattr(_lock_depth, "depth", 0)
    handle = None
    if depth == 0:
        lock_path = home / ".mission.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        assert_under(lock_path, home)  # reject a symlinked .mission.lock escaping HAI_HOME
        handle = open(lock_path, "a", encoding="utf-8")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        _lock_depth.handle = handle
    try:
        _lock_depth.depth = depth + 1
        yield
    finally:
        _lock_depth.depth = depth
        if depth == 0 and handle is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
            _lock_depth.handle = None
