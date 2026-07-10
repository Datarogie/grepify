"""GRP-25: ``grepify extract`` CLI wiring.

Mirrors ``test_cli_backfill.py``'s pattern: ``grepify.cli.build_client`` is
monkeypatched to a scripted, offline :class:`~grepify.llm.client.LlmClient` so
this exercises the full CLI -> repository -> selection -> ``run_extract`` ->
normalization -> quality gate -> repository path without any network access
(PRD §9).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from grepify.cli import app
from grepify.config.schemas import LlmProfile
from grepify.errors import DataQualityError
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

_KEYWORDS_NO_ALIASES = """
    aliases: {}
    mute: []
    pin: []
"""

_KEYWORDS_WITH_MUTE = """
    aliases: {}
    mute:
      - webinar
    pin: []
"""


def _seed_item(data_root: Path, *, item_id: str = "item-1", tagged: bool = False) -> None:
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
        if tagged:
            repository.add_item_keywords(
                [
                    ItemKeyword(
                        item_id=item_id,
                        keyword="already-tagged",
                        rank=1,
                        method=ExtractionMethod.LLM,
                        model="prior-model",
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
        ["--config-root", str(config_root), "--data-root", str(data_root), "extract", *args],
    )


def test_extract_without_llm_base_url_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    cfg = write_config(tmp_path / "sources", groups={"g1.yml": _GROUP})
    result = _invoke(cfg, tmp_path / "data")
    assert result.exit_code == 1
    assert "LLM_BASE_URL" in result.stderr


def test_extract_untagged_item_writes_normalized_keywords(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "https://x/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setattr(
        "grepify.cli.build_client",
        _scripted_build_client(
            [json.dumps([{"item_id": "item-1", "keywords": ["  Gen   AI!! "]}])]
        ),
    )

    cfg = write_config(
        tmp_path / "sources", groups={"g1.yml": _GROUP}, keywords=_KEYWORDS_NO_ALIASES
    )
    data = tmp_path / "data"
    _seed_item(data)

    result = _invoke(cfg, data)
    assert result.exit_code == 0
    assert "1 items" in result.stdout
    assert "1 new keyword rows" in result.stdout

    manifest = latest_manifest(DataLayout(data))
    assert manifest is not None
    assert manifest.command == "extract"
    assert manifest.counts["items_selected"] == 1
    assert manifest.counts["keywords_written"] == 1
    assert manifest.counts["items_no_keywords"] == 0

    repository = JsonlSqliteRepository(data)
    try:
        rows = list(repository.iter_item_keywords())
    finally:
        repository.close()
    assert [row.keyword for row in rows] == ["gen ai"]
    assert rows[0].method is ExtractionMethod.LLM


def test_extract_skips_already_tagged_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "https://x/v1")
    monkeypatch.setattr("grepify.cli.build_client", _scripted_build_client([]))

    cfg = write_config(tmp_path / "sources", groups={"g1.yml": _GROUP})
    data = tmp_path / "data"
    _seed_item(data, tagged=True)

    result = _invoke(cfg, data)
    assert result.exit_code == 0
    assert "0 items" in result.stdout

    manifest = latest_manifest(DataLayout(data))
    assert manifest is not None
    assert manifest.counts["items_selected"] == 0
    assert manifest.counts["keywords_written"] == 0


def test_extract_mutes_a_keyword_and_notes_no_keywords_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "https://x/v1")
    monkeypatch.setattr(
        "grepify.cli.build_client",
        _scripted_build_client([json.dumps([{"item_id": "item-1", "keywords": ["webinar"]}])]),
    )

    cfg = write_config(
        tmp_path / "sources", groups={"g1.yml": _GROUP}, keywords=_KEYWORDS_WITH_MUTE
    )
    data = tmp_path / "data"
    _seed_item(data)

    result = _invoke(cfg, data)
    assert result.exit_code == 0

    manifest = latest_manifest(DataLayout(data))
    assert manifest is not None
    assert manifest.counts["keywords_written"] == 0
    assert manifest.counts["keywords_muted"] == 1
    assert manifest.counts["items_no_keywords"] == 1
    assert any("item-1" in note for note in manifest.notes)


def test_extract_force_reextracts_already_tagged_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "https://x/v1")
    monkeypatch.setattr(
        "grepify.cli.build_client",
        _scripted_build_client(
            [json.dumps([{"item_id": "item-1", "keywords": ["fresh-keyword"]}])]
        ),
    )

    cfg = write_config(
        tmp_path / "sources", groups={"g1.yml": _GROUP}, keywords=_KEYWORDS_NO_ALIASES
    )
    data = tmp_path / "data"
    _seed_item(data, tagged=True)

    result = _invoke(cfg, data, "--force")
    assert result.exit_code == 0
    assert "1 items" in result.stdout

    manifest = latest_manifest(DataLayout(data))
    assert manifest is not None
    assert manifest.counts["items_selected"] == 1
    assert manifest.counts["keywords_written"] == 1

    repository = JsonlSqliteRepository(data)
    try:
        rows = list(repository.iter_item_keywords())
    finally:
        repository.close()
    assert {row.keyword for row in rows} == {"already-tagged", "fresh-keyword"}


def test_extract_rerun_does_not_reselect_extracted_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "https://x/v1")
    cfg = write_config(tmp_path / "sources", groups={"g1.yml": _GROUP})
    data = tmp_path / "data"
    _seed_item(data)

    monkeypatch.setattr(
        "grepify.cli.build_client",
        _scripted_build_client([json.dumps([{"item_id": "item-1", "keywords": ["genai"]}])]),
    )
    first = _invoke(cfg, data)
    assert first.exit_code == 0

    monkeypatch.setattr("grepify.cli.build_client", _scripted_build_client([]))
    second = _invoke(cfg, data)
    assert second.exit_code == 0
    assert "0 items" in second.stdout


def test_extract_data_quality_violation_fails_the_run_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Force a >60-char "keyword" past the batcher's own sanity check by having
    # the fallback extractor (not the LLM) produce it, so the pipeline's own
    # quality gate - not the batcher's per-response validation - is what fires.
    monkeypatch.setenv("LLM_BASE_URL", "https://x/v1")
    monkeypatch.setattr(
        "grepify.cli.build_client", _scripted_build_client(["not json", "still not json"])
    )

    class _OverLongFallback:
        def extract(self, items: object) -> dict[str, list[str]]:
            return {item.item_id: ["x" * 61] for item in items}  # type: ignore[attr-defined]

    monkeypatch.setattr("grepify.cli.YakeFallbackExtractor", _OverLongFallback)

    cfg = write_config(tmp_path / "sources", groups={"g1.yml": _GROUP})
    data = tmp_path / "data"
    _seed_item(data)

    result = _invoke(cfg, data)
    assert result.exit_code != 0
    assert isinstance(result.exception, DataQualityError)
