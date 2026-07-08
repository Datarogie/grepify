"""Tests for scripts/commit_pipeline_data.py (GRP-06 pipeline data-commit glue)."""

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

    data_dir = repo / "data" / "runs"
    data_dir.mkdir(parents=True)
    (data_dir / "run-1.json").write_text("{}", encoding="utf-8")

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


def test_nothing_to_commit_when_no_new_data(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "work")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "data").mkdir()
    (repo / "data" / "runs.json").write_text("{}", encoding="utf-8")
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
