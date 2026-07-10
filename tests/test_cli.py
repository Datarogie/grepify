"""CLI tests (GRP-05): validate exit codes, stub manifests, health.

GRP-15/16 add the ``ingest`` wiring tests: ``grepify.cli.build_registry`` is
monkeypatched to a :class:`~grepify.ingest.fake.FakeFetcher`-backed registry
so these exercise the full CLI -> orchestrator -> health-snapshot path without
any network access (PRD §9).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from grepify.cli import app
from grepify.errors import FetchError
from grepify.ingest.base import Fetcher, RawItem
from grepify.ingest.fake import FakeFetcher
from grepify.ingest.registry import FetcherRegistry
from grepify.models import RunManifest, Source, SourceKind
from grepify.paths import DataLayout
from grepify.run import latest_manifest
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

_GROUP_INGEST = """
    group: g1
    name: G1
    category: ai
    sources:
      - id: good-src
        kind: rss
        url: https://example.com/good/feed
      - id: bad-src
        kind: rss
        url: https://example.com/bad/feed
"""

_GROUP_WITH_UNEXPECTED_EXCEPTION = """
    group: g2
    name: G2
    category: ai
    sources:
      - id: good-src
        kind: rss
        url: https://example.com/good/feed
      - id: boom-src
        kind: reddit
        subreddit: boom
"""


def _invoke(config_root: Path, data_root: Path, command: str) -> object:
    return runner.invoke(
        app,
        ["--config-root", str(config_root), "--data-root", str(data_root), command],
    )


def _run_id_from_output(output: str) -> str:
    """Pull the trailing ``run <run_id>`` token the ``ingest`` command prints.

    Two invocations issued back-to-back can land in the same wall-clock
    second, so ``run_id``'s lexical sort order (``latest_manifest``'s
    assumption) isn't reliable enough to pick out *this* invocation's
    manifest - read it directly by the run_id this call actually printed.
    """
    match = re.search(r"run (\S+)\s*$", output.strip())
    assert match is not None, output
    return match.group(1)


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

    trends = _invoke(cfg, data, "trends")
    assert trends.exit_code == 0
    assert "stub" in trends.stdout
    assert list((data / "runs").glob("*.json"))

    health = _invoke(cfg, data, "health")
    assert health.exit_code == 0
    assert '"command": "trends"' in health.stdout


def test_build_writes_site_and_manifest(tmp_path: Path) -> None:
    cfg = write_config(tmp_path / "sources", groups={"ai-research.yml": _GROUP_OK})
    data = tmp_path / "data"
    out = tmp_path / "public"
    result = runner.invoke(
        app,
        [
            "--config-root",
            str(cfg),
            "--data-root",
            str(data),
            "build",
            "--output-dir",
            str(out),
            "--base-path",
            "/grepify/",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert (out / "index.html").is_file()
    assert (out / "items" / "index.html").is_file()
    assert (out / "sources" / "index.html").is_file()
    assert (out / "health" / "index.html").is_file()
    assert (out / "digest" / "index.html").is_file()
    assert (out / "static" / "style.css").is_file()
    # links carry the base path; sources page lists the configured source
    assert "/grepify/static/style.css" in (out / "index.html").read_text(encoding="utf-8")
    assert "ahead-of-ai" in (out / "sources" / "index.html").read_text(encoding="utf-8")

    manifest = latest_manifest(DataLayout(data))
    assert manifest is not None
    assert manifest.command == "build"
    # home + digest index + items + sources + health
    assert manifest.counts["pages_written"] == 5


def _fake_registry(**_kwargs: object) -> FetcherRegistry:
    """One RSS ``FakeFetcher``: ``good-src`` returns an item, ``bad-src`` errors.

    Accepts (and ignores) the E5 ``build_registry`` kwargs (``tweet_source``,
    ``since_ids``, ``transcript_store``) so the CLI's real call site can pass
    them to this monkeypatched stand-in unchanged."""
    reg = FetcherRegistry()
    reg.register(
        FakeFetcher(
            SourceKind.RSS,
            results={"bad-src": FetchError("boom")},
            default=[RawItem(url="https://example.com/a", title="A", external_id="a")],
        )
    )
    return reg


def test_ingest_wired_isolates_failures_and_writes_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = write_config(tmp_path / "sources", groups={"g1.yml": _GROUP_INGEST})
    data = tmp_path / "data"
    monkeypatch.setattr("grepify.cli.build_registry", _fake_registry)

    result = _invoke(cfg, data, "ingest")
    assert result.exit_code == 0
    assert "1 ok" in result.stdout
    assert "1 error" in result.stdout

    manifest = latest_manifest(DataLayout(data))
    assert manifest is not None
    assert manifest.command == "ingest"
    assert manifest.counts["sources_ok"] == 1
    assert manifest.counts["sources_error"] == 1
    assert manifest.counts["items_new"] == 1
    assert any("bad-src" in note for note in manifest.notes)

    health = json.loads((data / "health.json").read_text(encoding="utf-8"))
    by_id = {s["source_id"]: s for s in health["sources"]}
    assert by_id["bad-src"]["consecutive_failures"] == 1
    assert by_id["bad-src"]["flagged"] is False
    assert by_id["good-src"]["last_status"] == "ok"


class _ExplodingFetcher(Fetcher):
    """A fetcher that raises a non-``FetchError`` exception (unexpected-failure path)."""

    @property
    def kind(self) -> SourceKind:
        return SourceKind.REDDIT

    def fetch(self, source: Source) -> list[RawItem]:
        raise ValueError("boom-unexpected")


def _fake_registry_with_exploding_reddit(**_kwargs: object) -> FetcherRegistry:
    reg = FetcherRegistry()
    reg.register(
        FakeFetcher(
            SourceKind.RSS,
            default=[RawItem(url="https://example.com/a", title="A", external_id="a")],
        )
    )
    reg.register(_ExplodingFetcher())
    return reg


def test_ingest_isolates_unexpected_exception_at_cli_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = write_config(tmp_path / "sources", groups={"g2.yml": _GROUP_WITH_UNEXPECTED_EXCEPTION})
    data = tmp_path / "data"
    monkeypatch.setattr("grepify.cli.build_registry", _fake_registry_with_exploding_reddit)

    result = _invoke(cfg, data, "ingest")
    assert result.exit_code == 0
    assert "1 ok" in result.stdout
    assert "1 error" in result.stdout

    run_id = _run_id_from_output(result.stdout)
    manifest = RunManifest.model_validate_json(
        DataLayout(data).run_manifest(run_id).read_text(encoding="utf-8")
    )
    assert manifest.counts["sources_error"] == 1
    assert any("boom-unexpected" in note for note in manifest.notes)


def test_malformed_x_accounts_secret_does_not_fail_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bad GREPIFY_X_ACCOUNTS secret degrades to a logged skip, not a run
    failure (X is best-effort, PRD §13): the rss/reddit sources still ingest and
    a manifest note records the ignored secret."""
    cfg = write_config(tmp_path / "sources", groups={"g1.yml": _GROUP_INGEST})
    data = tmp_path / "data"
    monkeypatch.setattr("grepify.cli.build_registry", _fake_registry)
    monkeypatch.setenv("GREPIFY_X_ACCOUNTS", "{not json")

    result = _invoke(cfg, data, "ingest")

    assert result.exit_code == 0
    assert "1 ok" in result.stdout  # good-src still ingested
    manifest = latest_manifest(DataLayout(data))
    assert manifest is not None
    assert any("x accounts secret ignored" in note for note in manifest.notes)


def test_ingest_rerun_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = write_config(tmp_path / "sources", groups={"g1.yml": _GROUP_INGEST})
    data = tmp_path / "data"
    monkeypatch.setattr("grepify.cli.build_registry", _fake_registry)

    first = _invoke(cfg, data, "ingest")
    assert first.exit_code == 0
    second = _invoke(cfg, data, "ingest")
    assert second.exit_code == 0

    run_id = _run_id_from_output(second.stdout)
    manifest_path = DataLayout(data).run_manifest(run_id)
    manifest = RunManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    assert manifest.counts["items_new"] == 0  # F-ING-07: rerun adds zero new rows
