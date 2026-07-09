"""CLI glue: score the current extract prompt/model against the GRP-24 labeled
eval set (PRD §10.5). Thin wrapper, mirroring ``scripts/commit_pipeline_data.py``'s
shape: build the real LLM client + keyword rules from committed config (same
environment convention as ``grepify extract``/``grepify backfill`` — deployment
secrets from the environment, never from committed config, PRD §5), drive the
fixture through the real extract pipeline (:mod:`grepify.extract.pipeline`,
unmodified), and hand the result to :mod:`grepify.extract.eval`'s pure scorer
for the printed report. Manual/offline (PRD §10.5): not part of `make check`
or any CI workflow — run this after changing the extract prompt or LLM
profile and paste the printed report into the MR description.

Eval runs are deliberately not persisted anywhere in ``data/`` (no repository,
no run manifest, no ``llm_log`` row): the fixture items are synthetic
(:func:`~grepify.extract.eval.eval_cases_to_items`), never real ingested
items, so writing them to truth or the health/manifest surface would pollute
both with fictitious data. This is the one exception to "every real LLM call
gets an `llm_log` row" (PRD §5/§6) — deliberate, since no call here is a real
pipeline call.

Failure modes
-------------
Exits non-zero (no report printed) if ``LLM_BASE_URL`` is unset — same
convention as ``grepify extract``/``backfill`` (nothing to call). Once
running, per-batch LLM failures degrade to the YAKE fallback extractor like
every other extraction path (PRD §9) rather than failing the run; a
:class:`~grepify.errors.DataQualityError` (an over-length keyword after alias
substitution) propagates and fails the script loudly, matching the real
pipeline's PRD §10.7 gate. A malformed fixture line raises ``ValueError``
from :func:`~grepify.extract.eval.load_eval_cases` (a bad hand-edit, not a
runtime concern).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from grepify.clock import SystemClock
from grepify.config.filesystem import FilesystemConfigProvider
from grepify.extract import (
    YakeFallbackExtractor,
    eval_cases_to_items,
    format_report,
    group_keywords_by_item,
    load_eval_cases,
    run_extract_pipeline,
    score_predictions,
)
from grepify.keywords import KeywordRules
from grepify.llm import build_client
from grepify.run import new_run_id

DEFAULT_CANDIDATES = Path("tests/fixtures/eval/keyword_eval_candidates.jsonl")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--candidates",
        type=Path,
        default=DEFAULT_CANDIDATES,
        help="eval fixture (title/summary/expected_keywords per line)",
    )
    parser.add_argument(
        "--config-root", type=Path, default=Path("sources"), help="config directory (sources/)"
    )
    args = parser.parse_args(argv)

    base_url = os.environ.get("LLM_BASE_URL")
    if not base_url:
        print("eval: LLM_BASE_URL is not set; nothing to do", file=sys.stderr)
        return 1

    clock = SystemClock()
    config = FilesystemConfigProvider(args.config_root)
    settings = config.settings()
    profile = settings.llm.profiles[settings.llm.active_profile]
    client = build_client(
        profile,
        api_key=os.environ.get("LLM_API_KEY") or None,
        base_url=base_url,
        log_sink=lambda _entry: None,  # not a real pipeline run - see module docstring
        clock=clock,
    )
    rules = KeywordRules.from_config(config.keywords())

    cases = load_eval_cases(args.candidates)
    items = eval_cases_to_items(cases, clock=clock)
    _summary, rows = run_extract_pipeline(
        items,
        [],
        client,
        run_id=new_run_id(clock),
        clock=clock,
        fallback=YakeFallbackExtractor(),
        rules=rules,
        force=True,
        max_items_per_call=settings.llm.max_items_per_call,
    )

    predicted_by_id = group_keywords_by_item(rows)
    report = score_predictions(cases, predicted_by_id)
    heading = f"grepify eval - {settings.llm.active_profile} ({profile.model or profile.endpoint})"
    print(format_report(report, heading=heading))
    return 0


if __name__ == "__main__":
    sys.exit(main())
