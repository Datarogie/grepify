"""Build orchestrator (GRP-35): SQLite cache + config → byte-stable ``public/``.

The one place the site's I/O happens. :func:`build_site` rebuilds the cache from
truth, opens a read connection, queries the trend datasets (GRP-31), shapes them
with the page helpers (GRP-32/33/34), renders the templates (GRP-30), and writes
``public/``. It is a **pure function of the cache + config + injected clock**
(F-SIT-08): same inputs → identical bytes.

Trailing-90d emission (F-SIT-03/04, §9)
---------------------------------------
Only items published in the trailing :data:`EMISSION_DAYS` are paginated into
``public/`` (older data stays queryable via the cache/DB but is not emitted),
bounding build time + output size at 50k+ items. The cloud/stats windows
(``settings.windows.cloud_days``) are independent and narrower.

Output tree::

    public/
      index.html                 # home (GRP-32)
      items/index.html           # items browser page 1 (GRP-33)
      items/page-<n>/index.html  # pages 2..N
      items/page-<n>.json        # emitted per-page JSON (filters.js contract)
      sources/index.html         # sources page (GRP-34)
      health/index.html          # health page (GRP-34)
      static/style.css           # rendered from tokens
      static/filters.js          # client-side items filter (vanilla)

Failure modes
-------------
- ``Repository.rebuild_cache()`` failure → :class:`~grepify.errors.RepositoryError`
  (systemic; the build can't proceed on a broken cache).
- Bad config → :class:`~grepify.errors.ConfigError` from the ``ConfigProvider``.
- A missing/unreadable ``health.json`` renders an **empty** health page (health
  is best-effort, PRD §8) - it never fails the build.
- Writing ``public/`` propagates ``OSError`` (e.g. read-only fs), same as every
  other data-root write in the package.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import jinja2

from grepify.clock import Clock, to_iso
from grepify.config.provider import ConfigProvider
from grepify.health import HealthSnapshot
from grepify.keywords import KeywordRules
from grepify.models import Source
from grepify.paths import DataLayout
from grepify.repository.base import Repository
from grepify.site.pages import (
    ITEMS_PER_PAGE,
    Page,
    build_pages,
    item_json,
    page_facets,
)
from grepify.site.render import (
    PageContext,
    SiteMeta,
    create_environment,
    render_page,
    render_stylesheet,
)
from grepify.site.trends import (
    ItemSummary,
    TrendQueries,
    Window,
    open_cache,
    window_ending_at,
)

EMISSION_DAYS = 90  # trailing-window paginated into public/ (F-SIT-03/04, §9)
SITE_TITLE = "grepify"
STATIC_DIR = Path(__file__).parent / "static"
_ALL = 10_000_000  # effectively-unbounded limit for the emission query


@dataclass(frozen=True)
class BuildResult:
    """Rollup for the ``build`` CLI's run manifest."""

    output_dir: Path
    pages_written: int
    items_emitted: int
    items_total: int


def build_site(  # noqa: PLR0913 - distinct collaborators, all required
    *,
    config: ConfigProvider,
    repository: Repository,
    layout: DataLayout,
    clock: Clock,
    run_id: str,
    output_dir: Path,
    base_path: str = "/",
) -> BuildResult:
    """Render the whole site into ``output_dir`` from the cache + config."""
    # Project config-derived sources/groups into the cache before the rebuild
    # so the `sources` table is populated - otherwise top-sources / latest-items
    # would fall back to raw source_ids instead of display names. (`ingest`
    # loads config in its own process; `build` runs standalone and must too.)
    repository.load_config(config.groups(), config.sources())
    repository.rebuild_cache()
    settings = config.settings()
    rules = KeywordRules.from_config(config.keywords())

    conn = open_cache(layout)
    try:
        queries = TrendQueries(conn, rules)
        now = clock.now()
        meta = SiteMeta(
            title=SITE_TITLE,
            base_path=base_path,
            generated_at=to_iso(now),
            run_id=run_id,
        )
        env = create_environment()

        cloud_window = window_ending_at(now, days=settings.windows.cloud_days)
        emission_since = to_iso(now - timedelta(days=EMISSION_DAYS))

        _reset_output(output_dir)
        (output_dir / "static").mkdir(parents=True, exist_ok=True)
        _write(output_dir / "static" / "style.css", render_stylesheet(env))
        _copy_static(output_dir)

        emitted_items = queries.latest_items(limit=_ALL, since=emission_since)
        items_total = _count_items(conn)

        pages_written = (
            _write_home(env, meta, output_dir, queries, cloud_window)
            + _write_items(env, meta, output_dir, queries, emitted_items)
            + _write_sources(env, meta, output_dir, config)
            + _write_health(env, meta, output_dir, layout)
        )
    finally:
        conn.close()

    return BuildResult(
        output_dir=output_dir,
        pages_written=pages_written,
        items_emitted=len(emitted_items),
        items_total=items_total,
    )


# --- page writers ------------------------------------------------------------


def _write_home(
    env: jinja2.Environment,
    meta: SiteMeta,
    output_dir: Path,
    queries: TrendQueries,
    cloud_window: Window,
) -> int:
    latest_items = queries.latest_items()
    html = render_page(
        env,
        "home.html",
        PageContext(meta=meta, active="home"),
        cloud=queries.cloud(cloud_window),
        stats=queries.stats(cloud_window),
        top_sources=queries.top_sources(cloud_window),
        latest_items=latest_items,
        latest_digests=queries.latest_digests(),
        item_tags=queries.distinct_keywords_for_items(i.item_id for i in latest_items),
    )
    _write(output_dir / "index.html", html)
    return 1


def _write_items(
    env: jinja2.Environment,
    meta: SiteMeta,
    output_dir: Path,
    queries: TrendQueries,
    emitted_items: list[ItemSummary],
) -> int:
    pages = build_pages(emitted_items, per_page=ITEMS_PER_PAGE)
    item_tags = queries.distinct_keywords_for_items(i.item_id for i in emitted_items)

    for page in pages:
        payload = _page_payload(page, item_tags)
        html = render_page(
            env,
            "items.html",
            PageContext(meta=meta, active="items"),
            page=page,
            item_tags=item_tags,
        )
        pretty = json.dumps(payload, indent=2, sort_keys=True)
        if page.number == 1:
            _write(output_dir / "items" / "index.html", html)
            _write(output_dir / "items" / "page-1.json", pretty)
        else:
            _write(output_dir / "items" / f"page-{page.number}" / "index.html", html)
            _write(output_dir / "items" / f"page-{page.number}.json", pretty)
    return len(pages)


def _write_sources(
    env: jinja2.Environment, meta: SiteMeta, output_dir: Path, config: ConfigProvider
) -> int:
    groups = sorted(config.groups(), key=lambda g: g.group_id)
    sources = config.sources()
    by_group: dict[str, list[Source]] = {}
    for source in sources:
        by_group.setdefault(source.group_id, []).append(source)
    grouped = [
        (group, sorted(by_group.get(group.group_id, []), key=lambda s: s.source_id))
        for group in groups
    ]
    html = render_page(
        env,
        "sources.html",
        PageContext(meta=meta, active="sources"),
        grouped=grouped,
        source_count=len(sources),
    )
    _write(output_dir / "sources" / "index.html", html)
    return 1


def _write_health(
    env: jinja2.Environment, meta: SiteMeta, output_dir: Path, layout: DataLayout
) -> int:
    html = render_page(
        env,
        "health.html",
        PageContext(meta=meta, active="health"),
        health=_load_health(layout),
    )
    _write(output_dir / "health" / "index.html", html)
    return 1


# --- helpers -----------------------------------------------------------------


def _page_payload(page: Page, item_tags: dict[str, list[str]]) -> dict[str, object]:
    items = [
        item_json(
            group.representative,
            keywords=item_tags.get(group.representative.item_id, []),
            similar_count=group.similar_count,
        )
        for group in page.groups
    ]
    return {
        "page": page.number,
        "total_pages": page.total_pages,
        "items": items,
        "facets": page_facets(page, item_tags),
    }


def _load_health(layout: DataLayout) -> HealthSnapshot | None:
    path = layout.health_file
    if not path.is_file():
        return None
    try:
        return HealthSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError:
        # best-effort: a malformed snapshot renders an empty health page,
        # never fails the build (health is informational, PRD §8).
        return None


def _count_items(conn: sqlite3.Connection) -> int:
    (count,) = conn.execute("select count(*) from items").fetchone()
    return int(count)


def _reset_output(output_dir: Path) -> None:
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)


def _copy_static(output_dir: Path) -> None:
    for asset in sorted(STATIC_DIR.glob("*")):
        if asset.is_file():
            shutil.copyfile(asset, output_dir / "static" / asset.name)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
