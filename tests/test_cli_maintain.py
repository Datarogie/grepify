"""GRP-60: ``grepify maintain renormalize`` CLI wiring.

Mirrors ``test_cli_extract.py``: ``grepify.cli.build_client`` is monkeypatched to
a scripted, offline :class:`~grepify.llm.client.LlmClient`, so the full CLI ->
renormalize (clean summaries + drop stale keyword rows) -> forced re-extract ->
store path runs without a network call. The fixture is a dirty-HTML summary with
a stale YAKE-noise keyword row ("div"), exactly the pre-GRP-19 contamination the
command exists to remediate.
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


def _seed_dirty(data_root: Path) -> None:
    repository = JsonlSqliteRepository(data_root)
    try:
        repository.add_items(
            [
                Item(
                    item_id="dirty-1",
                    source_id="src-1",
                    kind=SourceKind.RSS,
                    external_id="dirty-1",
                    canonical_url="https://example.com/dirty-1",
                    title="OpenAI releases GPT-5.2",
                    summary='<div class="post">Major reasoning gains.</div>',
                    published_at="2026-07-08T09:00:00+00:00",
                    fetched_at="2026-07-08T10:00:00+00:00",
                    content_hash="hash-dirty-1",
                )
            ]
        )
        repository.add_item_keywords(
            [
                ItemKeyword(
                    item_id="dirty-1",
                    keyword="div",  # YAKE-fallback noise from the dirty summary
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


def _invoke(config_root: Path, data_root: Path) -> object:
    return runner.invoke(
        app,
        [
            "--config-root",
            str(config_root),
            "--data-root",
            str(data_root),
            "maintain",
            "renormalize",
        ],
    )


def test_renormalize_without_llm_base_url_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    cfg = write_config(tmp_path / "sources", groups={"g1.yml": _GROUP})
    result = _invoke(cfg, tmp_path / "data")
    assert result.exit_code == 1
    assert "LLM_BASE_URL" in result.stderr


def test_renormalize_cleans_summary_and_reextracts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "https://x/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setattr(
        "grepify.cli.build_client",
        _scripted_build_client(
            [json.dumps([{"item_id": "dirty-1", "keywords": ["reasoning gains"]}])]
        ),
    )

    cfg = write_config(tmp_path / "sources", groups={"g1.yml": _GROUP})
    data = tmp_path / "data"
    _seed_dirty(data)

    result = _invoke(cfg, data)
    assert result.exit_code == 0, result.stdout
    assert "1 summaries rewritten" in result.stdout

    manifest = latest_manifest(DataLayout(data))
    assert manifest is not None
    assert manifest.command == "maintain-renormalize"
    assert manifest.counts["items_rewritten"] == 1
    assert manifest.counts["keyword_rows_deleted"] == 1
    assert manifest.counts["items_reextracted"] == 1
    assert manifest.counts["keywords_written"] == 1

    repository = JsonlSqliteRepository(data)
    try:
        items = list(repository.iter_items())
        rows = list(repository.iter_item_keywords())
    finally:
        repository.close()
    assert items[0].summary == "Major reasoning gains."  # markup stripped
    # stale "div" fallback row replaced by the fresh LLM keyword
    assert [(r.keyword, r.method) for r in rows] == [("reasoning gains", ExtractionMethod.LLM)]


def test_renormalize_is_idempotent_second_run_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "https://x/v1")
    cfg = write_config(tmp_path / "sources", groups={"g1.yml": _GROUP})
    data = tmp_path / "data"
    _seed_dirty(data)

    monkeypatch.setattr(
        "grepify.cli.build_client",
        _scripted_build_client(
            [json.dumps([{"item_id": "dirty-1", "keywords": ["reasoning gains"]}])]
        ),
    )
    first = _invoke(cfg, data)
    assert first.exit_code == 0

    # Second run: summaries already clean, so nothing is rewritten and the
    # re-extract path is never entered (empty script would IndexError if it were).
    monkeypatch.setattr("grepify.cli.build_client", _scripted_build_client([]))
    second = _invoke(cfg, data)
    assert second.exit_code == 0, second.stdout
    assert "0 summaries rewritten" in second.stdout

    # Truth is unchanged by the second run (asserted directly: both runs land in
    # the same wall-clock second, so run-id order - and thus latest_manifest - is
    # a coin flip between them; the stored state is the deterministic witness).
    repository = JsonlSqliteRepository(data)
    try:
        items = list(repository.iter_items())
        rows = list(repository.iter_item_keywords())
    finally:
        repository.close()
    assert items[0].summary == "Major reasoning gains."
    assert [(r.keyword, r.method) for r in rows] == [("reasoning gains", ExtractionMethod.LLM)]
