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

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from grepify.clock import from_iso
from grepify.health import HealthSnapshot, SourceHealth
from grepify.ingest.dedup import hamming_distance
from grepify.models import Source, SourceStatus
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


@dataclass(frozen=True)
class HealthRow:
    """One source's row on the health page: config lifecycle joined with its
    fetch-log health (GRP-66)."""

    source_id: str
    name: str
    kind: str
    status: SourceStatus
    quiet: bool
    evidence: str | None
    message: str | None
    health: SourceHealth | None

    @property
    def is_degraded(self) -> bool:
        return self.status is SourceStatus.DEGRADED

    @property
    def rung_label(self) -> str | None:
        if self.health is not None and self.health.last_rung is not None:
            return self.health.last_rung.value
        return None

    @property
    def show_flagged(self) -> bool:
        """A red flag only for a genuinely-flagged live source. Quiet
        (best-effort) sources never flag, matching the ingest/doctor rule."""
        return self.health is not None and self.health.flagged and not self.quiet


@dataclass(frozen=True)
class HealthView:
    """The health page split by lifecycle (GRP-66, pinned health-page ACs).

    ``live`` are the enabled (``active``/``degraded``) sources shown in the main
    table; ``disabled`` are ``paywalled``/``dead`` sources shown in a separate
    labelled, collapsed section so they never read as live flagged errors.
    ``gone`` sources are absent from config, so their stale fetch-log rows are
    dropped entirely (they simply disappear). ``run_id``/``generated_at`` carry
    the snapshot provenance (``None`` when no snapshot has been written yet)."""

    live: list[HealthRow]
    disabled: list[HealthRow]
    run_id: str | None = None
    generated_at: str | None = None

    @property
    def has_snapshot(self) -> bool:
        return self.run_id is not None


def build_health_view(
    snapshot: HealthSnapshot | None,
    sources: Iterable[Source],
    *,
    quiet_source_ids: Iterable[str] = (),
) -> HealthView:
    """Join a :class:`~grepify.health.HealthSnapshot` with config ``sources`` by
    lifecycle class (ADR 0002 §2; pinned health-page ACs).

    Cross-checking against current config is the whole point: a snapshot row for
    a source no longer in config (a removed ``gone`` source) is dropped, and a
    ``dead``/``paywalled`` source is routed to the ``disabled`` section instead
    of showing its frozen error streak as a live flag. Both sections are sorted
    by ``source_id`` for byte-stable output. A source with no fetch-log history
    yet still appears, in the section its class dictates."""
    by_id: dict[str, SourceHealth] = {}
    if snapshot is not None:
        by_id = {h.source_id: h for h in snapshot.sources}
    quiet = frozenset(quiet_source_ids)

    live: list[HealthRow] = []
    disabled: list[HealthRow] = []
    for source in sorted(sources, key=lambda s: s.source_id):
        row = HealthRow(
            source_id=source.source_id,
            name=source.name,
            kind=source.kind.value,
            status=source.status,
            quiet=source.source_id in quiet,
            evidence=source.evidence,
            message=source.message,
            health=by_id.get(source.source_id),
        )
        (live if source.status.is_enabled else disabled).append(row)
    return HealthView(
        live=live,
        disabled=disabled,
        run_id=snapshot.run_id if snapshot is not None else None,
        generated_at=snapshot.generated_at if snapshot is not None else None,
    )


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


# --- coverage (GRP-70: "sources you are no longer hearing from") -------------


@dataclass(frozen=True)
class Recency:
    """How long ago a source last contributed an item, relative to a build instant.

    ``last_contributed_at`` is ``None`` when the source has never appeared in
    ``items`` (never fetched, or fetched but never produced an item); ``days``
    is then ``None`` too rather than some sentinel int, so a caller cannot
    mistake "never" for a large-but-real day count.
    """

    last_contributed_at: str | None
    days: int | None

    @property
    def label(self) -> str:
        if self.days is None:
            return "never"
        if self.days == 0:
            return "today"
        if self.days == 1:
            return "1 day ago"
        return f"{self.days} days ago"


def source_recency(last_contributed_at: str | None, *, now: datetime) -> Recency:
    """Whole-day recency of a source's last contributed item, as of ``now``.

    ``last_contributed_at`` (``None`` for a source with zero cached items)
    comes from :meth:`~grepify.site.trends.TrendQueries.last_contributed_at`.
    A timestamp later than ``now`` (clock skew, an odd fixture) floors to zero
    days rather than a nonsensical negative count.
    """
    if last_contributed_at is None:
        return Recency(last_contributed_at=None, days=None)
    days = max((now.date() - from_iso(last_contributed_at).date()).days, 0)
    return Recency(last_contributed_at=last_contributed_at, days=days)


@dataclass(frozen=True)
class SourceRow:
    """One source's row on the sources page: config joined with contribution
    recency (GRP-70).

    ``quiet`` is a live (enabled) source that has not contributed in
    ``coverage_quiet_days`` (or ever) - it is always ``False`` for a
    dead/paywalled source, since #100's lifecycle classes already explain
    that silence; a "quiet" flag on top would double up two distinct causes
    under one word. This is unrelated to :attr:`HealthRow.quiet` (T6
    best-effort/Reddit flag suppression) despite the shared name - that one
    scopes a fetch-error flag, this one scopes item recency.
    """

    source_id: str
    group_id: str
    name: str
    kind: str
    status: SourceStatus
    url: str
    active_url: str | None
    message: str | None
    evidence: str | None
    recency: Recency
    quiet: bool

    @property
    def is_degraded(self) -> bool:
        return self.status is SourceStatus.DEGRADED


def build_source_rows(
    sources: Iterable[Source],
    last_contributed: Mapping[str, str],
    *,
    now: datetime,
    quiet_after_days: int,
) -> list[SourceRow]:
    """Per-source coverage row for the sources page (GRP-70).

    ``last_contributed`` is all-time (:meth:`TrendQueries.last_contributed_at`),
    not the trailing-90d emission window, so a slow-but-real feed is not
    penalized for the build's emission cutoff. Sorted by ``source_id`` for
    byte-stable output.
    """
    rows: list[SourceRow] = []
    for source in sorted(sources, key=lambda s: s.source_id):
        recency = source_recency(last_contributed.get(source.source_id), now=now)
        quiet = source.status.is_enabled and (
            recency.days is None or recency.days >= quiet_after_days
        )
        rows.append(
            SourceRow(
                source_id=source.source_id,
                group_id=source.group_id,
                name=source.name,
                kind=source.kind.value,
                status=source.status,
                url=source.url,
                active_url=source.active_url,
                message=source.message,
                evidence=source.evidence,
                recency=recency,
                quiet=quiet,
            )
        )
    return rows


@dataclass(frozen=True)
class CoverageRollup:
    """The coverage rollup shared by the health + sources pages (GRP-70): how
    many live sources have gone quiet, out of how many live sources total.

    Scoped to live sources only - a dead/paywalled source is excluded from
    both ``quiet_names`` and the live total, never counted as quiet (see
    :class:`SourceRow`).
    """

    quiet_names: tuple[str, ...]
    live_count: int
    quiet_after_days: int

    @property
    def quiet_count(self) -> int:
        return len(self.quiet_names)

    @property
    def has_quiet(self) -> bool:
        return self.quiet_count > 0


def coverage_rollup(rows: Sequence[SourceRow], *, quiet_after_days: int) -> CoverageRollup:
    """Fold :class:`SourceRow`\\ s into the one-line coverage rollup (GRP-70).

    Names are sorted for byte-stable rendering; ``live_count`` recounts
    ``status.is_enabled`` directly rather than trusting ``len(rows)`` so this
    stays correct even if a caller passes a pre-filtered subset.
    """
    live = [row for row in rows if row.status.is_enabled]
    quiet_names = tuple(sorted(row.name for row in live if row.quiet))
    return CoverageRollup(
        quiet_names=quiet_names, live_count=len(live), quiet_after_days=quiet_after_days
    )
