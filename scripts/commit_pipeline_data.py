"""CLI glue: commit the pipeline's JSONL truth data back to the repo (GRP-06).

Thin wrapper around :func:`grepify.repository.commit.commit_data` so the
Makefile (and therefore any CI system, GH or GitLab — PRD F-OPS-03) can
trigger a data commit without embedding git plumbing in workflow YAML.

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
    parser.add_argument("--message", default=DEFAULT_MESSAGE)
    args = parser.parse_args(argv)

    committed = commit_data(Path.cwd(), [Path("data")], args.message, branch=args.branch)
    print("committed" if committed else "nothing to commit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
