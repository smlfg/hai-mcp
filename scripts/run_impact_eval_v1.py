#!/usr/bin/env python3
"""Run hai_mcp_impact_v1 hard-assertion eval (no LLM / no MiniMax)."""

from __future__ import annotations

import json
import sys

from hai_mcp.eval_impact import run_impact_eval_v1, write_run_artifact


def main() -> int:
    summary = run_impact_eval_v1()
    path = write_run_artifact(summary)
    print(
        json.dumps(
            {
                "artifact": str(path),
                "ok": summary["ok"],
                "hard_pass": summary["hard_pass"],
                "hard_fail": summary["hard_fail"],
                "invalid": summary["invalid"],
            },
            indent=2,
        )
    )
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
