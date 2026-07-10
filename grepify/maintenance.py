"""One-time data-remediation helpers (GRP-60).

The ``renormalize`` maintenance command's pure core: re-apply the current
summary cleaner (:func:`grepify.ingest.normalize.clean_summary`) to every stored
:class:`~grepify.models.Item` and rewrite the ones whose summary changed, then
drop those items' stale keyword rows so a forced re-extract can regenerate them
from the corrected text.

Why this exists (not a normalizer bug)
--------------------------------------
Ingest is idempotent and extraction is cached, so summaries stored *before* the
GRP-19 HTML-strip fix - and the YAKE-fallback keyword rows derived from them -
were never rewritten. The normalizer is correct today; this is a one-time
backfill over already-stored truth, not a code fix to the ingest path.

Determinism + idempotency (S8)
------------------------------
:func:`renormalize_summaries` is a pure function of the repository's truth and
:func:`clean_summary` (no clock, no network, no LLM). Because ``clean_summary``
is idempotent, a second run finds no changed summaries and rewrites nothing -
the caller can re-run it safely. Re-extraction is a separate, LLM-gated step the
CLI wires on top of the changed-item set this returns.

Failure modes
-------------
Delegates all I/O to the repository; a corrupt truth file surfaces as
:class:`~grepify.errors.RepositoryError` from
:meth:`~grepify.repository.base.Repository.rewrite_items` /
:meth:`~grepify.repository.base.Repository.delete_item_keywords`. This module
raises nothing of its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from grepify.ingest import clean_summary
from grepify.models import Item
from grepify.repository.base import Repository


@dataclass(frozen=True)
class RenormalizeResult:
    """Rollup of a ``renormalize`` data-remediation pass (pre re-extraction)."""

    items_scanned: int
    items_rewritten: int
    keyword_rows_deleted: int
    changed_item_ids: list[str] = field(default_factory=list)


def renormalize_summaries(repository: Repository) -> RenormalizeResult:
    """Re-clean stored summaries; rewrite changed items and drop their keyword rows.

    Scans every item in truth, re-applies :func:`clean_summary` to each non-null
    summary, and for every item whose summary actually changed: rewrites it in
    place (:meth:`~grepify.repository.base.Repository.rewrite_items`) and deletes
    its keyword rows
    (:meth:`~grepify.repository.base.Repository.delete_item_keywords`). Returns a
    :class:`RenormalizeResult`; ``changed_item_ids`` is the set the caller feeds
    to a forced re-extract. Idempotent: a clean corpus yields an all-zero result.
    """
    scanned = 0
    changed: list[Item] = []
    for item in repository.iter_items():
        scanned += 1
        if item.summary is None:
            continue
        cleaned = clean_summary(item.summary)
        if cleaned != item.summary:
            changed.append(item.model_copy(update={"summary": cleaned}))

    if not changed:
        return RenormalizeResult(items_scanned=scanned, items_rewritten=0, keyword_rows_deleted=0)

    changed_ids = [item.item_id for item in changed]
    rewritten = repository.rewrite_items(changed)
    deleted = repository.delete_item_keywords(changed_ids)
    return RenormalizeResult(
        items_scanned=scanned,
        items_rewritten=rewritten,
        keyword_rows_deleted=deleted,
        changed_item_ids=changed_ids,
    )
