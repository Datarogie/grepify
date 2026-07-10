"""Ordinary extraction, wired into the pipeline (GRP-25).

Selects items with no keyword rows at all (never extracted -
:func:`select_untagged_items`), runs them through
:func:`~grepify.extract.batcher.run_extract` against a real LLM client
(budget-gated, GRP-20), applies :mod:`grepify.keywords`'s normalization +
alias/mute rules to the result, and runs the PRD §10.7 data-quality gate
(:mod:`grepify.extract.quality`) before the caller writes anything to truth.
:func:`run_extract_pipeline` is what the ``extract`` CLI command drives; the
CLI owns building the config/repository/LLM client and writing the run
manifest (mirrors :mod:`grepify.ingest.orchestrator`'s split with ``ingest``).

Untagged-item selection (F-EXT-04)
-----------------------------------
:func:`select_untagged_items` excludes any item with *any* keyword row,
regardless of ``method`` - ``run_extract`` itself has no cache-awareness (it
extracts unconditionally on whatever it is handed, same as the backfill
orchestrator reuses it on its own candidate set), so the caller's selection
*is* the "never re-extract unless ``--force``" rule (F-EXT-04). This is
deliberately a different predicate from
:func:`~grepify.extract.backfill.select_fallback_items` ("already extracted,
entirely fallback"): this module's job is items that have *never* been
extracted at all. ``run_extract_pipeline(..., force=True)`` bypasses the
filter entirely and extracts every item handed to it, tagged or not - the
``extract --force`` CLI escape hatch F-EXT-04 reserves for deliberate
re-extraction (e.g. after a prompt/model change, ahead of GRP-24's eval
harness).

Known limitation (documented, not fixed here): an item whose extraction
legitimately yields zero keyword rows (F-EXT-02 - nothing salient, or every
candidate got muted below) still has zero keyword rows afterwards, so it is
indistinguishable from "never extracted" and will be re-selected on every
future ``extract`` run. There is no persisted "no_keywords" marker in the §6
schema for this (only the one PK revision - adding ``method`` - was
Kyle-approved this session); adding one is a further schema decision, flagged
here as a PRD-diff candidate rather than guessed at. In practice this is
bounded by the same LLM budget gate as every other batch, so it is a repeated
cost, not a correctness bug or an unbounded loop.

Normalization boundary (F-EXT-03 / F-EXT-05)
---------------------------------------------
Every produced :class:`~grepify.models.ItemKeyword` row is passed through
:func:`grepify.keywords.apply_to_keyword` before it is considered for
writing: lowercase/trim/collapse/strip-trailing-punctuation (F-EXT-03),
then alias substitution, then mute - a muted keyword's row is dropped
entirely rather than written (F-EXT-05: "a mute list drops keyword rows").
Applying the alias map here is an eager convenience, not a substitute for
downstream retroactivity: PRD §6 still requires trend queries (E3/E4) to
re-apply :class:`~grepify.keywords.KeywordRules` at read time so that rows
written *before* an alias/mute existed are still covered - re-applying
already-canonical text is idempotent, so doing it once more at write time
changes nothing for those older rows.

Failure modes
-------------
Inherits :func:`run_extract`'s contract (LLM/budget failures degrade to the
fallback extractor, PRD §9) plus :func:`~grepify.extract.quality.assert_data_quality`'s
:class:`~grepify.errors.DataQualityError` for an over-length keyword - both
propagate to the caller unchanged; this module raises nothing of its own.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from grepify.clock import Clock
from grepify.extract.batcher import (
    DEFAULT_MAX_ITEMS_PER_CALL,
    DEFAULT_MAX_KEYWORDS,
    FallbackExtractor,
    run_extract,
)
from grepify.extract.prompt import TranscriptReader
from grepify.extract.quality import DataQualityReport, assert_data_quality
from grepify.keywords import KeywordRules, apply_to_keyword
from grepify.llm import LlmClient
from grepify.models import Item, ItemKeyword


def select_untagged_items(items: Iterable[Item], keywords: Iterable[ItemKeyword]) -> list[Item]:
    """Items with no keyword rows at all (never extracted, any method)."""
    tagged_ids = {row.item_id for row in keywords}
    return [item for item in items if item.item_id not in tagged_ids]


@dataclass(frozen=True)
class ExtractPipelineResult:
    """Run-level rollup feeding the ``extract`` CLI's run manifest."""

    items_selected: int
    batches_total: int
    batches_llm: int
    batches_fallback: int
    budget_exhausted: bool
    muted_count: int
    no_keywords_item_ids: list[str]


def run_extract_pipeline(  # noqa: PLR0913 - items+keywords+client+run context+rules are distinct inputs
    items: Sequence[Item],
    existing_keywords: Sequence[ItemKeyword],
    client: LlmClient,
    *,
    run_id: str,
    clock: Clock,
    fallback: FallbackExtractor,
    rules: KeywordRules,
    force: bool = False,
    max_items_per_call: int = DEFAULT_MAX_ITEMS_PER_CALL,
    max_keywords: int = DEFAULT_MAX_KEYWORDS,
    transcript_reader: TranscriptReader | None = None,
) -> tuple[ExtractPipelineResult, list[ItemKeyword]]:
    """Select untagged items (or, with ``force``, every item), extract,
    normalize, and quality-gate the result.

    Returns ``(summary, keyword_rows)``; the caller (the ``extract`` CLI
    command) is responsible for writing ``keyword_rows`` via
    :meth:`~grepify.repository.base.Repository.add_item_keywords` and
    reporting ``summary`` on the run manifest. ``transcript_reader`` (GRP-53) is
    passed to the batcher so youtube items with a stored transcript get a
    <=1500-char excerpt in their prompt. Raises
    :class:`~grepify.errors.DataQualityError` (propagated from
    :func:`~grepify.extract.quality.assert_data_quality`) before anything is
    returned - the caller never has to write and then discover a violation.
    """
    candidates = list(items) if force else select_untagged_items(items, existing_keywords)
    result = run_extract(
        candidates,
        client,
        run_id=run_id,
        clock=clock,
        fallback=fallback,
        max_items_per_call=max_items_per_call,
        max_keywords=max_keywords,
        transcript_reader=transcript_reader,
    )

    normalized: list[ItemKeyword] = []
    muted_count = 0
    for row in result.keywords:
        applied = apply_to_keyword(row, rules)
        if applied is None:
            muted_count += 1
        else:
            normalized.append(applied)

    report: DataQualityReport = assert_data_quality(candidates, normalized)

    summary = ExtractPipelineResult(
        items_selected=len(candidates),
        batches_total=result.batches_total,
        batches_llm=result.batches_llm,
        batches_fallback=result.batches_fallback,
        budget_exhausted=result.budget_exhausted,
        muted_count=muted_count,
        no_keywords_item_ids=report.no_keywords_item_ids,
    )
    return summary, normalized
