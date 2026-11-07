#!/usr/bin/env bash
# Reduce smlfg/hai-mcp from 18 to 2 branches: main + ccr-d8ebee1c-ptmwac.
# Every deleted branch is first saved as tag archive/<branch>, so every commit stays restorable:
#   git checkout -b <branch> archive/<branch>
# Deleting a branch with an open PR closes that PR (#1, #2, #3, #8, #9, #12, #17);
# their content lives on in ccr-d8ebee1c-ptmwac.
set -euo pipefail

BRANCHES=(
  bot/cli/tool-descriptions            # merged (PR #17)
  ci/tests                             # merged (PR #12)
  codex/readable-owner-dialog          # merged
  cursor/add-composer-subagent-8d5e    # merged (PR #1)
  cursor/slice4-freeze-packet-4ec8     # rebased (PR #3), subset of stdio-smoke
  cursor/stdio-smoke-latest-json-e083  # rebased
  public/03-learning-budget            # rebased (PR #9), subset of learning-ledger
  cursor/learning-ledger-validation-dfbc # rebased
  public/02-owner-instrumente          # rebased (PR #8), identical to loving-galileo
  claude/loving-galileo-gqb577         # identical to public/02
  cursor/hai-mcp-logic-bugs-99fa       # rebased (public/02 + fix)
  cursor/readme-docs-images-52eb       # partially taken (PR #2): docs index + SVG title
  cursor/touchid-owner-gate-aec9       # already in main (PR #13)
  public/01-typed-tool-schemas         # already in main (PR #7)
  cursor/central-hai-core-454b         # already in main (PR #5, squash)
  cursor/central-hai-core-3299         # superseded by PR #5 (PR #4 closed unmerged)
)

git fetch origin --prune
for b in "${BRANCHES[@]}"; do
  git tag -f "archive/$b" "origin/$b"
done
git push origin $(printf 'refs/tags/archive/%s ' "${BRANCHES[@]}")
git push origin --delete "${BRANCHES[@]}"
git ls-remote --heads origin
