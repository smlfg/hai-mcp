from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_stdio_smoke_health_check_accepts_resolved_path_alias(tmp_path: Path) -> None:
    from scripts.stdio_smoke import _same_resolved_path

    real_home = tmp_path / "real-home"
    real_home.mkdir()
    alias_home = tmp_path / "alias-home"
    alias_home.symlink_to(real_home, target_is_directory=True)

    assert _same_resolved_path(alias_home, real_home) is True


def test_stdio_smoke_script_writes_artifact(tmp_path: Path) -> None:
    """Slice-3: run the stdio smoke script against an isolated HAI_HOME."""
    hai_home = tmp_path / "hai-home"
    hai_home.mkdir()
    artifact_dir = tmp_path / "artifacts"
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
            "--artifact-dir",
            str(artifact_dir),
        ],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ},
    )
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr
    artifact = artifact_dir / "latest.json"
    assert artifact.is_file()
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert payload["ok"] is True, payload
    assert payload["env_boundary"]["touches_live_dot_hai"] is False
    assert payload["env_boundary"]["HAI_HOME"] == str(hai_home)
    tools_step = next(s for s in payload["steps"] if s["step"] == "list_tools")
    assert tools_step["tool_count"] == 26
    assert tools_step["missing"] == []
    close_step = next(s for s in payload["steps"] if s["step"] == "hai_close_mission_completed")
    assert close_step["ok"] is True
