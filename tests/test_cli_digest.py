"""GRP-41/42/45: ``grepify digest`` and ``grepify digest-gate`` CLI wiring.

Mirrors ``test_cli_extract.py``: ``grepify.cli.build_client`` is monkeypatched to
a scripted, offline :class:`~grepify.llm.client.LlmClient`, and
``grepify.cli.SystemClock`` to a :class:`~grepify.clock.FixedClock`, so the full
CLI -> rebuild-cache -> assemble -> generate -> store path runs without a network
call and with a deterministic period.
"""

from __future__ import annotations

import json
import textwrap
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from grepify.cli import app
from grepify.clock import FixedClock
from grepify.config.schemas import LlmProfile
from grepify.llm.client import LlmClient, LogSink, RetryPolicy
from grepify.models import ExtractionMethod, Item, ItemKeyword, SourceKind
from grepify.paths import DataLayout
from grepify.repository.jsonl_sqlite import JsonlSqliteRepository
from grepify.run import latest_manifest
from tests.conftest import ScriptedCompletionTransport, envelope_response, write_config

runner = CliRunner()

# summer clock -> the just-completed Edmonton day is 2026-07-07
_CLOCK = FixedClock(datetime(2026, 7, 8, 13, 0, tzinfo=UTC))
_YESTERDAY = "2026-07-07T18:00:00+00:00"

_SETTINGS = textwrap.dedent(
    """
    llm:
      active_profile: p
      max_items_per_call: 25
      profiles:
        p: {endpoint: openai-compat, model: digest-model, max_calls_per_run: 40}
    digest:
      min_items: 2
    timezone: America/Edmonton
    """
).strip()

_SETTINGS_PAUSED = textwrap.dedent(
    """
    llm:
      active_profile: p
      max_items_per_call: 25
      profiles:
        p: {endpoint: openai-compat, model: digest-model, max_calls_per_run: 40}
    digest:
      min_items: 2
      enabled: false
    timezone: America/Edmonton
    """
).strip()

_GROUP = """
    group: g1
    name: G1
    category: ai
    sources:
      - id: src-1
        kind: rss
        url: https://example.com/feed
"""


def _seed(data_root: Path) -> None:
    repo = JsonlSqliteRepository(data_root)
    try:
        repo.add_items(
            [
                Item(
                    item_id=f"item-{n}",
                    source_id="src-1",
                    kind=SourceKind.RSS,
                    external_id=f"item-{n}",
                    canonical_url=f"https://example.com/item-{n}",
                    title=f"story {n}",
                    summary="s",
                    published_at=_YESTERDAY,
                    fetched_at=_YESTERDAY,
                    content_hash=f"{n:016x}",
                )
                for n in range(3)
            ]
        )
        repo.add_item_keywords(
            [
                ItemKeyword(
                    item_id=f"item-{n}",
                    keyword="genai",
                    rank=1,
                    method=ExtractionMethod.LLM,
                    model="m",
                    extracted_at=_YESTERDAY,
                )
                for n in range(3)
            ]
        )
    finally:
        repo.close()


def _scripted_build_client(script: list[str]) -> object:
    transport = ScriptedCompletionTransport([envelope_response(s) for s in script])

    def fake_build_client(
        profile: LlmProfile,
        *,
        api_key: str | None,
        base_url: str,
        log_sink: LogSink,
        clock: object,
        **_: object,
    ) -> LlmClient:
        return LlmClient(
            model=profile.model or "fake-model",
            base_url=base_url,
            api_key=api_key,
            log_sink=log_sink,  # type: ignore[arg-type]
            clock=clock,  # type: ignore[arg-type]
            transport=transport,
            max_calls_per_run=profile.max_calls_per_run,
            retry=RetryPolicy(sleep=lambda _s: None, rng=lambda: 0.0),
        )

    return fake_build_client


def _invoke(config_root: Path, data_root: Path, *args: str) -> object:
    return runner.invoke(
        app,
        ["--config-root", str(config_root), "--data-root", str(data_root), *args],
    )


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("grepify.cli.SystemClock", lambda: _CLOCK)


def test_digest_without_llm_base_url_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    cfg = write_config(tmp_path / "sources", groups={"g1.yml": _GROUP}, settings=_SETTINGS)
    result = _invoke(cfg, tmp_path / "data", "digest")
    assert result.exit_code == 1
    assert "LLM_BASE_URL" in result.stderr


def test_digest_daily_generates_and_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "https://x/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    reply = json.dumps({"title": "AI today", "tldr": ["genai up"], "body_md": "A narrative."})
    monkeypatch.setattr("grepify.cli.build_client", _scripted_build_client([reply]))

    cfg = write_config(tmp_path / "sources", groups={"g1.yml": _GROUP}, settings=_SETTINGS)
    data = tmp_path / "data"
    _seed(data)

    result = _invoke(cfg, data, "digest", "--kind", "daily")
    assert result.exit_code == 0, result.stdout
    assert "1 generated" in result.stdout

    manifest = latest_manifest(DataLayout(data))
    assert manifest is not None
    assert manifest.command == "digest"
    assert manifest.counts["digests_generated"] == 1

    repo = JsonlSqliteRepository(data)
    try:
        digests = list(repo.iter_digests())
    finally:
        repo.close()
    assert [d.digest_id for d in digests] == ["daily-ai-2026-07-07"]
    assert digests[0].title == "AI today"
    assert digests[0].model == "digest-model"


def test_digest_daily_is_idempotent_across_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # T3 regression: the first run generates + persists yesterday's digest; a
    # second run with the same clock makes no LLM call (empty script would
    # IndexError) and generates nothing - the digest is already in truth.
    monkeypatch.setenv("LLM_BASE_URL", "https://x/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    reply = json.dumps({"title": "AI today", "tldr": ["genai up"], "body_md": "A narrative."})

    cfg = write_config(tmp_path / "sources", groups={"g1.yml": _GROUP}, settings=_SETTINGS)
    data = tmp_path / "data"
    _seed(data)

    monkeypatch.setattr("grepify.cli.build_client", _scripted_build_client([reply]))
    first = _invoke(cfg, data, "digest", "--kind", "daily")
    assert first.exit_code == 0, first.stdout
    assert "1 generated" in first.stdout

    monkeypatch.setattr("grepify.cli.build_client", _scripted_build_client([]))
    second = _invoke(cfg, data, "digest", "--kind", "daily")
    assert second.exit_code == 0, second.stdout
    assert "0 generated" in second.stdout
    assert "1 already present" in second.stdout

    manifest = latest_manifest(DataLayout(data))
    assert manifest is not None
    assert manifest.counts["digests_generated"] == 0
    assert manifest.counts["digests_already_present"] == 1

    repo = JsonlSqliteRepository(data)
    try:
        digests = list(repo.iter_digests())
    finally:
        repo.close()
    assert [d.digest_id for d in digests] == ["daily-ai-2026-07-07"]  # exactly one, not duplicated


def test_digest_skips_category_below_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "https://x/v1")
    monkeypatch.setattr("grepify.cli.build_client", _scripted_build_client([]))

    # min_items default is 10 here (no digest block) -> the 3 seeded items skip
    settings = textwrap.dedent(
        """
        llm:
          active_profile: p
          profiles:
            p: {endpoint: openai-compat, model: m, max_calls_per_run: 40}
        """
    ).strip()
    cfg = write_config(tmp_path / "sources", groups={"g1.yml": _GROUP}, settings=settings)
    data = tmp_path / "data"
    _seed(data)

    result = _invoke(cfg, data, "digest")
    assert result.exit_code == 0, result.stdout
    assert "0 generated" in result.stdout
    manifest = latest_manifest(DataLayout(data))
    assert manifest is not None
    assert manifest.counts["categories_skipped"] == 1


def test_digest_paused_when_disabled_generates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # digest.enabled=false freezes generation: no LLM calls, no digest files,
    # exit 0, and a manifest note recording the pause (T1 pause switch).
    monkeypatch.setenv("LLM_BASE_URL", "https://x/v1")

    def _explode(*_a: object, **_k: object) -> object:
        raise AssertionError("build_client must not run when digest is paused")

    monkeypatch.setattr("grepify.cli.build_client", _explode)

    cfg = write_config(tmp_path / "sources", groups={"g1.yml": _GROUP}, settings=_SETTINGS_PAUSED)
    data = tmp_path / "data"
    _seed(data)

    result = _invoke(cfg, data, "digest", "--kind", "daily")
    assert result.exit_code == 0, result.stdout
    assert "paused" in result.stdout

    manifest = latest_manifest(DataLayout(data))
    assert manifest is not None
    assert manifest.command == "digest"
    assert manifest.ok is True
    assert manifest.counts["digests_generated"] == 0
    assert any("paused" in note for note in manifest.notes)

    repo = JsonlSqliteRepository(data)
    try:
        assert list(repo.iter_digests()) == []
    finally:
        repo.close()


def test_digest_paused_does_not_require_llm_base_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The pause is checked before the LLM_BASE_URL requirement, so remediation
    # runs need no deployment secrets to keep digests frozen.
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    cfg = write_config(tmp_path / "sources", groups={"g1.yml": _GROUP}, settings=_SETTINGS_PAUSED)
    data = tmp_path / "data"
    _seed(data)

    result = _invoke(cfg, data, "digest")
    assert result.exit_code == 0, result.stdout
    assert "paused" in result.stdout


def test_digest_gate_prints_flags(tmp_path: Path) -> None:
    cfg = write_config(tmp_path / "sources", groups={"g1.yml": _GROUP}, settings=_SETTINGS)
    result = _invoke(cfg, tmp_path / "data", "digest-gate")
    assert result.exit_code == 0
    # 2026-07-08 13:00Z is 07:00 MDT (Wednesday) -> daily due, weekly not
    assert result.stdout.strip().splitlines() == ["daily=true", "weekly=false"]
