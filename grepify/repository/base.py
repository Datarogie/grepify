"""``Repository`` - the single storage contract (PRD §5).

All storage access goes through this interface. v1 is JSONL-truth + SQLite-cache
(:class:`~grepify.repository.jsonl_sqlite.JsonlSqliteRepository`); v2 is Postgres.
The pipeline, trend queries, and digest assembler depend only on this ABC and on
:mod:`grepify.models` - **no backend-specific types appear in any signature**, so
swapping backends is an implementation change, not a caller change.

Design rules
------------
- Writes append to truth and are **idempotent**: re-adding a record with the same
  primary key is a no-op (PRD §8 F-ING-07).
- Reads of *truth* come from JSONL. Reads of *derived* aggregates come from the
  cache, which :meth:`rebuild_cache` regenerates deterministically from truth.
- Sources and source groups are not truth - they are loaded from the
  ``ConfigProvider`` into the cache via :meth:`load_config` (PRD §7).
- Truth is append-only in normal operation. The only exceptions are the
  explicit maintenance rewrites (:meth:`rewrite_items`,
  :meth:`delete_item_keywords`) used by one-time data remediation (GRP-60);
  they mutate existing truth in place and are deliberately outside the
  append-only/idempotent-add contract above.

Failure modes
-------------
Implementations raise :class:`~grepify.errors.RepositoryError` on unreadable
truth or a failed cache rebuild. Per-record validation errors surface as
``pydantic.ValidationError`` from the model constructors before write.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator, Sequence

from grepify.models import (
    Digest,
    FetchLogEntry,
    Item,
    ItemKeyword,
    LlmLogEntry,
    Source,
    SourceGroup,
)


class Repository(ABC):
    """Backend-neutral storage contract."""

    # --- truth writes (append-only, idempotent) ------------------------------

    @abstractmethod
    def add_items(self, items: Sequence[Item]) -> int:
        """Append new items to truth. Returns the count actually written
        (items whose ``item_id`` already exists are skipped)."""

    @abstractmethod
    def add_item_keywords(self, keywords: Sequence[ItemKeyword]) -> int:
        """Append new keyword rows. Returns the count written; existing
        ``(item_id, keyword, method)`` triples are skipped - ``method`` is
        part of the key so an ``llm`` row and a ``fallback`` row can coexist
        for the same keyword text (PRD §6, GRP-25 schema revision)."""

    @abstractmethod
    def add_digest(self, digest: Digest) -> None:
        """Store (overwrite) a digest by ``digest_id``."""

    @abstractmethod
    def log_fetch(self, entry: FetchLogEntry) -> None:
        """Append a fetch-log entry."""

    @abstractmethod
    def log_llm(self, entry: LlmLogEntry) -> None:
        """Append an LLM-call log entry."""

    # --- truth maintenance (rare, deliberate rewrites) -----------------------

    @abstractmethod
    def rewrite_items(self, items: Sequence[Item]) -> int:
        """Overwrite existing item records in truth in place, matched by
        ``item_id``. Returns the count actually rewritten (an ``item_id`` not
        already in truth is skipped, never appended). Unlike :meth:`add_items`
        this is *not* append-only - it is the deliberate escape hatch for a
        one-time data remediation (GRP-60 ``renormalize``), keeping each item in
        its existing date partition (``item_id`` and ``published_at`` are
        unchanged by such a rewrite)."""

    @abstractmethod
    def delete_item_keywords(self, item_ids: Iterable[str]) -> int:
        """Delete every keyword row whose ``item_id`` is in ``item_ids`` from
        truth. Returns the count deleted. The companion to :meth:`rewrite_items`
        for ``renormalize``: after a summary is corrected its stale keyword rows
        are dropped so a forced re-extract regenerates them from the clean text."""

    # --- truth reads ----------------------------------------------------------

    @abstractmethod
    def iter_items(self) -> Iterator[Item]:
        """Iterate all items from truth in deterministic (date, id) order."""

    @abstractmethod
    def iter_item_keywords(self) -> Iterator[ItemKeyword]:
        """Iterate all keyword rows from truth in deterministic order."""

    @abstractmethod
    def iter_digests(self) -> Iterator[Digest]:
        """Iterate all digests from truth in deterministic order."""

    @abstractmethod
    def existing_item_ids(self) -> set[str]:
        """Return the set of item_ids already in truth (for dedup/idempotency)."""

    @abstractmethod
    def iter_fetch_log(self) -> Iterator[FetchLogEntry]:
        """Iterate all fetch-log rows from truth in deterministic order (health
        snapshot, PRD §8 F-ING-08 / GRP-16)."""

    # --- config projection ----------------------------------------------------

    @abstractmethod
    def load_config(self, groups: Iterable[SourceGroup], sources: Iterable[Source]) -> None:
        """Project config-derived sources/groups into the cache (PRD §7)."""

    # --- cache lifecycle & queries -------------------------------------------

    @abstractmethod
    def rebuild_cache(self) -> None:
        """Rebuild the derived query cache from truth. Deterministic and
        idempotent: same truth in → same cache out."""

    @abstractmethod
    def count_items(self) -> int:
        """Number of items in the cache (requires a prior rebuild)."""

    @abstractmethod
    def count_item_keywords(self) -> int:
        """Number of keyword rows in the cache (requires a prior rebuild)."""

    @abstractmethod
    def close(self) -> None:
        """Release any held resources (e.g. DB connection)."""
