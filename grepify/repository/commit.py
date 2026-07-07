"""Rebase-retry commit of JSONL truth files (PRD §5).

Cron runs must not clobber each other's data commits. Concurrent *runs* are
prevented by the Actions concurrency group (configured in the CI workflow,
GRP-06); this helper is the second guard — when a push races a commit that
landed after our checkout, it rebases onto the remote head and retries with
bounded attempts (no unbounded loops, per the budget-gate discipline).

The SQLite cache is never committed (it is gitignored); only JSONL truth under
the data root is staged.

Failure modes
-------------
- A git command fails for a non-race reason (auth, corrupt repo) → the
  ``subprocess.CalledProcessError`` propagates; the caller fails the run loudly.
- Push keeps losing the race past ``max_attempts`` → :class:`CommitError`.
- Nothing to commit (no new/changed data) → returns ``False``, no commit made.

This module shells out to ``git`` and touches the network on push; it is
exercised by the pipeline in CI rather than by offline unit tests.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

from grepify.errors import GrepifyError


class CommitError(GrepifyError):
    """A data commit could not be pushed within the retry budget."""


def commit_data(  # noqa: PLR0913 - explicit, keyword-only knobs read clearer than a config object
    repo_dir: Path,
    paths: Sequence[Path],
    message: str,
    *,
    branch: str,
    push: bool = True,
    max_attempts: int = 5,
) -> bool:
    """Stage, commit, and (optionally) push data ``paths`` with rebase-retry.

    Returns ``True`` if a commit was created, ``False`` if there was nothing to
    commit. ``message`` should carry ``[skip ci]`` for pipeline data commits so
    the write does not re-trigger the cron workflow (loop guard, GRP-06).
    """
    if not _stage(repo_dir, paths):
        return False

    _git(repo_dir, "commit", "-m", message)
    if not push:
        return True

    for _attempt in range(max_attempts):
        result = subprocess.run(
            ["git", "push", "origin", branch],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return True
        # Lost the race: pull --rebase onto the remote head and retry.
        _git(repo_dir, "pull", "--rebase", "origin", branch)

    raise CommitError(f"push to {branch} failed after {max_attempts} attempts")


def _stage(repo_dir: Path, paths: Sequence[Path]) -> bool:
    """Stage paths; return True if anything is actually staged."""
    _git(repo_dir, "add", "--", *[str(p) for p in paths])
    status = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=repo_dir,
        check=False,
    )
    return status.returncode != 0  # non-zero => there are staged changes


def _git(repo_dir: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo_dir, check=True)
