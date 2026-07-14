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

Next-scheduled-digest-run + last-digest-per-category (T4)
-----------------------------------------------------------
The health page also surfaces the next America/Edmonton digest-gate opening
(:func:`~grepify.site.next_digest.next_scheduled_run`, a pure function of the
build's injected ``clock``) and the most-recent digest per category
(:func:`~grepify.site.pages.latest_digest_per_category`, a pure fold over the
digests already queried for the digest pages). Neither reads new state - the
digest gate (GRP-45, ``grepify.digest.gating``) stays the single source of
truth for whether a digest actually runs; this only renders it.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import jinja2

from grepify.clock import Clock, to_iso
from grepify.config.provider import ConfigProvider
from grepify.config.schemas import SettingsConfig
from grepify.digest.periods import previous_day
from grepify.health import HealthSnapshot
from grepify.keywords import KeywordRules
from grepify.models import DigestKind, Source, SourceGroup
from grepify.paths import DataLayout
from grepify.repository.base import Repository
from grepify.site.next_digest import next_scheduled_run
from grepify.site.pages import (
    ITEMS_PER_PAGE,
    Page,
    build_health_view,
    build_pages,
    item_json,
    latest_digest_per_category,
    page_facets,
    rising_strip,
)
from grepify.site.render import (
    PageContext,
    SiteMeta,
    create_environment,
    render_page,
    render_stylesheet,
)
from grepify.site.trends import (
    DigestDetail,
    ItemSummary,
    KeywordDetail,
    TrendQueries,
    Window,
    open_cache,
    window_ending_at,
)
from grepify.site.urls import digest_slug, keyword_slug

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
    # Populate the `sources` table before the rebuild so top-sources /
    # latest-items resolve display names, not raw source_ids. (`build` runs
    # standalone, so it must load config itself like `ingest` does.)
    repository.load_config(config.groups(), config.sources())
    repository.rebuild_cache()
    settings = config.settings()
    rules = KeywordRules.from_config(config.keywords())

    conn = open_cache(layout)
    try:
        queries = TrendQueries(conn, rules)
        now = clock.now()
        env = create_environment()
        # Hash each static asset's bytes so pages reference it as
        # `static/<name>?v=<hash>`: a changed asset gets a new URL (no stale
        # Pages cache after deploy), identical bytes keep their hash (byte-stable
        # build). style.css is hashed from its rendered string, not a file.
        stylesheet = render_stylesheet(env)
        meta = SiteMeta(
            title=SITE_TITLE,
            base_path=base_path,
            generated_at=to_iso(now),
            run_id=run_id,
            asset_versions=_asset_versions(stylesheet),
        )

        cloud_window = window_ending_at(now, days=settings.windows.cloud_days)
        emission_since = to_iso(now - timedelta(days=EMISSION_DAYS))
        keyword_window = window_ending_at(now, days=settings.windows.keyword_days)

        _reset_output(output_dir)
        (output_dir / "static").mkdir(parents=True, exist_ok=True)
        _write(output_dir / "static" / "style.css", stylesheet)
        _copy_static(output_dir)

        emitted_items = queries.latest_items(limit=_ALL, since=emission_since)
        items_total = _count_items(conn)
        keyword_details = queries.keyword_details(
            keyword_window, min_mentions=settings.windows.keyword_min_mentions
        )
        keyword_pages = set(keyword_details)  # keywords that have a detail page
        digests = queries.all_digests()
        daily_exists = _daily_digest_exists(digests, config.groups(), now)

        pages_written = (
            _write_home(env, meta, output_dir, queries, cloud_window, keyword_pages, settings)
            + _write_items(env, meta, output_dir, queries, emitted_items)
            + _write_sources(env, meta, output_dir, config)
            + _write_health(
                env,
                meta,
                output_dir,
                layout,
                config=config,
                settings=settings,
                digests=digests,
                now=now,
                daily_exists=daily_exists,
            )
            + _write_digests(env, meta, output_dir, digests)
            + _write_keyword_pages(env, meta, output_dir, keyword_details)
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


def _write_home(  # noqa: PLR0913 - collaborators of one page render, all required
    env: jinja2.Environment,
    meta: SiteMeta,
    output_dir: Path,
    queries: TrendQueries,
    cloud_window: Window,
    keyword_pages: set[str],
    settings: SettingsConfig,
) -> int:
    latest_items = queries.latest_items()
    cloud = queries.cloud(
        cloud_window,
        rising_min_count=settings.digest.rising_min_count,
        rising_ratio=settings.digest.rising_ratio,
    )
    html = render_page(
        env,
        "home.html",
        PageContext(meta=meta, active="home"),
        cloud=cloud,
        rising=rising_strip(cloud),
        stats=queries.stats(cloud_window),
        top_sources=queries.top_sources(cloud_window),
        latest_items=latest_items,
        latest_digests=queries.latest_digests(),
        item_tags=queries.distinct_keywords_for_items(i.item_id for i in latest_items),
        keyword_pages=keyword_pages,
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


def _daily_digest_exists(
    digests: list[DigestDetail], groups: list[SourceGroup], now: datetime
) -> bool:
    """Has every enabled category's daily digest for the current period landed?

    Mirrors the existence check :func:`grepify.cli.digest_gate_command` feeds
    :func:`grepify.digest.gating.digest_gate` (GRP-63), so the health page's
    "next run" rolls to tomorrow exactly when the real gate would already
    consider the daily step done. A template digest (LLM degraded) does not
    count as present, matching the pipeline's own upgrade-on-retry rule.
    """
    # Deferred: grepify.digest imports grepify.digest.assemble, which imports
    # this module, so a module-level import would be circular.
    from grepify.digest import TEMPLATE_MODEL, digest_id_for  # noqa: PLC0415

    period_key = previous_day(now).key
    real_ids = {d.digest_id for d in digests if d.model != TEMPLATE_MODEL}
    categories = {g.category for g in groups if g.enabled}
    return all(
        digest_id_for(DigestKind.DAILY, category, period_key) in real_ids for category in categories
    )


def _write_health(  # noqa: PLR0913 - collaborators of one page render, all required
    env: jinja2.Environment,
    meta: SiteMeta,
    output_dir: Path,
    layout: DataLayout,
    *,
    config: ConfigProvider,
    settings: SettingsConfig,
    digests: list[DigestDetail],
    now: datetime,
    daily_exists: bool,
) -> int:
    sources = config.sources()
    quiet_kinds = set(settings.ingest.quiet_kinds)
    quiet_ids = {s.source_id for s in sources if s.kind in quiet_kinds}
    view = build_health_view(_load_health(layout), sources, quiet_source_ids=quiet_ids)
    html = render_page(
        env,
        "health.html",
        PageContext(meta=meta, active="health"),
        health_view=view,
        next_run=next_scheduled_run(now, daily_exists=daily_exists),
        category_digests=latest_digest_per_category(digests),
    )
    _write(output_dir / "health" / "index.html", html)
    return 1


def _write_digests(
    env: jinja2.Environment, meta: SiteMeta, output_dir: Path, digests: list[DigestDetail]
) -> int:
    """Digest index (always, a nav destination) + one detail page per digest.

    The index is server-rendered with *all* digests newest-first-by-period, then
    progressively enhanced by ``digests.js`` with an ``All`` / ``Following`` tab
    and a daily/weekly filter. Both are client-only, so with JS off the page
    degrades to the full ``All`` list - that all-digests baseline is the
    server-rendered surface the goldens cover.
    """
    index_html = render_page(
        env,
        "digest_index.html",
        PageContext(meta=meta, active="digests"),
        digests=digests,
    )
    _write(output_dir / "digest" / "index.html", index_html)

    for digest in digests:
        detail_html = render_page(
            env,
            "digest_detail.html",
            PageContext(meta=meta, active="digests"),
            digest=digest,
        )
        slug = digest_slug(digest.digest_id, digest.kind)
        _write(output_dir / "digest" / digest.kind / slug / "index.html", detail_html)
    return 1 + len(digests)


def _write_keyword_pages(
    env: jinja2.Environment,
    meta: SiteMeta,
    output_dir: Path,
    keyword_details: dict[str, KeywordDetail],
) -> int:
    """One detail page per keyword above threshold (F-SIT-04), sorted for stable order."""
    for keyword in sorted(keyword_details):
        detail = keyword_details[keyword]
        html = render_page(
            env,
            "keyword.html",
            PageContext(meta=meta, active=""),
            detail=detail,
        )
        _write(output_dir / "keyword" / keyword_slug(keyword) / "index.html", html)
    return len(keyword_details)


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


ASSET_HASH_LEN = 8  # short content hash for the `?v=` cache-buster


def _asset_hash(data: bytes) -> str:
    """Short, deterministic content hash for one static asset's bytes."""
    return hashlib.sha256(data).hexdigest()[:ASSET_HASH_LEN]


def _asset_versions(stylesheet: str) -> dict[str, str]:
    """Map every shipped ``static/`` asset to a short content hash.

    Covers the rendered ``style.css`` (passed in, since it is not a file on
    disk) plus each file copied by :func:`_copy_static`. Deterministic: the same
    asset bytes always yield the same hash, so the build stays byte-stable.
    """
    versions = {"style.css": _asset_hash(stylesheet.encode("utf-8"))}
    for asset in sorted(STATIC_DIR.glob("*")):
        if asset.is_file():
            versions[asset.name] = _asset_hash(asset.read_bytes())
    return versions


def _copy_static(output_dir: Path) -> None:
    for asset in sorted(STATIC_DIR.glob("*")):
        if asset.is_file():
            shutil.copyfile(asset, output_dir / "static" / asset.name)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
