"""Tests for scripts/ensure-data-branch.sh (GRP-06 data-branch checkout).

Exercises both paths against real local git remotes (no network): bootstrapping
an empty orphan `data` branch on first run, and checking out an existing one on
a later run.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "ensure-data-branch.sh"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_main_checkout(remote: Path, path: Path) -> Path:
    subprocess.run(["git", "clone", "-q", str(remote), str(path)], check=True, capture_output=True)
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "Test")
    # actions/checkout always leaves a valid checked-out branch (unlike a bare
    # `git clone` here, whose remote HEAD symref was never pointed at `main`).
    _git(path, "checkout", "main")
    return path


def _log(remote: Path, branch: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(remote), "log", "--oneline", branch],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def test_bootstraps_orphan_branch_when_missing(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True)

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "config", "user.email", "t@example.com")
    _git(seed, "config", "user.name", "Test")
    (seed / "README.md").write_text("hello", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-qm", "init")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-q", "-u", "origin", "main")

    main_checkout = _init_main_checkout(remote, tmp_path / "checkout")
    subprocess.run(
        ["bash", str(SCRIPT)], cwd=main_checkout, check=True, capture_output=True, text=True
    )

    data_dir = main_checkout / "data"
    assert data_dir.is_dir()
    branch = subprocess.run(
        ["git", "-C", str(data_dir), "branch", "--show-current"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert branch == "data"
    assert [p for p in data_dir.glob("*") if p.name != ".git"] == []  # orphan branch starts empty

    remote_log = _log(remote, "data")
    assert "[skip ci]" in remote_log


def test_checks_out_existing_branch_as_worktree(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True)

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "config", "user.email", "t@example.com")
    _git(seed, "config", "user.name", "Test")
    (seed / "README.md").write_text("hello", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-qm", "init")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-q", "-u", "origin", "main")

    # Pre-populate `data` on the remote, independent of `main`'s history.
    data_seed = tmp_path / "data-seed"
    data_seed.mkdir()
    _git(data_seed, "init", "-q", "-b", "data")
    _git(data_seed, "config", "user.email", "t@example.com")
    _git(data_seed, "config", "user.name", "Test")
    (data_seed / "runs.json").write_text("{}", encoding="utf-8")
    _git(data_seed, "add", ".")
    _git(data_seed, "commit", "-qm", "chore(data): seed [skip ci]")
    _git(data_seed, "remote", "add", "origin", str(remote))
    _git(data_seed, "push", "-q", "-u", "origin", "data")

    main_checkout = _init_main_checkout(remote, tmp_path / "checkout")
    subprocess.run(
        ["bash", str(SCRIPT)], cwd=main_checkout, check=True, capture_output=True, text=True
    )

    data_dir = main_checkout / "data"
    assert data_dir.is_dir()
    assert (data_dir / "runs.json").exists()

    branch = subprocess.run(
        ["git", "-C", str(data_dir), "branch", "--show-current"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert branch == "data"

    # No stray extra commit was made on the existing branch.
    assert _log(remote, "data").count("\n") == 1
