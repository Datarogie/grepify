"""Site rendering (E3): SQLite cache + config → byte-stable ``public/``.

The v1 static-site layer (PRD §5 "Jinja SSG"). Public surface later E3/E4
issues build on:

- Render skeleton (GRP-30) - :func:`create_environment`, :func:`render_page`,
  :func:`render_stylesheet`, :class:`SiteMeta`/:class:`PageContext`/:class:`NavLink`,
  :func:`cloud_weight_bucket`, :data:`STYLE_TOKENS`/:data:`LIGHT_STYLE_TOKENS`,
  and :func:`sparkline_svg`.
- Trend queries (GRP-31) - :class:`TrendQueries` and its datasets
  (:class:`CloudDataset`, :class:`Stats`, :class:`SourceCount`,
  :class:`ItemSummary`, :class:`DigestSummary`), plus :func:`window_ending_at` /
  :func:`previous_window` / :func:`open_cache`.
- Page helpers (GRP-32/33/34) - :func:`collapse_near_duplicates`,
  :func:`paginate`, :func:`item_matches_filter`, :class:`ItemGroup`,
  :class:`Page`.
- Build orchestrator (GRP-35) - :func:`build_site` + :class:`BuildResult`.

Determinism (F-SIT-08 / S8): the clock is injected (never read in the render
path), all dict/set iteration is sorted before templating, and snapshot tests
render twice in a row and assert byte-identical output.

Failure modes
-------------
None of its own - a re-export aggregator. See :mod:`grepify.site.render`,
:mod:`grepify.site.trends`, :mod:`grepify.site.sparkline`, and
:mod:`grepify.site.tokens` for module-level failure modes.
"""

from __future__ import annotations

from grepify.site.build import BuildResult, build_site
from grepify.site.pages import (
    ItemGroup,
    Page,
    build_pages,
    collapse_near_duplicates,
    item_matches_filter,
    paginate,
)
from grepify.site.render import (
    NAV,
    NavLink,
    PageContext,
    SiteMeta,
    cloud_weight_bucket,
    create_environment,
    render_page,
    render_stylesheet,
)
from grepify.site.sparkline import sparkline_svg
from grepify.site.tokens import CLOUD_BUCKETS, LIGHT_STYLE_TOKENS, STYLE_TOKENS
from grepify.site.trends import (
    CloudDataset,
    DigestSummary,
    ItemSummary,
    KeywordCount,
    SourceCount,
    Stats,
    TrendQueries,
    Window,
    open_cache,
    previous_window,
    window_ending_at,
)

__all__ = [
    "CLOUD_BUCKETS",
    "LIGHT_STYLE_TOKENS",
    "NAV",
    "STYLE_TOKENS",
    "BuildResult",
    "CloudDataset",
    "DigestSummary",
    "ItemGroup",
    "ItemSummary",
    "KeywordCount",
    "NavLink",
    "Page",
    "PageContext",
    "SiteMeta",
    "SourceCount",
    "Stats",
    "TrendQueries",
    "Window",
    "build_pages",
    "build_site",
    "cloud_weight_bucket",
    "collapse_near_duplicates",
    "create_environment",
    "item_matches_filter",
    "open_cache",
    "paginate",
    "previous_window",
    "render_page",
    "render_stylesheet",
    "sparkline_svg",
    "window_ending_at",
]
