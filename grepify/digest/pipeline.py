"""Digest pipeline wiring (E4, GRP-41/42): assemble + generate per category.

The orchestration the ``grepify digest`` CLI command drives: for the requested
kind (daily/weekly), compute the period(s) to consider, then for every enabled
category assemble its input (GRP-40) and generate its digest (GRP-41), collecting
a run summary. The CLI owns building the config, repository, and LLM client and
writing the results to truth (mirrors :mod:`grepify.extract.pipeline`'s split
with the ``extract`` command).

Reliability: catch-up + idempotency (T3)
----------------------------------------
The daily digest walks a catch-up window of the last ``digest.daily_lookback_days``
completed Edmonton days (weekly stays a single ISO week). A (category, period)
whose digest already exists in truth is skipped with **no LLM call**, so a run is
idempotent - re-running generates and re-commits nothing. Together these make the
daily digest self-healing: a morning run that lands outside the GRP-45 gate window
(GitHub cron jitter) no longer leaves a permanently missing digest, because the
next gated run backfills it. Cost stays bounded - only the genuinely-missing pairs
call the LLM.

Digests are keyed on **category**, never user (PRD §2/§7): the category list is
the distinct set of enabled groups' categories, so a personal group joining a
category flows into that category's one digest.

Determinism (F-SIT-08 / S8)
---------------------------
The periods come from the injected clock via :mod:`grepify.digest.periods`;
periods (newest first) and categories (sorted) are iterated in a fixed order;
each digest is a pure function of the cache + that single (faked in tests) LLM
call. Same inputs + same already-present set -> same digests.

Failure modes
-------------
A category below the item threshold is **skipped** (recorded, not failed,
F-DIG-03). LLM problems degrade to the template digest inside
:func:`~grepify.digest.generate.generate_digest` (PRD §9/§13). A bad cache/config
propagates from the layer that owns it; this module raises nothing of its own.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from grepify.clock import Clock
from grepify.config.schemas import SettingsConfig
from grepify.digest.assemble import assemble_digest_input
from grepify.digest.generate import TEMPLATE_MODEL, digest_id_for, generate_digest
from grepify.digest.periods import Period, previous_iso_week, recent_days
from grepify.llm import LlmClient
from grepify.models import Digest, DigestKind

if TYPE_CHECKING:
    from grepify.site.trends import TrendQueries


@dataclass(frozen=True)
class DigestRunResult:
    """Run-level rollup feeding the ``digest`` CLI's run manifest."""

    kind: DigestKind
    period_key: str  # newest period considered (yesterday for daily, last week for weekly)
    categories_total: int  # (category x period) pairs considered this run
    digests_generated: int
    already_present: int = 0  # (category, period) pairs skipped because a digest already exists
    skipped_categories: list[str] = field(default_factory=list)  # below-threshold, deduped+sorted
    template_categories: list[str] = field(default_factory=list)


def periods_for(kind: DigestKind, clock: Clock, *, daily_lookback: int) -> list[Period]:
    """The periods a run considers, newest first.

    Weekly is a single period (the just-completed ISO week). Daily walks a
    catch-up window of the last ``daily_lookback`` completed days so a run
    backfills any recent day missed by a skipped morning gate (T3 reliability);
    the idempotent skip in :func:`run_digest_pipeline` keeps re-runs free.
    """
    if kind is DigestKind.WEEKLY:
        return [previous_iso_week(clock.now())]
    return recent_days(clock.now(), max(daily_lookback, 1))


def run_digest_pipeline(  # noqa: PLR0913 - queries+client+run context+settings+existing ids are distinct inputs
    queries: TrendQueries,
    client: LlmClient,
    *,
    categories: Iterable[str],
    kind: DigestKind,
    clock: Clock,
    run_id: str,
    settings: SettingsConfig,
    existing_digest_ids: set[str] | None = None,
) -> tuple[DigestRunResult, list[Digest]]:
    """Assemble + generate the missing digests over the catch-up window.

    For every (category, period) pair whose digest is not already in
    ``existing_digest_ids``, assemble its input and generate a digest; a pair
    already present is skipped with no LLM call (idempotent). The caller writes
    ``digests`` via :meth:`~grepify.repository.base.Repository.add_digest` and
    reports the summary on the run manifest.
    """
    existing = existing_digest_ids or set()
    periods = periods_for(kind, clock, daily_lookback=settings.digest.daily_lookback_days)
    ordered = sorted(set(categories))

    digests: list[Digest] = []
    skipped: set[str] = set()
    templated: list[str] = []
    considered = 0
    already_present = 0
    for period in periods:
        for category in ordered:
            considered += 1
            if digest_id_for(kind, category, period.key) in existing:
                already_present += 1
                continue
            digest_input = assemble_digest_input(
                queries, category=category, kind=kind, period=period, settings=settings.digest
            )
            digest = generate_digest(
                digest_input,
                client,
                run_id=run_id,
                clock=clock,
                min_items=settings.digest.min_items,
            )
            if digest is None:
                skipped.add(category)
                continue
            digests.append(digest)
            if digest.model == TEMPLATE_MODEL:
                templated.append(category)

    summary = DigestRunResult(
        kind=kind,
        period_key=periods[0].key if periods else "",
        categories_total=considered,
        digests_generated=len(digests),
        already_present=already_present,
        skipped_categories=sorted(skipped),
        template_categories=templated,
    )
    return summary, digests
