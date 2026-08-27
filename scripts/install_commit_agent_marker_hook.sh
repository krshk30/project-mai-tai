#!/usr/bin/env bash
set -euo pipefail

agent=${1:-}
case "$agent" in
  codex|claude) ;;
  *)
    echo "usage: $0 codex|claude" >&2
    exit 2
    ;;
esac

repo_root=$(git rev-parse --show-toplevel)
test -x "$repo_root/.githooks/commit-msg" || {
  echo "REFUSED: $repo_root/.githooks/commit-msg is absent or not executable" >&2
  exit 1
}

# The repository is shared by many worktrees and by both agents. Keep the
# identity choice in worktree config so configuring Codex cannot relabel a
# Claude worktree (or vice versa).
git config --local extensions.worktreeConfig true
git config --worktree core.hooksPath .githooks
git config --worktree mai-tai.agentMarker "$agent"

echo "commit marker hook installed for $agent in $repo_root"
echo "core.hooksPath=$(git config --worktree --get core.hooksPath)"
echo "mai-tai.agentMarker=$(git config --worktree --get mai-tai.agentMarker)"
