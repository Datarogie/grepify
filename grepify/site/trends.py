"""Trend queries over the derived cache (GRP-31, PRD §8 F-TRD-01 / F-SIT-01).

Window-parameterized, deterministic queries that turn the SQLite cache
(``data/grepify.db``, rebuilt from JSONL truth) into the datasets the home page
renders: the keyword **cloud** (counts + window-over-window deltas), the
**stats** block, **top sources**, and the **latest items / digests** lists.

Why this reads SQLite directly (not ``Repository``)
---------------------------------------------------
The v1 site is a static build that PRD §15 replaces wholesale with FastAPI +
Postgres at v2, so the SSG layer is intentionally cache-aware and thrown away at
the v2 boundary - the ``Repository`` interface stays backend-neutral for the
*pipeline*, which is what v2 preserves. :func:`open_cache` opens a read
connection to the rebuilt cache; the build orchestrator (GRP-35) calls
``Repository.rebuild_cache()`` first.

Alias / mute merge (PRD §6)
---------------------------
Aliases and mutes are applied **at query time**, not extraction time, so remaps
are retroactive: every windowed ``(keyword, item_id)`` row is folded through
:meth:`grepify.keywords.KeywordRules.apply` before counting. A keyword's count
is its number of **distinct items** in the window (an ``llm`` row and a
``fallback`` row for the same keyword on the same item count once - §6's
method-in-primary-key design).

Determinism (F-SIT-08 / S8)
---------------------------
- Window bounds are computed from an **injected** instant, never a clock read
  here. :func:`window_ending_at` takes the instant as an argument.
- Every ``order by`` carries a tie-breaker, giving a total order; Python folds
  emit results sorted by ``(-count, keyword)`` etc. so output is byte-stable
  regardless of row-visit order.
- SQL is lowercase with explicit column lists (no ``select *``), repo style.

Failure modes
-------------
- A cache missing the expected tables (never rebuilt / corrupt) surfaces the
  underlying ``sqlite3.OperationalError`` - a systemic build fault, not a
  degradation (the build orchestrator owns rebuilding it first).
- Pure reads otherwise; nothing here writes or touches the network/LLM.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from grepify.clock import from_iso, to_iso
from grepify.keywords import KeywordRules
from grepify.paths import DataLayout
from grepify.site.urls import keyword_slug

DEFAULT_CLOUD_LIMIT = 60
DEFAULT_TOP_SOURCES_LIMIT = 10
DEFAULT_LATEST_ITEMS_LIMIT = 10
DEFAULT_LATEST_DIGESTS_LIMIT = 5


# --- window arithmetic -------------------------------------------------------


@dataclass(frozen=True)
class Window:
    """A half-open ``[start, end)`` window of ISO-8601 UTC timestamp strings."""

    start: str
    end: str
    days: int


def window_ending_at(instant: datetime, *, days: int) -> Window:
    """Window of ``days`` ending at ``instant`` (injected - never a clock read)."""
    if days <= 0:
        raise ValueError("window days must be positive")
    end = instant
    start = end - timedelta(days=days)
    return Window(start=to_iso(start), end=to_iso(end), days=days)


def previous_window(window: Window) -> Window:
    """The immediately-preceding window of the same length (for deltas)."""
    end = from_iso(window.start)
    start = end - timedelta(days=window.days)
    return Window(start=to_iso(start), end=to_iso(end), days=window.days)


# --- datasets ----------------------------------------------------------------


@dataclass(frozen=True)
class KeywordCount:
    """One cloud term: its in-window count, delta, and rising flag (F-TRD-03)."""

    keyword: str
    count: int
    delta: int
    rising: bool


@dataclass(frozen=True)
class CloudDataset:
    """The keyword cloud for a window (F-TRD-01)."""

    window: Window
    keywords: list[KeywordCount] = field(default_factory=list)

    @property
    def max_count(self) -> int:
        return max((k.count for k in self.keywords), default=0)

    @property
    def min_count(self) -> int:
        return min((k.count for k in self.keywords), default=0)


@dataclass(frozen=True)
class SourceCount:
    """A source ranked by in-window item count (F-SIT-01 top sources)."""

    source_id: str
    name: str
    kind: str
    count: int


@dataclass(frozen=True)
class Stats:
    """The home stats block (F-SIT-01), all in-window after merge/mute."""

    item_count: int
    source_count: int
    keyword_count: int
    mention_count: int
    top_keyword: str | None
    top_source: str | None


@dataclass(frozen=True)
class ItemSummary:
    """A row for the latest-items list / items browser."""

    item_id: str
    source_id: str
    source_name: str
    kind: str
    title: str
    canonical_url: str
    published_at: str
    summary: str | None
    content_hash: str


@dataclass(frozen=True)
class DigestSummary:
    """A row for the latest-digests list (populated once E4 writes digests)."""

    digest_id: str
    kind: str
    category: str
    title: str
    period_start: str
    period_end: str
    created_at: str


@dataclass(frozen=True)
class KeywordChip:
    """One ``top_keywords`` chip on a digest (keyword + its in-digest count)."""

    keyword: str
    count: int


@dataclass(frozen=True)
class DigestDetail:
    """A full digest for the index/detail pages (GRP-43), body included."""

    digest_id: str
    kind: str
    category: str
    title: str
    body_md: str
    top_keywords: list[KeywordChip]
    period_start: str
    period_end: str
    created_at: str


@dataclass(frozen=True)
class CoKeyword:
    """A co-occurring keyword on a keyword detail page (F-TRD-02)."""

    keyword: str
    count: int


@dataclass(frozen=True)
class KeywordDetail:
    """The keyword detail dataset (F-TRD-02 / F-SIT-04, GRP-44).

    ``timeline`` is one distinct-item count per day across the window (oldest
    first), sized for the sparkline. ``items_by_kind`` maps each ``kind`` to its
    most-recent items mentioning the keyword (capped), ``kinds`` is the sorted
    tab order. All lists are pre-sorted for byte-stable rendering.
    """

    keyword: str
    slug: str
    count: int
    source_count: int
    window_days: int
    timeline: list[int]
    co_occurring: list[CoKeyword]
    kinds: list[str]
    items_by_kind: dict[str, list[ItemSummary]]


# --- cache access ------------------------------------------------------------


def _parse_chips(top_keywords_json: str) -> list[KeywordChip]:
    """Parse the stored ``top_keywords`` json (``[{keyword, count}]``) into chips.

    Tolerant of a malformed/empty value (renders no chips rather than failing a
    build): a digest's chips are informational, so a bad blob yields ``[]``.
    """
    try:
        raw = json.loads(top_keywords_json)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(raw, list):
        return []
    chips: list[KeywordChip] = []
    for entry in raw:
        if isinstance(entry, dict) and "keyword" in entry and "count" in entry:
            chips.append(KeywordChip(keyword=str(entry["keyword"]), count=int(entry["count"])))
    return chips


def open_cache(layout: DataLayout) -> sqlite3.Connection:
    """Open a read connection to the rebuilt cache (``data/grepify.db``)."""
    return sqlite3.connect(layout.cache_db)


class TrendQueries:
    """Deterministic trend datasets over a cache connection + keyword rules."""

    def __init__(self, conn: sqlite3.Connection, rules: KeywordRules) -> None:
        self._conn = conn
        self._rules = rules

    # --- keyword cloud + deltas ---------------------------------------------

    def _merged_counts(self, window: Window, *, category: str | None = None) -> dict[str, set[str]]:
        """``canonical keyword -> set(item_id)`` in the window, after merge+mute.

        Distinct item ids per merged keyword - the count is ``len(set)``. When
        ``category`` is given, only items whose source's group is in that
        category are counted (``items -> sources -> source_groups`` join); the
        digest assembler (GRP-40) uses this to key counts on category, never on
        user (PRD §7).
        """
        if category is None:
            rows = self._conn.execute(
                "select ik.keyword, ik.item_id "
                "from item_keywords ik "
                "join items i on i.item_id = ik.item_id "
                "where i.published_at >= ? and i.published_at < ?",
                (window.start, window.end),
            )
        else:
            rows = self._conn.execute(
                "select ik.keyword, ik.item_id "
                "from item_keywords ik "
                "join items i on i.item_id = ik.item_id "
                "join sources s on s.source_id = i.source_id "
                "join source_groups g on g.group_id = s.group_id "
                "where i.published_at >= ? and i.published_at < ? and g.category = ?",
                (window.start, window.end, category),
            )
        merged: dict[str, set[str]] = {}
        for keyword, item_id in rows:
            canonical = self._rules.apply(keyword)
            if canonical is None:  # muted (F-EXT-05)
                continue
            merged.setdefault(canonical, set()).add(item_id)
        return merged

    def cloud(
        self,
        window: Window,
        *,
        previous: Window | None = None,
        limit: int = DEFAULT_CLOUD_LIMIT,
        rising_min_count: int,
        rising_ratio: float,
    ) -> CloudDataset:
        """Top ``limit`` keywords by in-window count, with deltas + rising flag
        (F-TRD-01/F-TRD-03). ``rising_min_count``/``rising_ratio`` are threaded
        in from ``settings.digest`` by the caller (build orchestrator), never
        defaulted here, so the cloud and the digest prompt always agree on the
        same config-driven thresholds.

        The import of :func:`~grepify.digest.rising.is_rising` is deferred to
        call time: ``grepify.digest`` (the package) imports
        :mod:`grepify.digest.assemble`, which imports this module, so a
        module-level import here would be circular.
        """
        from grepify.digest.rising import is_rising  # noqa: PLC0415 - deferred, see above

        current = {kw: len(items) for kw, items in self._merged_counts(window).items()}
        prev_window = previous if previous is not None else previous_window(window)
        prior = {kw: len(items) for kw, items in self._merged_counts(prev_window).items()}

        # rank by count desc, then keyword asc - total order, byte-stable
        ranked = sorted(current.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
        keywords = [
            KeywordCount(
                keyword=kw,
                count=count,
                delta=count - prior.get(kw, 0),
                rising=is_rising(
                    count,
                    prior.get(kw, 0),
                    min_count=rising_min_count,
                    ratio=rising_ratio,
                ),
            )
            for kw, count in ranked
        ]
        return CloudDataset(window=window, keywords=keywords)

    # --- stats block ---------------------------------------------------------

    def stats(self, window: Window) -> Stats:
        """The F-SIT-01 stats block for ``window`` (after merge/mute)."""
        merged = self._merged_counts(window)
        keyword_count = len(merged)
        mention_count = sum(len(items) for items in merged.values())
        # top keyword: highest distinct-item count, ties broken alphabetically
        top_keyword: str | None = None
        if merged:
            top_keyword = min(merged, key=lambda kw: (-len(merged[kw]), kw))

        (item_count,) = self._conn.execute(
            "select count(*) from items where published_at >= ? and published_at < ?",
            (window.start, window.end),
        ).fetchone()
        (source_count,) = self._conn.execute(
            "select count(distinct source_id) from items "
            "where published_at >= ? and published_at < ?",
            (window.start, window.end),
        ).fetchone()

        top_sources = self.top_sources(window, limit=1)
        top_source = top_sources[0].name if top_sources else None

        return Stats(
            item_count=int(item_count),
            source_count=int(source_count),
            keyword_count=keyword_count,
            mention_count=mention_count,
            top_keyword=top_keyword,
            top_source=top_source,
        )

    # --- top sources ---------------------------------------------------------

    def top_sources(
        self, window: Window, *, limit: int = DEFAULT_TOP_SOURCES_LIMIT
    ) -> list[SourceCount]:
        """Sources ranked by distinct in-window item count (F-SIT-01)."""
        rows = self._conn.execute(
            "select i.source_id, coalesce(s.name, i.source_id) as name, "
            "coalesce(s.kind, min(i.kind)) as kind, count(distinct i.item_id) as n "
            "from items i "
            "left join sources s on s.source_id = i.source_id "
            "where i.published_at >= ? and i.published_at < ? "
            "group by i.source_id "
            "order by n desc, i.source_id asc "
            "limit ?",
            (window.start, window.end, limit),
        )
        return [
            SourceCount(source_id=sid, name=name, kind=kind, count=int(n))
            for sid, name, kind, n in rows
        ]

    # --- latest lists --------------------------------------------------------

    def latest_items(
        self, *, limit: int = DEFAULT_LATEST_ITEMS_LIMIT, since: str | None = None
    ) -> list[ItemSummary]:
        """Most-recent items by ``published_at`` (F-SIT-01); ``since`` bounds
        the trailing-emission window (GRP-35's 90d rule passes it here)."""
        where = "where i.published_at >= ? " if since is not None else ""
        params: tuple[object, ...] = (since, limit) if since is not None else (limit,)
        rows = self._conn.execute(
            "select i.item_id, i.source_id, coalesce(s.name, i.source_id) as name, "
            "i.kind, i.title, i.canonical_url, i.published_at, i.summary, i.content_hash "
            "from items i "
            "left join sources s on s.source_id = i.source_id "
            f"{where}"
            "order by i.published_at desc, i.item_id desc "
            "limit ?",
            params,
        )
        return [
            ItemSummary(
                item_id=iid,
                source_id=sid,
                source_name=name,
                kind=kind,
                title=title,
                canonical_url=url,
                published_at=pub,
                summary=summary,
                content_hash=chash,
            )
            for iid, sid, name, kind, title, url, pub, summary, chash in rows
        ]

    def latest_digests(self, *, limit: int = DEFAULT_LATEST_DIGESTS_LIMIT) -> list[DigestSummary]:
        """Most-recent digests (empty until E4 writes any)."""
        rows = self._conn.execute(
            "select digest_id, kind, category, title, period_start, period_end, created_at "
            "from digests "
            "order by created_at desc, digest_id desc "
            "limit ?",
            (limit,),
        )
        return [
            DigestSummary(
                digest_id=did,
                kind=kind,
                category=cat,
                title=title,
                period_start=ps,
                period_end=pe,
                created_at=created,
            )
            for did, kind, cat, title, ps, pe, created in rows
        ]

    def distinct_keywords_for_items(self, item_ids: Iterable[str]) -> dict[str, list[str]]:
        """``item_id -> sorted canonical keywords`` for a set of items (merge+mute).

        Used by the item lists to show each item's keyword tags without an
        N+1 query - one pass, folded through the alias/mute rules.
        """
        ids = sorted(set(item_ids))
        result: dict[str, set[str]] = {iid: set() for iid in ids}
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = self._conn.execute(
            f"select item_id, keyword from item_keywords where item_id in ({placeholders})",
            tuple(ids),
        )
        for item_id, keyword in rows:
            canonical = self._rules.apply(keyword)
            if canonical is not None:
                result[item_id].add(canonical)
        return {iid: sorted(kws) for iid, kws in result.items()}

    # --- category-scoped digest queries (GRP-40) -----------------------------

    def keyword_counts(self, window: Window, *, category: str | None = None) -> dict[str, int]:
        """``canonical keyword -> distinct-item count`` in the window (merge+mute)."""
        merged = self._merged_counts(window, category=category)
        return {kw: len(items) for kw, items in merged.items()}

    def category_item_count(self, window: Window, category: str) -> int:
        """Distinct items published in the window whose source is in ``category``."""
        (count,) = self._conn.execute(
            "select count(distinct i.item_id) "
            "from items i "
            "join sources s on s.source_id = i.source_id "
            "join source_groups g on g.group_id = s.group_id "
            "where i.published_at >= ? and i.published_at < ? and g.category = ?",
            (window.start, window.end, category),
        ).fetchone()
        return int(count)

    def top_items_for_keyword(
        self, window: Window, keyword: str, *, category: str | None = None, limit: int = 3
    ) -> list[ItemSummary]:
        """Most-recent items mentioning ``keyword`` (canonical) in the window.

        ``keyword`` is matched against the canonical form (alias/mute applied),
        so the caller passes an already-canonical term; ties break by item id.
        """
        item_ids = self._merged_counts(window, category=category).get(keyword, set())
        return self._item_summaries(sorted(item_ids))[:limit]

    def _item_summaries(self, item_ids: list[str]) -> list[ItemSummary]:
        """``ItemSummary`` rows for ``item_ids``, newest first (``published_at`` desc)."""
        if not item_ids:
            return []
        placeholders = ",".join("?" for _ in item_ids)
        rows = self._conn.execute(
            "select i.item_id, i.source_id, coalesce(s.name, i.source_id) as name, "
            "i.kind, i.title, i.canonical_url, i.published_at, i.summary, i.content_hash "
            "from items i "
            "left join sources s on s.source_id = i.source_id "
            f"where i.item_id in ({placeholders}) "
            "order by i.published_at desc, i.item_id desc",
            tuple(item_ids),
        )
        return [
            ItemSummary(
                item_id=iid,
                source_id=sid,
                source_name=name,
                kind=kind,
                title=title,
                canonical_url=url,
                published_at=pub,
                summary=summary,
                content_hash=chash,
            )
            for iid, sid, name, kind, title, url, pub, summary, chash in rows
        ]

    # --- digest detail (GRP-43) ----------------------------------------------

    def all_digests(self) -> list[DigestDetail]:
        """Every digest, body included, newest first (``created_at`` desc, id)."""
        rows = self._conn.execute(
            "select digest_id, kind, category, title, body_md, top_keywords, "
            "period_start, period_end, created_at "
            "from digests "
            "order by created_at desc, digest_id desc"
        )
        details: list[DigestDetail] = []
        for did, kind, cat, title, body_md, top_kw_json, ps, pe, created in rows:
            details.append(
                DigestDetail(
                    digest_id=did,
                    kind=kind,
                    category=cat,
                    title=title,
                    body_md=body_md,
                    top_keywords=_parse_chips(top_kw_json),
                    period_start=ps,
                    period_end=pe,
                    created_at=created,
                )
            )
        return details

    # --- keyword detail pages (GRP-44) ---------------------------------------

    def keyword_details(
        self,
        window: Window,
        *,
        min_mentions: int,
        items_per_kind: int = 5,
        co_limit: int = 10,
    ) -> dict[str, KeywordDetail]:
        """Build the F-TRD-02 detail dataset for every keyword above threshold.

        One pass over the window's keyword rows (folded through the alias/mute
        rules), then per keyword with ``>= min_mentions`` distinct items: a daily
        timeline, distinct-source count, count-ranked co-occurring keywords, and
        latest items grouped by kind. Deterministic - every list is sorted.
        """
        rows = self._conn.execute(
            "select ik.keyword, i.item_id, i.source_id, coalesce(s.name, i.source_id) as name, "
            "i.kind, i.title, i.canonical_url, i.published_at, i.summary, i.content_hash "
            "from item_keywords ik "
            "join items i on i.item_id = ik.item_id "
            "left join sources s on s.source_id = i.source_id "
            "where i.published_at >= ? and i.published_at < ?",
            (window.start, window.end),
        )
        items: dict[str, ItemSummary] = {}
        item_keywords: dict[str, set[str]] = {}
        kw_items: dict[str, set[str]] = {}
        for kw, iid, sid, name, kind, title, url, pub, summary, chash in rows:
            canonical = self._rules.apply(kw)
            if canonical is None:  # muted
                continue
            if iid not in items:
                items[iid] = ItemSummary(
                    item_id=iid,
                    source_id=sid,
                    source_name=name,
                    kind=kind,
                    title=title,
                    canonical_url=url,
                    published_at=pub,
                    summary=summary,
                    content_hash=chash,
                )
            item_keywords.setdefault(iid, set()).add(canonical)
            kw_items.setdefault(canonical, set()).add(iid)

        start_date = date.fromisoformat(window.start[:10])
        details: dict[str, KeywordDetail] = {}
        for keyword, iid_set in kw_items.items():
            if len(iid_set) < min_mentions:
                continue
            details[keyword] = self._keyword_detail(
                keyword,
                iid_set,
                items=items,
                item_keywords=item_keywords,
                window=window,
                start_date=start_date,
                items_per_kind=items_per_kind,
                co_limit=co_limit,
            )
        return details

    def _keyword_detail(  # noqa: PLR0913 - collaborators of one precomputed pass, all required
        self,
        keyword: str,
        iid_set: set[str],
        *,
        items: dict[str, ItemSummary],
        item_keywords: dict[str, set[str]],
        window: Window,
        start_date: date,
        items_per_kind: int,
        co_limit: int,
    ) -> KeywordDetail:
        member_items = [items[iid] for iid in iid_set]

        # daily timeline: one distinct-item count per day across the window
        timeline = [0] * window.days
        for item in member_items:
            index = (date.fromisoformat(item.published_at[:10]) - start_date).days
            timeline[min(max(index, 0), window.days - 1)] += 1

        # co-occurring keywords: distinct items sharing each other keyword
        co_counts: dict[str, int] = {}
        for iid in iid_set:
            for other in item_keywords[iid]:
                if other != keyword:
                    co_counts[other] = co_counts.get(other, 0) + 1
        co_occurring = [
            CoKeyword(keyword=kw, count=count)
            for kw, count in sorted(co_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:co_limit]
        ]

        # latest items grouped by kind (each newest first, capped)
        by_kind: dict[str, list[ItemSummary]] = {}
        for item in member_items:
            by_kind.setdefault(item.kind, []).append(item)
        items_by_kind = {
            kind: sorted(group, key=lambda i: (i.published_at, i.item_id), reverse=True)[
                :items_per_kind
            ]
            for kind, group in by_kind.items()
        }

        return KeywordDetail(
            keyword=keyword,
            slug=keyword_slug(keyword),
            count=len(iid_set),
            source_count=len({items[iid].source_id for iid in iid_set}),
            window_days=window.days,
            timeline=timeline,
            co_occurring=co_occurring,
            kinds=sorted(items_by_kind),
            items_by_kind=items_by_kind,
        )
