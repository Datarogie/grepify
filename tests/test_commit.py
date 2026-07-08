"""Tests for the rebase-retry data-commit helper (GRP-03)."""

from __future__ import annotations

import subprocess
import types
from pathlib import Path

import pytest

from grepify.repository import commit as commit_mod
from grepify.repository.commit import CommitError, commit_data


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "work")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "Test")
    return path


def test_noop_when_nothing_staged(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    data = repo / "data.jsonl"
    data.write_text("row\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")

    # File is already committed and unchanged -> nothing to stage.
    assert commit_data(repo, [data], "noop [skip ci]", branch="work", push=False) is False


def test_commits_and_pushes_to_remote(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True)

    repo = _init_repo(tmp_path / "repo")
    (repo / "seed").write_text("s", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "-u", "origin", "work")

    data = repo / "data.jsonl"
    data.write_text("new row\n", encoding="utf-8")
    assert commit_data(repo, [data], "data [skip ci]", branch="work") is True

    log = subprocess.run(
        ["git", "-C", str(remote), "log", "--oneline", "work"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "data [skip ci]" in log.stdout


def test_raises_commit_error_after_retry_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    data = repo / "data.jsonl"
    data.write_text("row\n", encoding="utf-8")

    def fake_run(
        argv: list[str],
        *,
        cwd: object = None,
        capture_output: bool = False,
        text: bool = False,
        check: bool = False,
    ) -> types.SimpleNamespace:
        sub = argv[1]
        # `git diff --cached --quiet` -> rc 1 signals staged changes present.
        # `git push` -> always fails, forcing the rebase-retry loop.
        rc = 1 if sub in ("diff", "push") else 0
        if check and rc:
            raise subprocess.CalledProcessError(rc, argv)
        return types.SimpleNamespace(returncode=rc, stdout="", stderr="rejected")

    monkeypatch.setattr(commit_mod.subprocess, "run", fake_run)

    with pytest.raises(CommitError):
        commit_data(repo, [data], "msg [skip ci]", branch="work", max_attempts=3)
