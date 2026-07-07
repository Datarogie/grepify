"""CLI tests (GRP-05): validate exit codes, stub manifests, health."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from grepify.cli import app
from tests.conftest import write_config

runner = CliRunner()

_GROUP_OK = """
    group: ai-research
    name: AI Research
    category: ai
    sources:
      - id: ahead-of-ai
        kind: rss
        url: https://magazine.sebastianraschka.com/feed
"""


def _invoke(config_root: Path, data_root: Path, command: str) -> object:
    return runner.invoke(
        app,
        ["--config-root", str(config_root), "--data-root", str(data_root), command],
    )


def test_validate_ok_exits_zero(tmp_path: Path) -> None:
    cfg = write_config(tmp_path / "sources", groups={"ai-research.yml": _GROUP_OK})
    result = _invoke(cfg, tmp_path / "data", "validate")
    assert result.exit_code == 0
    assert "config ok" in result.stdout


def test_validate_invalid_exits_nonzero(tmp_path: Path) -> None:
    bad = "group: g\nname: G\nsources: []\n"  # missing category
    cfg = write_config(tmp_path / "sources", groups={"g.yml": bad})
    result = _invoke(cfg, tmp_path / "data", "validate")
    assert result.exit_code == 1
    assert "INVALID" in result.stdout


def test_health_without_runs(tmp_path: Path) -> None:
    cfg = write_config(tmp_path / "sources")
    result = _invoke(cfg, tmp_path / "data", "health")
    assert result.exit_code == 0
    assert "no runs recorded yet" in result.stdout


def test_stub_records_manifest_then_health_prints_it(tmp_path: Path) -> None:
    cfg = write_config(tmp_path / "sources")
    data = tmp_path / "data"

    ingest = _invoke(cfg, data, "ingest")
    assert ingest.exit_code == 0
    assert "stub" in ingest.stdout
    assert list((data / "runs").glob("*.json"))

    health = _invoke(cfg, data, "health")
    assert health.exit_code == 0
    assert '"command": "ingest"' in health.stdout
