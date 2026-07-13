"""Page-assembly helpers (GRP-32/33/34): pure transforms feeding the templates.

The logic the item/source/health pages need *before* templating - near-dup
collapse, pagination, the client-side filter predicate, and the emitted-JSON
shape - lives here as pure, deterministic functions so it is unit-testable
without a browser (the repo has no Node toolchain by decision, PRD §5). The
build orchestrator (:mod:`grepify.site.build`) queries the data, calls these to
shape it, and renders the templates; ``static/filters.js`` is a thin DOM wrapper
that mirrors :func:`item_matches_filter` exactly (tested here in Python so the
contract the JS depends on is pinned).

Determinism (F-SIT-08 / S8): near-dup clustering visits items in a fixed
``(published_at desc, item_id)`` order and pagination preserves it; the emitted
JSON and facet lists are built from sorted inputs. No clock, no dict-order
reliance.

Failure modes
-------------
Pure functions over already-queried :class:`~grepify.site.trends.ItemSummary`
rows; nothing here does I/O or raises for data (an empty item list yields zero
pages). :func:`~grepify.ingest.dedup.hamming_distance` raises ``ValueError`` on
mismatched hash widths - a corrupt cache, surfaced loudly.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from grepify.ingest.dedup import hamming_distance
from grepify.site.trends import CloudDataset, DigestDetail, ItemSummary, KeywordCount

ITEMS_PER_PAGE = 20  # F-SIT-03
NEAR_DUP_MAX_DISTANCE = 3  # simhash Hamming bits (matches ingest default)
RISING_STRIP_LIMIT = 8  # cap on the home "Rising this week" strip


@dataclass(frozen=True)
class ItemGroup:
    """A representative item plus its near-duplicates (F-SIT-03 collapse)."""

    representative: ItemSummary
    similar: list[ItemSummary]

    @property
    def similar_count(self) -> int:
        return len(self.similar)


@dataclass(frozen=True)
class Page:
    """One page of the items browser."""

    number: int  # 1-based
    total_pages: int
    groups: list[ItemGroup]

    @property
    def has_prev(self) -> bool:
        return self.number > 1

    @property
    def has_next(self) -> bool:
        return self.number < self.total_pages


def collapse_near_duplicates(
    items: list[ItemSummary], *, max_distance: int = NEAR_DUP_MAX_DISTANCE
) -> list[ItemGroup]:
    """Cluster near-duplicate titles (same wire story reposted) - grouping only.

    Items are visited in their given order (the browser hands them newest-first),
    each greedily joined to the first existing group within ``max_distance`` bits
    of any member; the first item of a group is its representative. Never deletes
    (PRD §6 note 2) - collapse is a UI expander. O(n²), so callers pass a
    windowed/paginated slice, not the whole corpus.
    """
    groups: list[list[ItemSummary]] = []
    for item in items:
        for group in groups:
            if any(
                hamming_distance(item.content_hash, member.content_hash) <= max_distance
                for member in group
            ):
                group.append(item)
                break
        else:
            groups.append([item])
    return [ItemGroup(representative=g[0], similar=g[1:]) for g in groups]


def paginate(groups: list[ItemGroup], *, per_page: int = ITEMS_PER_PAGE) -> list[Page]:
    """Split collapsed groups into fixed-size pages (F-SIT-03, 20/page)."""
    if per_page <= 0:
        raise ValueError("per_page must be positive")
    total_pages = max(1, (len(groups) + per_page - 1) // per_page)
    return [
        Page(
            number=n + 1,
            total_pages=total_pages,
            groups=groups[n * per_page : (n + 1) * per_page],
        )
        for n in range(total_pages)
    ]


def build_pages(
    items: list[ItemSummary],
    *,
    per_page: int = ITEMS_PER_PAGE,
    max_distance: int = NEAR_DUP_MAX_DISTANCE,
) -> list[Page]:
    """Paginate raw items 20/page, then collapse near-dups **within each page**.

    Collapsing per page (not over the whole corpus) keeps the O(n²) clustering
    bounded to O(per_page²) per page - total O(n) across the trailing-90d set,
    the "collapses per page/window, not the whole corpus" contract (PRD §6 note
    2, F-SIT-03). A near-dup that straddles a page boundary is not grouped, an
    accepted consequence of per-page collapse; ``items`` is already newest-first
    so straddlers are rare (repost lag < one page of throughput).
    """
    if per_page <= 0:
        raise ValueError("per_page must be positive")
    chunks = [items[i : i + per_page] for i in range(0, len(items), per_page)] or [[]]
    total_pages = len(chunks)
    return [
        Page(
            number=n + 1,
            total_pages=total_pages,
            groups=collapse_near_duplicates(chunk, max_distance=max_distance),
        )
        for n, chunk in enumerate(chunks)
    ]


def item_matches_filter(  # noqa: PLR0913 - mirrors the JS predicate's inputs exactly
    *,
    kind: str,
    source_id: str,
    keywords: list[str],
    kind_filter: str = "",
    source_filter: str = "",
    keyword_filter: str = "",
) -> bool:
    """The exact predicate ``static/filters.js`` implements (pinned here).

    An empty filter matches everything. ``kind``/``source`` are exact matches;
    ``keyword`` is a case-insensitive substring match against any of the item's
    keyword tags. All active filters must match (AND).
    """
    if kind_filter and kind != kind_filter:
        return False
    if source_filter and source_id != source_filter:
        return False
    if keyword_filter:
        needle = keyword_filter.strip().lower()
        if needle and not any(needle in kw.lower() for kw in keywords):
            return False
    return True


def item_json(item: ItemSummary, *, keywords: list[str], similar_count: int) -> dict[str, Any]:
    """The emitted-JSON shape for one item row (the filters.js data contract)."""
    return {
        "item_id": item.item_id,
        "kind": item.kind,
        "source_id": item.source_id,
        "source_name": item.source_name,
        "title": item.title,
        "url": item.canonical_url,
        "published_at": item.published_at,
        "keywords": keywords,
        "similar_count": similar_count,
    }


def page_facets(page: Page, item_tags: dict[str, list[str]]) -> dict[str, list[Any]]:
    """Distinct kinds / sources / keywords on a page, for the filter controls.

    Sorted for byte-stable output; sources carry ``id`` + ``name`` so the
    control can show a label while filtering on the id.
    """
    kinds: set[str] = set()
    sources: dict[str, str] = {}
    keywords: set[str] = set()
    for group in page.groups:
        for item in (group.representative, *group.similar):
            kinds.add(item.kind)
            sources[item.source_id] = item.source_name
            keywords.update(item_tags.get(item.item_id, []))
    return {
        "kinds": sorted(kinds),
        "sources": [{"id": sid, "name": sources[sid]} for sid in sorted(sources)],
        "keywords": sorted(keywords),
    }


def rising_strip(cloud: CloudDataset, *, limit: int = RISING_STRIP_LIMIT) -> list[KeywordCount]:
    """The count-ranked, capped subset of ``cloud.keywords`` flagged rising (GRP-68).

    A pure re-slice of the cloud dataset the home page already computed for
    the keyword cloud - no new query, no rising-math change. ``cloud.keywords``
    is already sorted ``(-count, keyword)`` (F-TRD-01/F-TRD-03), so filtering to
    the rising ones keeps that same count-ranked, byte-stable order; this only
    truncates it to ``limit`` for a compact home-page strip. Empty when nothing
    in the window is rising, so the caller can hide the strip entirely.
    """
    return [kw for kw in cloud.keywords if kw.rising][:limit]


def latest_digest_per_category(digests: Sequence[DigestDetail]) -> list[DigestDetail]:
    """Most-recent digest per category, regardless of kind (T4 health page).

    ``digests`` is expected in :meth:`~grepify.site.trends.TrendQueries.all_digests`'s
    order (``period_start`` desc, ``created_at`` desc, ``digest_id`` desc - a
    total order), so the first digest seen for a category is the one for its
    latest period; a plain ``setdefault`` fold captures that without
    re-deriving the sort here. Categories are stored per-digest text (not
    re-validated against currently configured groups), so a category retired
    from config still shows its last digest. Sorted by category name for
    byte-stable rendering. An empty ``digests`` yields ``[]``.
    """
    best: dict[str, DigestDetail] = {}
    for digest in digests:
        best.setdefault(digest.category, digest)
    return [best[category] for category in sorted(best)]
