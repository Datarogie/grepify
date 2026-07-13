"""CLI tests (GRP-05): validate exit codes, stub manifests, health.

GRP-15/16 add the ``ingest`` wiring tests: ``grepify.cli.build_registry`` is
monkeypatched to a :class:`~grepify.ingest.fake.FakeFetcher`-backed registry
so these exercise the full CLI -> orchestrator -> health-snapshot path without
any network access (PRD §9).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
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


_GROUP_UNREGISTERED_KIND = """
    group: g1
    name: G1
    category: ai
    sources:
      - id: x-src
        kind: x
        handle: someone
      - id: good-src
        kind: rss
        url: https://example.com/good/feed
"""


def test_validate_rejects_source_with_unregistered_kind(tmp_path: Path) -> None:
    """GRP-56: `validate` uses the real production registry (no monkeypatch
    here), which never registers a fetcher for `x` - the same registry
    `ingest` builds, so this is what a real misconfigured source hits."""
    cfg = write_config(tmp_path / "sources", groups={"g.yml": _GROUP_UNREGISTERED_KIND})
    result = _invoke(cfg, tmp_path / "data", "validate")
    assert result.exit_code == 1
    assert "INVALID" in result.stdout
    assert "x-src" in result.stdout
    assert "no registered fetcher" in result.stdout


def test_ingest_isolates_unregistered_kind_and_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GRP-56 defense in depth, exercised through the real CLI: a `kind: x`
    source that slipped past `validate` becomes a per-source `error`
    fetch_log row, `good-src` still ingests, and the run exits zero.
    ``build_registry`` is monkeypatched to :func:`_fake_registry` (no network
    access), same as the other ingest CLI tests in this module - it still
    registers no fetcher for `x`, which is the only thing this test exercises.
    """
    cfg = write_config(tmp_path / "sources", groups={"g.yml": _GROUP_UNREGISTERED_KIND})
    data = tmp_path / "data"
    monkeypatch.setattr("grepify.cli.build_registry", _fake_registry)

    result = _invoke(cfg, data, "ingest")
    assert result.exit_code == 0
    assert "1 error" in result.stdout

    manifest = latest_manifest(DataLayout(data))
    assert manifest is not None
    assert manifest.counts["sources_error"] == 1
    assert manifest.counts["sources_ok"] == 1
    assert any("x-src" in note for note in manifest.notes)

    health = json.loads((data / "health.json").read_text(encoding="utf-8"))
    by_id = {s["source_id"]: s for s in health["sources"]}
    assert by_id["x-src"]["last_status"] == "error"
    assert by_id["good-src"]["last_status"] == "ok"


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

    Accepts (and ignores) the ``build_registry`` kwargs (``transcript_store``)
    so the CLI's real call site can pass them to this monkeypatched stand-in
    unchanged."""
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
    assert manifest.counts["items_new"] == 0


# --- doctor ---------------------------------------------------------------------


def test_doctor_with_no_config_and_no_history(tmp_path: Path) -> None:
    cfg = write_config(tmp_path / "sources")
    result = _invoke(cfg, tmp_path / "data", "doctor")
    assert result.exit_code == 0
    assert "no sources configured" in result.stdout


def test_doctor_reports_status_and_error_class_after_ingest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = write_config(tmp_path / "sources", groups={"g1.yml": _GROUP_INGEST})
    data = tmp_path / "data"
    monkeypatch.setattr("grepify.cli.build_registry", _fake_registry)

    ingest_result = _invoke(cfg, data, "ingest")
    assert ingest_result.exit_code == 0

    result = _invoke(cfg, data, "doctor")
    assert result.exit_code == 0
    assert "2 sources, 1 last-run error" in result.stdout
    assert "bad-src" in result.stdout
    assert "good-src" in result.stdout
    assert "ok" in result.stdout


def test_doctor_is_repeatable_without_a_prior_ingest_run(tmp_path: Path) -> None:
    # No health.json, no prior run - still a valid, deterministic report over
    # whatever config + fetch_log exist (here: none).
    cfg = write_config(tmp_path / "sources", groups={"g1.yml": _GROUP_INGEST})
    data = tmp_path / "data"
    first = _invoke(cfg, data, "doctor")
    second = _invoke(cfg, data, "doctor")
    assert first.exit_code == 0 == second.exit_code
    assert first.stdout == second.stdout
    assert "bad-src" in first.stdout
    assert "never-fetched" in first.stdout


# --- Reddit best-effort cadence + quiet health status ----------------------------

_GROUP_RSS_AND_REDDIT_ALWAYS_ERROR = """
    group: g3
    name: G3
    category: ai
    sources:
      - id: good-src
        kind: rss
        url: https://example.com/good/feed
      - id: bad-rss-src
        kind: rss
        url: https://example.com/bad/feed
      - id: bad-reddit-src
        kind: reddit
        subreddit: bad
"""


class _StepClock:
    """A controllable clock double for CLI tests: each ``ingest`` invocation
    constructs its own ``SystemClock()`` (see ``grepify.cli.main``), so this is
    monkeypatched in as the ``SystemClock`` symbol itself - calling it returns
    this same instance across every invocation in a test, letting the test
    step wall-clock time between successive real CLI calls."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


def _always_error_registry(**_kwargs: object) -> FetcherRegistry:
    reg = FetcherRegistry()
    reg.register(
        FakeFetcher(
            SourceKind.RSS,
            results={"bad-rss-src": FetchError("rss down")},
            default=[RawItem(url="https://example.com/a", title="A", external_id="a")],
        )
    )
    reg.register(_ExplodingFetcher())  # every reddit source errors (kind=reddit)
    return reg


def test_reddit_consecutive_failures_never_flag_but_rss_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = write_config(tmp_path / "sources", groups={"g3.yml": _GROUP_RSS_AND_REDDIT_ALWAYS_ERROR})
    data = tmp_path / "data"
    monkeypatch.setattr("grepify.cli.build_registry", _always_error_registry)

    clock = _StepClock(datetime(2026, 7, 8, 12, 0, tzinfo=UTC))
    monkeypatch.setattr("grepify.cli.SystemClock", lambda: clock)

    # Each run is 21h apart - past the default 20h reddit cadence, so every
    # run is a real attempt for both kinds (5 consecutive real failures each).
    for _ in range(5):
        result = _invoke(cfg, data, "ingest")
        assert result.exit_code == 0
        clock.advance(timedelta(hours=21))

    health = json.loads((data / "health.json").read_text(encoding="utf-8"))
    by_id = {s["source_id"]: s for s in health["sources"]}
    assert by_id["bad-rss-src"]["consecutive_failures"] == 5
    assert by_id["bad-rss-src"]["flagged"] is True
    assert by_id["bad-reddit-src"]["consecutive_failures"] == 5
    assert by_id["bad-reddit-src"]["flagged"] is False  # Reddit stays quiet

    doctor_result = _invoke(cfg, data, "doctor")
    # 1 flagged (bad-rss-src only) despite 2 sources currently in error status.
    assert "3 sources, 2 last-run error, 1 flagged (>=5 consecutive)" in doctor_result.stdout
    assert "bad-reddit-src" in doctor_result.stdout


def test_reddit_source_skipped_for_cadence_on_a_quick_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = write_config(tmp_path / "sources", groups={"g3.yml": _GROUP_RSS_AND_REDDIT_ALWAYS_ERROR})
    data = tmp_path / "data"
    monkeypatch.setattr("grepify.cli.build_registry", _always_error_registry)

    clock = _StepClock(datetime(2026, 7, 8, 12, 0, tzinfo=UTC))
    monkeypatch.setattr("grepify.cli.SystemClock", lambda: clock)

    first = _invoke(cfg, data, "ingest")
    assert first.exit_code == 0
    # Same instant - well within the reddit cadence window.
    second = _invoke(cfg, data, "ingest")
    assert second.exit_code == 0
    assert "1 skipped (cadence)" in second.stdout

    run_id = _run_id_from_output(second.stdout)
    manifest = RunManifest.model_validate_json(
        DataLayout(data).run_manifest(run_id).read_text(encoding="utf-8")
    )
    assert manifest.counts["sources_skipped"] == 1
    assert manifest.counts["sources_attempted"] == 2  # good-src + bad-rss-src; reddit excluded
