"""Re-extraction backfill for ``method='fallback'`` rows (GRP-22, F-EXT-04).

The batcher (GRP-21) never re-extracts an item that already has keyword rows.
That is correct for the common case, but it means an item extracted while the
LLM was down/over-budget/misbehaving stays on the deterministic fallback
forever unless something explicitly revisits it. This module is that
something: :func:`select_fallback_items` finds items whose *entire* keyword
set is still ``method='fallback'``, and :func:`run_fallback_backfill` re-runs
them through :func:`~grepify.extract.batcher.run_extract` against a real LLM
client.

This is a manual/one-time command (``grepify backfill``; playbook S7 recommends
a capped budget, e.g. 200 calls), not pipeline-cron wiring — GRP-25 owns
wiring ordinary ``extract`` into the cron and its data-quality assertions.

Truth is append-only (PRD §5) and ``add_item_keywords`` is idempotent by
``(item_id, keyword, method)`` — a successful backfill adds new
``method='llm'`` rows without deleting the old fallback ones, so an item that
gains at least one *new* ``llm`` row is no longer "entirely fallback" and
drops out of future backfill candidate sets. Old fallback rows for keywords
the new extraction didn't happen to repeat are not retroactively removed;
that is an accepted consequence of the locked append-only truth design
(PRD §5), not a bug in this module.

**Resolved (GRP-25, Kyle-approved PRD §6 diff):** earlier revisions of this
module flagged a convergence gap here — if the LLM's re-extraction returned a
keyword whose *text* exactly matched one already stored as
``method='fallback'``, the old ``(item_id, keyword)`` primary key made
``add_item_keywords`` drop the write as a duplicate, so the item stayed
"entirely fallback" and kept getting re-selected. ``method`` is now part of
the primary key (PRD §6), so an ``llm`` row and a ``fallback`` row with
identical keyword text coexist as distinct rows — the LLM row is written, and
the item correctly drops out of ``select_fallback_items``. See
``test_backfill.py`` for the regression test covering this convergence.

Failure modes
-------------
:func:`select_fallback_items` is a pure function over in-memory records; it
never raises. :func:`run_fallback_backfill` inherits :func:`run_extract`'s
contract — LLM/budget failures degrade the affected batch back to the
fallback extractor rather than raising (PRD §9); it raises nothing of its own.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence

from grepify.clock import Clock
from grepify.extract.batcher import (
    DEFAULT_MAX_ITEMS_PER_CALL,
    DEFAULT_MAX_KEYWORDS,
    ExtractResult,
    FallbackExtractor,
    run_extract,
)
from grepify.llm import LlmClient
from grepify.models import ExtractionMethod, Item, ItemKeyword


def select_fallback_items(items: Iterable[Item], keywords: Iterable[ItemKeyword]) -> list[Item]:
    """Items with at least one keyword row, all of them ``method='fallback'``.

    Excludes untagged items (no keyword rows at all — that is ordinary
    extraction's job, GRP-25) and items that already carry any ``llm`` row
    (already successfully backfilled or LLM-extracted from the start).
    """
    methods_by_item: dict[str, list[ExtractionMethod]] = defaultdict(list)
    for row in keywords:
        methods_by_item[row.item_id].append(row.method)

    eligible_ids = {
        item_id
        for item_id, methods in methods_by_item.items()
        if methods and all(method is ExtractionMethod.FALLBACK for method in methods)
    }
    return [item for item in items if item.item_id in eligible_ids]


def run_fallback_backfill(  # noqa: PLR0913 — mirrors run_extract's flat, distinct-inputs seam
    items: Iterable[Item],
    keywords: Iterable[ItemKeyword],
    client: LlmClient,
    *,
    run_id: str,
    clock: Clock,
    fallback: FallbackExtractor,
    max_items_per_call: int = DEFAULT_MAX_ITEMS_PER_CALL,
    max_keywords: int = DEFAULT_MAX_KEYWORDS,
) -> ExtractResult:
    """Select fallback-only items and re-run them through :func:`run_extract`.

    ``client``'s own budget breaker (``max_calls_per_run``) bounds how many of
    the selected candidates actually reach the LLM this run — batches beyond
    the cap degrade back to ``fallback``, same as ordinary extraction.
    """
    candidates: Sequence[Item] = select_fallback_items(items, keywords)
    return run_extract(
        candidates,
        client,
        run_id=run_id,
        clock=clock,
        fallback=fallback,
        max_items_per_call=max_items_per_call,
        max_keywords=max_keywords,
    )
