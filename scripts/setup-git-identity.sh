#!/usr/bin/env bash
# Set the project's git identity + activate the committed commit-hygiene hooks.
#
# Idempotent: safe to run every session (the session-start hook calls it). Keeps
# every commit authored as the project owner rather than an AI/agent identity,
# and turns on `.githooks/` (CLAUDE.md: no AI attribution, no em/en dashes).
#
# Personal GitHub identity for now; this moves to the work email at the GitLab
# cutover (GRP-62).
set -euo pipefail

git config user.name "Datarogie"
git config user.email "42312814+Datarogie@users.noreply.github.com"
git config core.hooksPath .githooks

echo "git identity set to Datarogie <42312814+Datarogie@users.noreply.github.com>; hooks -> .githooks"
