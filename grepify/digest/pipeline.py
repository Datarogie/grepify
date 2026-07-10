"""Digest pipeline wiring (E4, GRP-41/42): assemble + generate per category.

The orchestration the ``grepify digest`` CLI command drives: for the requested
kind (daily/weekly), compute the just-completed America/Edmonton period, then for
every enabled category assemble its input (GRP-40) and generate its digest
(GRP-41), collecting a run summary. The CLI owns building the config, repository,
and LLM client and writing the results to truth (mirrors
:mod:`grepify.extract.pipeline`'s split with the ``extract`` command).

Digests are keyed on **category**, never user (PRD §2/§7): the category list is
the distinct set of enabled groups' categories, so a personal group joining a
category flows into that category's one digest.

Determinism (F-SIT-08 / S8)
---------------------------
The period comes from the injected clock via :mod:`grepify.digest.periods`;
categories are iterated in sorted order; each digest is a pure function of the
cache + that single (faked in tests) LLM call. Same inputs -> same digests.

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

from grepify.clock import Clock
from grepify.config.schemas import SettingsConfig
from grepify.digest.assemble import assemble_digest_input
from grepify.digest.generate import TEMPLATE_MODEL, generate_digest
from grepify.digest.periods import Period, previous_day, previous_iso_week
from grepify.llm import LlmClient
from grepify.models import Digest, DigestKind
from grepify.site.trends import TrendQueries


@dataclass(frozen=True)
class DigestRunResult:
    """Run-level rollup feeding the ``digest`` CLI's run manifest."""

    kind: DigestKind
    period_key: str
    categories_total: int
    digests_generated: int
    skipped_categories: list[str] = field(default_factory=list)
    template_categories: list[str] = field(default_factory=list)


def period_for(kind: DigestKind, clock: Clock) -> Period:
    """The just-completed America/Edmonton period for ``kind`` (injected clock)."""
    if kind is DigestKind.WEEKLY:
        return previous_iso_week(clock.now())
    return previous_day(clock.now())


def run_digest_pipeline(  # noqa: PLR0913 - queries+client+run context+settings are distinct inputs
    queries: TrendQueries,
    client: LlmClient,
    *,
    categories: Iterable[str],
    kind: DigestKind,
    clock: Clock,
    run_id: str,
    settings: SettingsConfig,
) -> tuple[DigestRunResult, list[Digest]]:
    """Assemble + generate one digest per category; return ``(summary, digests)``.

    The caller writes ``digests`` via
    :meth:`~grepify.repository.base.Repository.add_digest` and reports the
    summary on the run manifest.
    """
    period = period_for(kind, clock)
    ordered = sorted(set(categories))

    digests: list[Digest] = []
    skipped: list[str] = []
    templated: list[str] = []
    for category in ordered:
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
            skipped.append(category)
            continue
        digests.append(digest)
        if digest.model == TEMPLATE_MODEL:
            templated.append(category)

    summary = DigestRunResult(
        kind=kind,
        period_key=period.key,
        categories_total=len(ordered),
        digests_generated=len(digests),
        skipped_categories=skipped,
        template_categories=templated,
    )
    return summary, digests
