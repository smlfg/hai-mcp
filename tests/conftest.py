from __future__ import annotations

import os
from pathlib import Path

import pytest

ISOLATED_HAI_HOME = "/tmp/hai-mcp-composer-core-fix-20260722"
ISOLATED_UV_CACHE = "/tmp/hai-mcp-uv-cache"


@pytest.fixture(scope="session", autouse=True)
def isolated_hai_home() -> None:
    """Every test inherits an isolated HAI_HOME; never touch live ~/.hai."""
    os.environ["HAI_HOME"] = ISOLATED_HAI_HOME
    os.environ["UV_CACHE_DIR"] = ISOLATED_UV_CACHE
    Path(ISOLATED_HAI_HOME).mkdir(parents=True, exist_ok=True)
