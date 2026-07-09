"""GRP-22: ``grepify backfill`` CLI wiring.

``grepify.cli.build_client`` is monkeypatched to a scripted, offline
:class:`~grepify.llm.client.LlmClient` (same pattern ``test_cli.py`` uses for
``build_registry``) so this exercises the full CLI -> repository ->
selection -> ``run_extract`` -> repository path without any network access
(PRD §9).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from grepify.cli import app
from grepify.config.schemas import LlmProfile
from grepify.llm.client import LlmClient, LogSink, RetryPolicy
from grepify.models import ExtractionMethod, Item, ItemKeyword, SourceKind
from grepify.paths import DataLayout
from grepify.repository.jsonl_sqlite import JsonlSqliteRepository
from grepify.run import latest_manifest
from tests.conftest import ScriptedCompletionTransport, envelope_response, write_config

runner = CliRunner()

_GROUP = """
    group: g1
    name: G1
    category: ai
    sources:
      - id: src-1
        kind: rss
        url: https://example.com/feed
"""


def _seed_fallback_item(data_root: Path, *, item_id: str = "item-1") -> None:
    repository = JsonlSqliteRepository(data_root)
    try:
        repository.add_items(
            [
                Item(
                    item_id=item_id,
                    source_id="src-1",
                    kind=SourceKind.RSS,
                    external_id=item_id,
                    canonical_url=f"https://example.com/{item_id}",
                    title="OpenAI releases GPT-5.2",
                    summary="Major reasoning gains for agentic coding.",
                    published_at="2026-07-08T09:00:00+00:00",
                    fetched_at="2026-07-08T10:00:00+00:00",
                    content_hash=f"hash-{item_id}",
                )
            ]
        )
        repository.add_item_keywords(
            [
                ItemKeyword(
                    item_id=item_id,
                    keyword="old-fallback-kw",
                    rank=1,
                    method=ExtractionMethod.FALLBACK,
                    model=None,
                    extracted_at="2026-07-08T10:05:00+00:00",
                )
            ]
        )
    finally:
        repository.close()


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
        ["--config-root", str(config_root), "--data-root", str(data_root), "backfill", *args],
    )


def test_backfill_without_llm_base_url_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    cfg = write_config(tmp_path / "sources", groups={"g1.yml": _GROUP})
    result = _invoke(cfg, tmp_path / "data")
    assert result.exit_code == 1
    assert "LLM_BASE_URL" in result.stderr


def test_backfill_reextracts_fallback_only_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "https://x/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setattr(
        "grepify.cli.build_client",
        _scripted_build_client([json.dumps([{"item_id": "item-1", "keywords": ["genai"]}])]),
    )

    cfg = write_config(tmp_path / "sources", groups={"g1.yml": _GROUP})
    data = tmp_path / "data"
    _seed_fallback_item(data)

    result = _invoke(cfg, data)
    assert result.exit_code == 0
    assert "1 llm batches" in result.stdout

    manifest = latest_manifest(DataLayout(data))
    assert manifest is not None
    assert manifest.command == "backfill"
    assert manifest.counts["batches_llm"] == 1
    assert manifest.counts["keywords_written"] == 1

    repository = JsonlSqliteRepository(data)
    try:
        rows = list(repository.iter_item_keywords())
    finally:
        repository.close()
    by_keyword = {row.keyword: row for row in rows}
    assert by_keyword["old-fallback-kw"].method is ExtractionMethod.FALLBACK
    assert by_keyword["genai"].method is ExtractionMethod.LLM


def test_backfill_rerun_does_not_reselect_already_backfilled_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "https://x/v1")
    cfg = write_config(tmp_path / "sources", groups={"g1.yml": _GROUP})
    data = tmp_path / "data"
    _seed_fallback_item(data)

    monkeypatch.setattr(
        "grepify.cli.build_client",
        _scripted_build_client([json.dumps([{"item_id": "item-1", "keywords": ["genai"]}])]),
    )
    first = _invoke(cfg, data)
    assert first.exit_code == 0

    monkeypatch.setattr("grepify.cli.build_client", _scripted_build_client([]))
    second = _invoke(cfg, data)
    assert second.exit_code == 0
    assert "0 llm batches" in second.stdout
    assert "0 still-fallback batches" in second.stdout
