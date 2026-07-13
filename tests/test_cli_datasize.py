"""GRP-63: ``grepify datasize`` CLI wiring - exit codes + output for each
threshold band, driven through small ``--warn-bytes``/``--fail-bytes``
overrides rather than real 100/200 MB fixture data."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from grepify.cli import app
from grepify.paths import DataLayout

runner = CliRunner()


def _invoke(data_root: Path, *args: str) -> object:
    return runner.invoke(app, ["--data-root", str(data_root), "datasize", *args])


def _write(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def test_under_warn_exits_zero_with_no_warn_line(tmp_path: Path) -> None:
    layout = DataLayout(tmp_path / "data")
    _write(layout.items_dir / "2026" / "07" / "13.jsonl", 5)

    result = _invoke(tmp_path / "data", "--warn-bytes", "10", "--fail-bytes", "20")
    assert result.exit_code == 0
    assert "WARN" not in result.stdout
    assert "data size:" in result.stdout


def test_warn_band_exits_zero_with_warn_line(tmp_path: Path) -> None:
    layout = DataLayout(tmp_path / "data")
    _write(layout.items_dir / "2026" / "07" / "13.jsonl", 15)

    result = _invoke(tmp_path / "data", "--warn-bytes", "10", "--fail-bytes", "20")
    assert result.exit_code == 0
    assert "WARN: data size:" in result.stdout


def test_over_fail_exits_nonzero(tmp_path: Path) -> None:
    layout = DataLayout(tmp_path / "data")
    _write(layout.items_dir / "2026" / "07" / "13.jsonl", 25)

    result = _invoke(tmp_path / "data", "--warn-bytes", "10", "--fail-bytes", "20")
    assert result.exit_code == 1
    assert "FAIL: data size:" in result.stdout


def test_missing_data_root_is_zero_and_exits_zero(tmp_path: Path) -> None:
    result = _invoke(tmp_path / "does-not-exist", "--warn-bytes", "10", "--fail-bytes", "20")
    assert result.exit_code == 0
    assert "0.0 MB" in result.stdout


def test_default_thresholds_are_used_when_not_overridden(tmp_path: Path) -> None:
    layout = DataLayout(tmp_path / "data")
    _write(layout.items_dir / "2026" / "07" / "13.jsonl", 5)

    result = _invoke(tmp_path / "data")
    assert result.exit_code == 0
    assert "warn>=100 MB fail>=200 MB" in result.stdout
