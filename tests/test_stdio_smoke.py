from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ARTIFACT = REPO / "evals" / "stdio_smoke" / "latest.json"


def test_stdio_smoke_script_writes_artifact(tmp_path: Path) -> None:
    """Slice-3: run the stdio smoke script against an isolated HAI_HOME."""
    hai_home = tmp_path / "hai-home"
    hai_home.mkdir()
    uv = shutil.which("uv")
    assert uv, "uv is required to run hai-mcp smoke"
    proc = subprocess.run(
        [
            uv,
            "run",
            "--directory",
            str(REPO),
            "python",
            str(REPO / "scripts" / "stdio_smoke.py"),
            "--hai-home",
            str(hai_home),
        ],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ},
    )
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr
    assert ARTIFACT.is_file()
    payload = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    assert payload["ok"] is True, payload
    assert payload["env_boundary"]["touches_live_dot_hai"] is False
    assert payload["env_boundary"]["HAI_HOME"] == str(hai_home)
    tools_step = next(s for s in payload["steps"] if s["step"] == "list_tools")
    assert tools_step["tool_count"] == 24
    assert tools_step["missing"] == []
