"""Tests for scripts/commit_pipeline_data.py (GRP-06 pipeline data-commit glue).

Production usage points ``--repo-dir`` at the `data`-branch worktree, whose
top level *is* the data content (no nested ``data/`` prefix) - these tests
mirror that shape rather than the old same-repo-as-main layout.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "commit_pipeline_data.py"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_commits_and_pushes_new_data_with_skip_ci(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True)

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "work")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "seed").write_text("s", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "-u", "origin", "work")

    runs_dir = repo / "runs"
    runs_dir.mkdir(parents=True)
    (runs_dir / "run-1.json").write_text("{}", encoding="utf-8")

    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--branch", "work"],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "committed" in result.stdout

    log = subprocess.run(
        ["git", "-C", str(remote), "log", "--oneline", "work"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "[skip ci]" in log.stdout


def test_repo_dir_flag_commits_from_a_different_cwd(tmp_path: Path) -> None:
    """Mirrors the Makefile invocation: cwd is the repo root, --repo-dir points
    at a nested worktree directory (the `data`-branch checkout in production)."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True)

    data_repo = tmp_path / "workspace" / "data"
    data_repo.mkdir(parents=True)
    _git(data_repo, "init", "-q", "-b", "data")
    _git(data_repo, "config", "user.email", "t@example.com")
    _git(data_repo, "config", "user.name", "Test")
    (data_repo / "seed").write_text("s", encoding="utf-8")
    _git(data_repo, "add", ".")
    _git(data_repo, "commit", "-qm", "init")
    _git(data_repo, "remote", "add", "origin", str(remote))
    _git(data_repo, "push", "-q", "-u", "origin", "data")

    (data_repo / "runs.json").write_text("{}", encoding="utf-8")

    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo-dir", str(data_repo), "--branch", "data"],
        cwd=tmp_path / "workspace",
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "committed" in result.stdout

    log = subprocess.run(
        ["git", "-C", str(remote), "log", "--oneline", "data"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "[skip ci]" in log.stdout


def test_nothing_to_commit_when_no_new_data(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "work")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "runs.json").write_text("{}", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")

    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--branch", "work"],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "nothing to commit" in result.stdout
