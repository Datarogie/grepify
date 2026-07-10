"""Extraction (E2): batch untagged items into LLM keyword calls (GRP-21).

Public surface later E2 issues (GRP-24 eval, GRP-25 pipeline wiring) build on:

- :func:`run_extract` + :class:`ExtractResult` - the batcher: chunk items,
  call the LLM under its budget breaker, validate the strict-JSON response,
  retry a malformed batch once, then fall back deterministically.
- :class:`FallbackExtractor` - the Protocol the batcher calls when the LLM
  can't deliver; :class:`YakeFallbackExtractor` implements it (GRP-22).
- :func:`build_messages` + :data:`PROMPT_VERSION` - prompt v1.
- :func:`select_fallback_items` + :func:`run_fallback_backfill` - the
  ``method='fallback'`` re-extraction backfill (GRP-22).
- :func:`select_untagged_items` + :func:`run_extract_pipeline` - ordinary
  extraction wired into the pipeline: never-extracted items, normalized +
  quality-gated before the caller writes them (GRP-25).
- :func:`assert_data_quality` - the PRD §10.7 post-extract gate (GRP-25).
- :class:`EvalCase` + :func:`load_eval_cases`, :func:`jaccard_similarity`,
  :func:`score_predictions`, :func:`format_report` - the offline eval harness
  (GRP-24, PRD §10.5), driven by `scripts/eval.py` via `make eval`.

The LLM provider itself (client, budget breaker, retries, ``llm_log``) is
:mod:`grepify.llm` (GRP-20). Normalization + alias/mute application is
:mod:`grepify.keywords` (GRP-23); :mod:`grepify.extract.pipeline` (GRP-25) is
where this package first calls it, ahead of a keyword row ever reaching truth
- see that module's docstring for why applying it here doesn't compromise
§6's retroactive-alias guarantee for older rows.

Failure modes
-------------
None of its own - a re-export aggregator. See :mod:`grepify.extract.batcher`,
:mod:`grepify.extract.fallback`, :mod:`grepify.extract.backfill`,
:mod:`grepify.extract.pipeline`, and :mod:`grepify.extract.quality` for
module-level failure modes.
"""

from __future__ import annotations

from grepify.extract.backfill import run_fallback_backfill, select_fallback_items
from grepify.extract.batcher import (
    DEFAULT_MAX_ITEMS_PER_CALL,
    DEFAULT_MAX_KEYWORDS,
    ExtractResult,
    FallbackExtractor,
    run_extract,
)
from grepify.extract.eval import (
    EvalCase,
    EvalCaseScore,
    EvalReport,
    eval_cases_to_items,
    format_report,
    group_keywords_by_item,
    jaccard_similarity,
    load_eval_cases,
    score_predictions,
)
from grepify.extract.fallback import YakeFallbackExtractor
from grepify.extract.pipeline import (
    ExtractPipelineResult,
    run_extract_pipeline,
    select_untagged_items,
)
from grepify.extract.prompt import PROMPT_VERSION, build_messages
from grepify.extract.quality import DataQualityReport, assert_data_quality

__all__ = [
    "DEFAULT_MAX_ITEMS_PER_CALL",
    "DEFAULT_MAX_KEYWORDS",
    "PROMPT_VERSION",
    "DataQualityReport",
    "EvalCase",
    "EvalCaseScore",
    "EvalReport",
    "ExtractPipelineResult",
    "ExtractResult",
    "FallbackExtractor",
    "YakeFallbackExtractor",
    "assert_data_quality",
    "build_messages",
    "eval_cases_to_items",
    "format_report",
    "group_keywords_by_item",
    "jaccard_similarity",
    "load_eval_cases",
    "run_extract",
    "run_extract_pipeline",
    "run_fallback_backfill",
    "score_predictions",
    "select_fallback_items",
    "select_untagged_items",
]
