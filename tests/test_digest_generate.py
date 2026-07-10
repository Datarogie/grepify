"""Digest generation + pipeline tests (GRP-41/42): fake LLM, skip, template.

No network: a scripted in-memory transport stands in for the model (PRD §9/§10).
Covers the three outcomes - LLM success (provenance = model), skip below the
item threshold (F-DIG-03), and template fallback when the LLM is over budget or
returns a malformed reply (PRD §13) - plus the per-category pipeline rollup.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from grepify.clock import FixedClock
from grepify.config.schemas import KeywordsConfig, SettingsConfig
from grepify.digest.assemble import DigestInput, KeywordBrief
from grepify.digest.generate import TEMPLATE_MODEL, digest_id_for, generate_digest
from grepify.digest.periods import Period, previous_day
from grepify.digest.pipeline import run_digest_pipeline
from grepify.keywords import KeywordRules
from grepify.llm import LlmClient
from grepify.models import DigestKind, SourceGroup
from grepify.paths import DataLayout
from grepify.repository import JsonlSqliteRepository
from grepify.site.trends import ItemSummary, TrendQueries, open_cache
from tests.conftest import ScriptedCompletionTransport, envelope_response
from tests.test_digest_assemble import _item, _kw, _source  # reuse the canned builders

_CLOCK = FixedClock(datetime(2026, 7, 8, 13, 0, tzinfo=UTC))
_PERIOD = previous_day(_CLOCK.now())


def _client(replies: list[str], *, model: str = "digest-model", max_calls: int = 40) -> LlmClient:
    transport = ScriptedCompletionTransport([envelope_response(r) for r in replies])
    return LlmClient(
        model=model,
        base_url="https://api.example/v1",
        api_key="sk-test",
        log_sink=lambda entry: None,
        clock=_CLOCK,
        transport=transport,
        max_calls_per_run=max_calls,
    )


def _input(*, item_count: int, kind: DigestKind = DigestKind.DAILY) -> DigestInput:
    items = [
        ItemSummary(
            item_id="i1",
            source_id="s1",
            source_name="S1",
            kind="rss",
            title="OpenAI ships GPT-5",
            canonical_url="https://ex.com/i1",
            published_at="2026-07-07T18:00:00+00:00",
            summary="a summary",
            content_hash="h1",
        )
    ]
    return DigestInput(
        category="ai",
        kind=kind,
        period=_PERIOD,
        item_count=item_count,
        keywords=[
            KeywordBrief(keyword="genai", count=3, previous_count=1, rising=True, items=items),
            KeywordBrief(keyword="agents", count=1, previous_count=0, rising=False, items=[]),
        ],
    )


def _reply(title: str = "AI moved fast") -> str:
    return json.dumps(
        {
            "title": title,
            "tldr": ["genai surged", "agents steady"],
            "body_md": "Para one.\n\nPara two.",
        }
    )


# --- single-category generation ----------------------------------------------


def test_llm_success_records_model_and_composed_body() -> None:
    digest = generate_digest(
        _input(item_count=20), _client([_reply()]), run_id="r1", clock=_CLOCK, min_items=10
    )
    assert digest is not None
    assert digest.model == "digest-model"  # provenance = the model that wrote it
    assert digest.prompt_version == "digest-v1"  # prompt provenance recorded (F-DIG-04)
    assert digest.digest_id == digest_id_for(DigestKind.DAILY, "ai", _PERIOD.key)
    assert digest.title == "AI moved fast"
    # body composes the TL;DR bullets then the narrative
    assert digest.body_md.startswith("**TL;DR**\n\n- genai surged\n- agents steady")
    assert "Para one." in digest.body_md
    # chips come from the deterministic assembler, not the model (ranked order,
    # only the object keys are sorted)
    assert json.loads(digest.top_keywords) == [
        {"count": 3, "keyword": "genai"},
        {"count": 1, "keyword": "agents"},
    ]


def test_below_threshold_is_skipped() -> None:
    digest = generate_digest(
        _input(item_count=4), _client([]), run_id="r1", clock=_CLOCK, min_items=10
    )
    assert digest is None  # F-DIG-03: too few items - skipped, not failed


def test_over_budget_degrades_to_template() -> None:
    # a cap of 0 refuses the call before any network I/O -> template digest
    digest = generate_digest(
        _input(item_count=20), _client([], max_calls=0), run_id="r1", clock=_CLOCK, min_items=10
    )
    assert digest is not None
    assert digest.model == TEMPLATE_MODEL
    assert digest.prompt_version == "none"  # template path used no LLM prompt
    assert "genai" in digest.body_md  # built from assembler data alone


def test_malformed_reply_degrades_to_template() -> None:
    digest = generate_digest(
        _input(item_count=20), _client(["not json at all"]), run_id="r1", clock=_CLOCK, min_items=10
    )
    assert digest is not None
    assert digest.model == TEMPLATE_MODEL


def test_weekly_kind_id_and_period() -> None:
    weekly_input = _input(item_count=20, kind=DigestKind.WEEKLY)
    weekly_input = DigestInput(
        category=weekly_input.category,
        kind=DigestKind.WEEKLY,
        period=Period(
            start="2026-06-29T06:00:00+00:00",
            end="2026-07-06T06:00:00+00:00",
            key="2026-W27",
            days=7,
        ),
        item_count=weekly_input.item_count,
        keywords=weekly_input.keywords,
    )
    digest = generate_digest(
        weekly_input, _client([_reply()]), run_id="r1", clock=_CLOCK, min_items=1
    )
    assert digest is not None
    assert digest.kind is DigestKind.WEEKLY
    assert digest.digest_id == "weekly-ai-2026-W27"


# --- pipeline (per-category rollup) ------------------------------------------


def _pipeline_queries(tmp_path: Path) -> TrendQueries:
    repo = JsonlSqliteRepository(tmp_path)
    # ai category: 3 items in-window (>= min_items=3); data-eng: 1 item (skipped)
    repo.add_items(
        [
            _item("i1", source_id="s1", published_at="2026-07-07T18:00:00+00:00"),
            _item("i2", source_id="s1", published_at="2026-07-07T18:00:00+00:00"),
            _item("i3", source_id="s2", published_at="2026-07-07T18:00:00+00:00"),
            _item("d1", source_id="sd", published_at="2026-07-07T18:00:00+00:00"),
        ]
    )
    repo.add_item_keywords(
        [_kw("i1", "genai"), _kw("i2", "genai"), _kw("i3", "genai"), _kw("d1", "x")]
    )
    repo.load_config(
        [
            SourceGroup(group_id="g-ai", name="AI", category="ai"),
            SourceGroup(group_id="g-de", name="Data", category="data-eng"),
        ],
        [_source("s1", "g-ai"), _source("s2", "g-ai"), _source("sd", "g-de")],
    )
    repo.rebuild_cache()
    repo.close()
    return TrendQueries(
        open_cache(DataLayout(tmp_path)), KeywordRules.from_config(KeywordsConfig())
    )


def _settings() -> SettingsConfig:
    return SettingsConfig.model_validate(
        {
            "llm": {
                "active_profile": "p",
                "profiles": {"p": {"endpoint": "openai-compat", "model": "m"}},
            },
            "digest": {"min_items": 3},
        }
    )


def test_pipeline_generates_and_skips_per_category(tmp_path: Path) -> None:
    queries = _pipeline_queries(tmp_path)
    summary, digests = run_digest_pipeline(
        queries,
        _client([_reply()]),  # one reply: only the 'ai' category calls the LLM
        categories=["ai", "data-eng"],
        kind=DigestKind.DAILY,
        clock=_CLOCK,
        run_id="r1",
        settings=_settings(),
    )
    assert summary.categories_total == 2
    assert summary.digests_generated == 1
    assert summary.skipped_categories == ["data-eng"]  # only 1 item < min_items(3)
    assert [d.category for d in digests] == ["ai"]
    assert summary.period_key == _PERIOD.key


def test_pipeline_is_deterministic(tmp_path: Path) -> None:
    queries = _pipeline_queries(tmp_path)
    args = dict(
        categories=["ai", "data-eng"],
        kind=DigestKind.DAILY,
        clock=_CLOCK,
        run_id="r1",
        settings=_settings(),
    )
    first, _ = run_digest_pipeline(queries, _client([_reply()]), **args)  # type: ignore[arg-type]
    second, _ = run_digest_pipeline(queries, _client([_reply()]), **args)  # type: ignore[arg-type]
    assert first == second
