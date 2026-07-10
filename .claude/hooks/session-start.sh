#!/usr/bin/env bash
# SessionStart hook: make a Claude Code (web or local) session ready to work.
#   1. Pin the git identity + activate the commit-hygiene hooks so no commit is
#      ever authored as an AI identity or carries attribution / em-en dashes.
#   2. Install dev dependencies so `make check` (lint + mypy + pytest) runs.
# Synchronous + idempotent.
set -euo pipefail

cd "${CLAUDE_PROJECT_DIR:-.}"

# 1) Identity + hooks (always, local and remote).
bash scripts/setup-git-identity.sh

# 2) Dependencies. Skip if uv is unavailable (nothing to do without it).
if command -v uv >/dev/null 2>&1; then
  make install
else
  echo "uv not found; skipping dependency install" >&2
fi
