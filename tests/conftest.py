from __future__ import annotations

import os
from pathlib import Path

import pytest

ISOLATED_HAI_HOME = "/tmp/hai-mcp-composer-core-fix-20260722"
# Owner channel isolation: next to the isolated HAI_HOME, never inside it, never ~/.hai-owner.
ISOLATED_OWNER_HOME = "/tmp/hai-mcp-composer-core-fix-20260722-owner"
ISOLATED_UV_CACHE = "/tmp/hai-mcp-uv-cache"


@pytest.fixture(scope="session", autouse=True)
def isolated_hai_home() -> None:
    """Every test (and every subprocess inheriting os.environ) gets isolated HAI_HOME and
    HAI_OWNER_HOME; never touch live ~/.hai or ~/.hai-owner."""
    os.environ["HAI_HOME"] = ISOLATED_HAI_HOME
    os.environ["HAI_OWNER_HOME"] = ISOLATED_OWNER_HOME
    os.environ["UV_CACHE_DIR"] = ISOLATED_UV_CACHE
    Path(ISOLATED_HAI_HOME).mkdir(parents=True, exist_ok=True)
