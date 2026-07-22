from __future__ import annotations

import json

from hai_mcp.eval_impact import CELLS, run_impact_eval_v1, write_run_artifact


def test_impact_eval_v1_all_six_cells_hard_pass() -> None:
    summary = run_impact_eval_v1()
    artifact = write_run_artifact(summary)
    disk = json.loads(artifact.read_text(encoding="utf-8"))
    assert disk["cell_count"] == 6 == len(CELLS)
    assert disk["invalid"] == 0, disk
    assert disk["hard_pass"] == 6, disk
    assert disk["ok"] is True
    assert disk["provider"] is None
    assert disk["touches_live_dot_hai"] is False
