"""Per-category digest input assembler (E4, GRP-40, PRD §8 F-DIG-01/F-TRD-03).

Deterministic, **category-keyed** (never per-user, PRD §2/§7): for one category
and one period it gathers everything the digest generator (GRP-41/42) needs -
the category's in-window item count (the skip-threshold input, F-DIG-03), its
top-N keywords by distinct-item count (after alias/mute), each flagged rising or
not (:mod:`grepify.digest.rising`) and carrying its top item titles/summaries,
and the rising subset. It reads only the derived cache via
:class:`~grepify.site.trends.TrendQueries`, so it is pure and offline-testable.

Determinism (F-SIT-08 / S8)
---------------------------
The period/window come from an injected instant (:mod:`grepify.digest.periods`),
never a clock read here; keywords are ranked ``(-count, keyword)`` and each
keyword's items ``published_at desc, item_id`` - a total order, byte-stable.

Failure modes
-------------
Pure reads over the cache; surfaces only the underlying ``sqlite3`` error if the
cache is missing/corrupt (a systemic build fault the caller owns). Nothing here
touches the network or the LLM.
"""

from __future__ import annotations

from dataclasses import dataclass

from grepify.config.schemas import DigestSettings
from grepify.digest.periods import Period
from grepify.digest.rising import is_rising
from grepify.models import DigestKind
from grepify.site.trends import ItemSummary, TrendQueries, Window, previous_window


@dataclass(frozen=True)
class KeywordBrief:
    """One keyword's digest input: its counts, rising flag, and top items."""

    keyword: str
    count: int
    previous_count: int
    rising: bool
    items: list[ItemSummary]


@dataclass(frozen=True)
class DigestInput:
    """The deterministic input for one category's digest (GRP-40)."""

    category: str
    kind: DigestKind
    period: Period
    item_count: int
    keywords: list[KeywordBrief]

    @property
    def rising_keywords(self) -> list[str]:
        """The subset of ``keywords`` flagged rising, in ranked order."""
        return [kw.keyword for kw in self.keywords if kw.rising]


def assemble_digest_input(
    queries: TrendQueries,
    *,
    category: str,
    kind: DigestKind,
    period: Period,
    settings: DigestSettings,
) -> DigestInput:
    """Assemble the digest input for ``category`` over ``period`` (GRP-40)."""
    window = Window(start=period.start, end=period.end, days=period.days)
    prev = previous_window(window)

    current = queries.keyword_counts(window, category=category)
    previous = queries.keyword_counts(prev, category=category)

    top_n = (
        settings.weekly_top_keywords if kind is DigestKind.WEEKLY else settings.daily_top_keywords
    )
    ranked = sorted(current.items(), key=lambda kv: (-kv[1], kv[0]))[:top_n]

    briefs = [
        KeywordBrief(
            keyword=keyword,
            count=count,
            previous_count=previous.get(keyword, 0),
            rising=is_rising(
                count,
                previous.get(keyword, 0),
                min_count=settings.rising_min_count,
                ratio=settings.rising_ratio,
            ),
            items=queries.top_items_for_keyword(
                window, keyword, category=category, limit=settings.items_per_keyword
            ),
        )
        for keyword, count in ranked
    ]

    return DigestInput(
        category=category,
        kind=kind,
        period=period,
        item_count=queries.category_item_count(window, category),
        keywords=briefs,
    )
