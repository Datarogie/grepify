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

Coverage: last-contributed recency + quiet rollup (GRP-70)
------------------------------------------------------------
Both the sources page (a last-contributed column) and the health page (a
one-line "N of M live sources contributed nothing in <days> days" rollup)
render :func:`~grepify.site.pages.build_source_rows` /
:func:`~grepify.site.pages.coverage_rollup`, computed once per build from
``TrendQueries.last_contributed_at`` (all-time, not the trailing-90d emission
window) and ``settings.windows.coverage_quiet_days``. Quiet is scoped to live
(enabled) sources only - a dead/paywalled source's silence is already
explained by its lifecycle class (GRP-66), not double-counted as coverage
quiet.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import jinja2

from grepify.clock import Clock, to_iso
from grepify.config.provider import ConfigProvider
from grepify.config.schemas import SettingsConfig
from grepify.digest import TEMPLATE_MODEL, digest_id_for
from grepify.digest.periods import previous_day
from grepify.health import HealthSnapshot
from grepify.keywords import KeywordRules
from grepify.models import DigestKind, SourceGroup
from grepify.path_safety import ContainedPath, ensure_safe_output_dir
from grepify.paths import DataLayout
from grepify.repository.base import Repository
from grepify.site.next_digest import next_scheduled_run
from grepify.site.pages import (
    ITEMS_PER_PAGE,
    CoverageRollup,
    Page,
    SourceRow,
    build_health_view,
    build_pages,
    build_source_rows,
    coverage_rollup,
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
    open_cache,
)
from grepify.site.urls import digest_slug, keyword_slug
from grepify.windows import Window, window_ending_at

EMISSION_DAYS = 90  # trailing-window paginated into public/ (F-SIT-03/04, §9)
SITE_TITLE = "grepify"
STATIC_DIR = Path(__file__).parent / "static"
_ALL = 10_000_000  # effectively-unbounded limit for the emission query


class _BackupIdentityMismatchError(RuntimeError):
    pass


@dataclass(frozen=True)
class _DestinationState:
    exists: bool
    device: int | None = None
    inode: int | None = None
    mode: int | None = None


@dataclass(frozen=True)
class _PublishFailure:
    stage_dir: Path
    output_dir: Path
    backup_dir: Path
    moved_existing: bool
    replace: Callable[[Path, Path], None]
    original: BaseException


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
    protected_roots: tuple[Path, ...] = (),
) -> BuildResult:
    """Render the whole site into ``output_dir`` from the cache + config."""
    safe_output_dir = ensure_safe_output_dir(
        output_dir, cwd=Path.cwd(), protected_roots=(layout.root, *protected_roots)
    )
    expected_output_state = _capture_destination_state(safe_output_dir)
    stage_dir: Path | None = None
    build_complete = False

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

        stage_dir = _temporary_sibling(safe_output_dir)
        output = ContainedPath.create(stage_dir)
        output.join("static").mkdir(parents=True, exist_ok=True)
        _write(output, Path("static/style.css"), stylesheet)
        _copy_static(output)

        emitted_items = queries.latest_items(limit=_ALL, since=emission_since)
        items_total = _count_items(conn)
        keyword_details = queries.keyword_details(
            keyword_window, min_mentions=settings.windows.keyword_min_mentions
        )
        keyword_pages = set(keyword_details)  # keywords that have a detail page
        digests = queries.all_digests()
        daily_exists = _daily_digest_exists(digests, config.groups(), now)

        quiet_after_days = settings.windows.coverage_quiet_days
        source_rows = build_source_rows(
            config.sources(),
            queries.last_contributed_at(),
            now=now,
            quiet_after_days=quiet_after_days,
        )
        coverage = coverage_rollup(source_rows, quiet_after_days=quiet_after_days)

        pages_written = (
            _write_home(env, meta, output, queries, cloud_window, keyword_pages, settings)
            + _write_items(env, meta, output, queries, emitted_items)
            + _write_sources(env, meta, output, config, source_rows, coverage)
            + _write_health(
                env,
                meta,
                output,
                layout,
                config=config,
                settings=settings,
                digests=digests,
                now=now,
                daily_exists=daily_exists,
                coverage=coverage,
            )
            + _write_digests(env, meta, output, digests)
            + _write_keyword_pages(env, meta, output, keyword_details)
        )
        build_complete = True
    finally:
        conn.close()
        if not build_complete and stage_dir is not None and stage_dir.exists():
            shutil.rmtree(stage_dir)

    if stage_dir is None:
        raise RuntimeError("build did not create a staging directory")
    _replace_output(stage_dir, safe_output_dir, expected_output_state=expected_output_state)

    return BuildResult(
        output_dir=safe_output_dir,
        pages_written=pages_written,
        items_emitted=len(emitted_items),
        items_total=items_total,
    )


# --- page writers ------------------------------------------------------------


def _write_home(  # noqa: PLR0913 - collaborators of one page render, all required
    env: jinja2.Environment,
    meta: SiteMeta,
    output_dir: ContainedPath,
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
    _write(output_dir, Path("index.html"), html)
    return 1


def _write_items(
    env: jinja2.Environment,
    meta: SiteMeta,
    output_dir: ContainedPath,
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
            _write(output_dir, Path("items/index.html"), html)
            _write(output_dir, Path("items/page-1.json"), pretty)
        else:
            _write(output_dir, Path("items") / f"page-{page.number}" / "index.html", html)
            _write(output_dir, Path("items") / f"page-{page.number}.json", pretty)
    return len(pages)


def _write_sources(  # noqa: PLR0913 - collaborators of one page render, all required
    env: jinja2.Environment,
    meta: SiteMeta,
    output_dir: ContainedPath,
    config: ConfigProvider,
    source_rows: list[SourceRow],
    coverage: CoverageRollup,
) -> int:
    groups = sorted(config.groups(), key=lambda g: g.group_id)
    by_group: dict[str, list[SourceRow]] = {}
    for row in source_rows:
        by_group.setdefault(row.group_id, []).append(row)
    grouped = [
        (group, sorted(by_group.get(group.group_id, []), key=lambda r: r.source_id))
        for group in groups
    ]
    html = render_page(
        env,
        "sources.html",
        PageContext(meta=meta, active="sources"),
        grouped=grouped,
        source_count=len(source_rows),
        coverage=coverage,
    )
    _write(output_dir, Path("sources/index.html"), html)
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
    period_key = previous_day(now).key
    real_ids = {d.digest_id for d in digests if d.model != TEMPLATE_MODEL}
    categories = {g.category for g in groups if g.enabled}
    return all(
        digest_id_for(DigestKind.DAILY, category, period_key) in real_ids for category in categories
    )


def _write_health(  # noqa: PLR0913 - collaborators of one page render, all required
    env: jinja2.Environment,
    meta: SiteMeta,
    output_dir: ContainedPath,
    layout: DataLayout,
    *,
    config: ConfigProvider,
    settings: SettingsConfig,
    digests: list[DigestDetail],
    now: datetime,
    daily_exists: bool,
    coverage: CoverageRollup,
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
        coverage=coverage,
    )
    _write(output_dir, Path("health/index.html"), html)
    return 1


def _write_digests(
    env: jinja2.Environment, meta: SiteMeta, output_dir: ContainedPath, digests: list[DigestDetail]
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
    _write(output_dir, Path("digest/index.html"), index_html)

    for digest in digests:
        detail_html = render_page(
            env,
            "digest_detail.html",
            PageContext(meta=meta, active="digests"),
            digest=digest,
        )
        slug = digest_slug(digest.digest_id, digest.kind)
        _write(output_dir, Path("digest") / digest.kind / slug / "index.html", detail_html)
    return 1 + len(digests)


def _write_keyword_pages(
    env: jinja2.Environment,
    meta: SiteMeta,
    output_dir: ContainedPath,
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
        _write(output_dir, Path("keyword") / keyword_slug(keyword) / "index.html", html)
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


def _temporary_sibling(output_dir: Path) -> Path:
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    stage_dir = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}.", suffix=".tmp", dir=output_dir.parent)
    )
    stage_dir.chmod(0o700)
    return stage_dir


def _replace_output(
    stage_dir: Path,
    output_dir: Path,
    *,
    expected_output_state: _DestinationState,
    replace: Callable[[Path, Path], None] = os.replace,
) -> None:
    if not _destination_matches(output_dir, expected_output_state):
        _remove_owned_stage(stage_dir)
        raise RuntimeError(f"output destination changed during build: {output_dir}")
    if not expected_output_state.exists:
        _publish_into_absent_output(stage_dir, output_dir)
        return

    backup_dir = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}.", suffix=".previous", dir=output_dir.parent)
    )
    backup_dir.rmdir()
    moved_existing = False
    try:
        replace(output_dir, backup_dir)
        moved_existing = True
        if _capture_destination_state(backup_dir) != expected_output_state:
            _handle_backup_identity_mismatch(
                stage_dir=stage_dir,
                output_dir=output_dir,
                backup_dir=backup_dir,
                replace=replace,
            )
        if _path_occupied(output_dir):
            raise RuntimeError(
                f"output destination became occupied; previous output preserved at {backup_dir}"
            )
        replace(stage_dir, output_dir)
    except _BackupIdentityMismatchError:
        raise
    except BaseException as exc:
        _handle_publish_failure(
            _PublishFailure(
                stage_dir=stage_dir,
                output_dir=output_dir,
                backup_dir=backup_dir,
                moved_existing=moved_existing,
                replace=replace,
                original=exc,
            )
        )
        raise
    if backup_dir.exists():
        shutil.rmtree(backup_dir)


def _publish_into_absent_output(stage_dir: Path, output_dir: Path) -> None:
    try:
        output_dir.mkdir()
    except FileExistsError as exc:
        _remove_owned_stage(stage_dir)
        raise RuntimeError(f"output destination changed during build: {output_dir}") from exc
    try:
        for child in stage_dir.iterdir():
            os.replace(child, output_dir / child.name)
        stage_dir.rmdir()
    except BaseException:
        if output_dir.exists():
            shutil.rmtree(output_dir)
        _remove_owned_stage(stage_dir)
        raise


def _handle_backup_identity_mismatch(
    *,
    stage_dir: Path,
    output_dir: Path,
    backup_dir: Path,
    replace: Callable[[Path, Path], None],
) -> None:
    _remove_owned_stage(stage_dir)
    preserved_at = backup_dir
    if not _path_occupied(output_dir):
        replace(backup_dir, output_dir)
        preserved_at = output_dir
    raise _BackupIdentityMismatchError(
        f"output backup identity mismatch; moved entry preserved at {preserved_at}"
    )


def _remove_owned_stage(stage_dir: Path) -> None:
    if stage_dir.exists():
        shutil.rmtree(stage_dir)


def _handle_publish_failure(failure: _PublishFailure) -> None:
    if not failure.moved_existing:
        _remove_owned_stage(failure.stage_dir)
        return
    if not _path_occupied(failure.output_dir):
        failure.replace(failure.backup_dir, failure.output_dir)
        _remove_owned_stage(failure.stage_dir)
        return
    raise RuntimeError(
        f"output publish failed and destination is occupied; "
        f"previous output preserved at {failure.backup_dir}"
    ) from failure.original


def _destination_matches(path: Path, expected: _DestinationState) -> bool:
    return _capture_destination_state(path) == expected


def _capture_destination_state(path: Path) -> _DestinationState:
    try:
        stat = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return _DestinationState(exists=False)
    return _DestinationState(
        exists=True,
        device=stat.st_dev,
        inode=stat.st_ino,
        mode=stat.st_mode,
    )


def _path_occupied(path: Path) -> bool:
    return _capture_destination_state(path).exists


def _copy_static(output_dir: ContainedPath) -> None:
    for asset in sorted(STATIC_DIR.glob("*")):
        if asset.is_file():
            shutil.copyfile(asset, output_dir.join("static", asset.name))


def _write(output_dir: ContainedPath, path: Path, content: str) -> None:
    destination = output_dir.resolve(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")
