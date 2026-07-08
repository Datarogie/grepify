#!/usr/bin/env bash
# Ensure the dedicated `data` branch is checked out as a worktree at ./data
# (GRP-06 revision). Main now carries a ruleset requiring PRs with no bypass,
# so the pipeline's JSONL truth commits can no longer land on `main` — they go
# to `data` instead, pushed with the default GITHUB_TOKEN. Creates `data` as
# an empty orphan branch on first run.
#
# Failure modes
# -------------
# Any git command failing (auth, corrupt repo) propagates via `set -e` and
# fails the pipeline run loudly — same posture as commit_data (no silent data
# loss). Run from the repository root of an already-checked-out `main`.
set -euo pipefail

if git ls-remote --exit-code --heads origin data >/dev/null 2>&1; then
  git fetch origin data:refs/remotes/origin/data
  git worktree add data origin/data
  git -C data switch -c data
else
  git worktree add --detach data
  git -C data checkout --orphan data
  git -C data rm -rf . >/dev/null 2>&1 || true
  git -C data commit --allow-empty -m "chore(data): initialize data branch [skip ci]"
  git -C data push -u origin data
fi
