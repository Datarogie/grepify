"""CLI glue: commit the pipeline's JSONL truth data to the `data` branch (GRP-06).

Thin wrapper around :func:`grepify.repository.commit.commit_data` so the
Makefile (and therefore any CI system, GH or GitLab - PRD F-OPS-03) can
trigger a data commit without embedding git plumbing in workflow YAML.
``--repo-dir`` defaults to the current directory but in production points at
the `data`-branch worktree checked out by ``scripts/ensure-data-branch.sh``
(main's ruleset requires PRs, so truth commits go to a dedicated branch
instead - see docs/process.md).

Failure modes
-------------
Delegates entirely to :func:`commit_data`: a non-race git failure or an
exhausted rebase-retry budget propagates and exits non-zero, failing the
pipeline run loudly (no silent data loss).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from grepify.repository.commit import commit_data

DEFAULT_MESSAGE = "chore(data): pipeline run [skip ci]"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--branch", required=True, help="branch to push the data commit to")
    parser.add_argument(
        "--repo-dir",
        type=Path,
        default=Path.cwd(),
        help="git working tree to commit from (the data-branch worktree in production)",
    )
    parser.add_argument(
        "--paths",
        nargs="+",
        type=Path,
        default=[Path()],
        help="paths within --repo-dir to stage (default: everything)",
    )
    parser.add_argument("--message", default=DEFAULT_MESSAGE)
    args = parser.parse_args(argv)

    committed = commit_data(args.repo_dir, args.paths, args.message, branch=args.branch)
    print("committed" if committed else "nothing to commit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
