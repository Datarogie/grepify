"""Build orchestrator tests (GRP-32/33/34/35): snapshots, determinism, emission.

Builds a canned site into a temp dir with a :class:`FixedClock` + fixed run id
(so ``generated_at``/``run_id`` are stable) and snapshots the four pages against
committed goldens under ``tests/fixtures/site/pages/``. Also covers the
trailing-90d emission rule, pagination, near-dup collapse in the output, static
assets, the missing-health path, and determinism (build twice → identical
bytes, the S8 "passes twice in CI" rule).
"""

from __future__ import annotations

import hashlib
import json
import re
import textwrap
from datetime import UTC, datetime
from pathlib import Path

from grepify.clock import FixedClock
from grepify.config.filesystem import FilesystemConfigProvider
from grepify.health import ErrorClass, HealthSnapshot, SourceHealth
from grepify.models import ExtractionMethod, FetchStatus, Item, ItemKeyword, SourceKind
from grepify.paths import DataLayout
from grepify.repository.jsonl_sqlite import JsonlSqliteRepository
from grepify.site.build import BuildResult, build_site

GOLDEN = Path(__file__).parent / "fixtures" / "site" / "pages"
_CLOCK = FixedClock(datetime(2026, 7, 8, tzinfo=UTC))
_RUN_ID = "20260709T120000Z-testrun"

_SETTINGS = textwrap.dedent(
    """
    llm:
      active_profile: p
      profiles:
        p: {endpoint: openai-compat, model: m, max_calls_per_run: 40}
    windows:
      cloud_days: 7
    """
).strip()

_KEYWORDS = textwrap.dedent(
    """
    aliases:
      "gen ai": genai
    mute:
      - webinar
    """
).strip()

_KEYWORDS_WITH_PIN = textwrap.dedent(
    """
    aliases:
      "gen ai": genai
    mute:
      - webinar
    pin:
      - anthropic
      - dbt
    """
).strip()

_GROUP = textwrap.dedent(
    """
    group: ai-research
    name: AI Research
    category: ai
    sources:
      - {id: s1, kind: rss, name: Source One, url: 'https://ex.com/one/feed'}
      - {id: s2, kind: youtube, name: Source Two, channel_id: UC123}
    """
).strip()


def _item(
    item_id: str, *, source_id: str, published_at: str, title: str, content_hash: str
) -> Item:
    return Item(
        item_id=item_id,
        source_id=source_id,
        kind=SourceKind.RSS if source_id == "s1" else SourceKind.YOUTUBE,
        external_id=item_id,
        canonical_url=f"https://ex.com/{item_id}",
        title=title,
        summary="a summary",
        published_at=published_at,
        fetched_at="2026-07-08T11:00:00+00:00",
        content_hash=content_hash,
    )


def _kw(
    item_id: str, keyword: str, rank: int = 1, method: ExtractionMethod = ExtractionMethod.LLM
) -> ItemKeyword:
    return ItemKeyword(
        item_id=item_id,
        keyword=keyword,
        rank=rank,
        method=method,
        model="m" if method is ExtractionMethod.LLM else None,
        extracted_at="2026-07-08T12:00:00+00:00",
    )


def build_canned(  # noqa: PLR0913 - test fixture builder, each knob is a distinct scenario
    tmp_path: Path,
    *,
    extra_recent_items: int = 0,
    rising_items: int = 0,
    with_health: bool = True,
    base_path: str = "/grepify/",
    keywords_yaml: str = _KEYWORDS,
) -> tuple[BuildResult, Path]:
    """Materialize config + truth, build the site, return (result, output_dir)."""
    conf = tmp_path / "sources"
    (conf / "groups").mkdir(parents=True, exist_ok=True)
    (conf / "settings.yml").write_text(_SETTINGS, encoding="utf-8")
    (conf / "keywords.yml").write_text(keywords_yaml, encoding="utf-8")
    (conf / "groups" / "ai-research.yml").write_text(_GROUP, encoding="utf-8")

    data = tmp_path / "data"
    repo = JsonlSqliteRepository(data)
    items = [
        _item(
            "i1",
            source_id="s1",
            published_at="2026-07-05T10:00:00+00:00",
            title="OpenAI ships GPT-5",
            content_hash="0000000000000001",
        ),
        _item(
            "i2",
            source_id="s2",
            published_at="2026-07-06T10:00:00+00:00",
            title="OpenAI ships GPT-5!",
            content_hash="0000000000000003",
        ),  # near-dup of i1
        _item(
            "i3",
            source_id="s1",
            published_at="2026-07-07T10:00:00+00:00",
            title="Anthropic releases Claude",
            content_hash="ffffffffffffffff",
        ),
        _item(
            "old",
            source_id="s1",
            published_at="2026-01-01T10:00:00+00:00",
            title="Ancient news",
            content_hash="00ff00ff00ff00ff",
        ),  # >90d → not emitted
    ]
    items += [
        _item(
            f"x{n}",
            source_id="s1",
            published_at=f"2026-07-0{1 + n % 7}T0{n % 9}:00:00+00:00",
            title=f"Filler story {n}",
            content_hash=hashlib.blake2b(f"x{n}".encode(), digest_size=8).hexdigest(),
        )
        for n in range(extra_recent_items)
    ]
    # N items all tagged "riseterm" and published inside the cloud window with
    # none in the prior window: is_rising() surges from nothing (previous_count
    # == 0) once the count clears rising_min_count.
    items += [
        _item(
            f"r{n}",
            source_id="s1",
            published_at="2026-07-06T10:00:00+00:00",
            title=f"Rising story {n}",
            content_hash=hashlib.blake2b(f"r{n}".encode(), digest_size=8).hexdigest(),
        )
        for n in range(rising_items)
    ]
    repo.add_items(items)
    repo.add_item_keywords(
        [
            _kw("i1", "genai"),
            _kw("i1", "webinar", 2),  # muted
            _kw("i2", "genai"),
            _kw("i3", "agents"),
            _kw("i3", "anthropic", 2),
            _kw("i3", "agentic coding", 3),  # multi-word phrase (filters.js round-trip)
            _kw("old", "genai"),
            *[_kw(f"r{n}", "riseterm") for n in range(rising_items)],
        ]
    )
    repo.close()

    if with_health:
        snapshot = HealthSnapshot(
            run_id="prior-run",
            generated_at="2026-07-08T09:00:00+00:00",
            sources=[
                SourceHealth(
                    source_id="s1",
                    attempts=10,
                    last_status=FetchStatus.OK,
                    last_started_at="2026-07-08T08:00:00+00:00",
                    consecutive_failures=0,
                    flagged=False,
                ),
                SourceHealth(
                    source_id="s2",
                    attempts=8,
                    last_status=FetchStatus.ERROR,
                    last_started_at="2026-07-08T08:00:00+00:00",
                    last_error="returned HTTP 429 Too Many Requests",
                    error_class=ErrorClass.HTTP_4XX,
                    consecutive_failures=6,
                    flagged=True,
                ),
            ],
        )
        layout = DataLayout(data)
        layout.health_file.parent.mkdir(parents=True, exist_ok=True)
        layout.health_file.write_text(snapshot.model_dump_json(indent=2) + "\n", encoding="utf-8")

    result = build_site(
        config=FilesystemConfigProvider(conf),
        repository=JsonlSqliteRepository(data),
        layout=DataLayout(data),
        clock=_CLOCK,
        run_id=_RUN_ID,
        output_dir=tmp_path / "public",
        base_path=base_path,
    )
    return result, tmp_path / "public"


# --- snapshots ---------------------------------------------------------------


def test_home_matches_golden(tmp_path: Path) -> None:
    _, out = build_canned(tmp_path)
    assert (out / "index.html").read_text(encoding="utf-8") == (GOLDEN / "index.html").read_text(
        encoding="utf-8"
    )


def test_home_pin_fixture_matches_golden(tmp_path: Path) -> None:
    # End-to-end pin wiring through a real keywords.yml: config parse ->
    # KeywordRules -> cloud() -> home template. "anthropic" already shows within
    # the default cloud limit, so pinning it is a byte-stable no-op; "dbt" has
    # zero mentions, so it stays absent (pin never invents a mention). The
    # below-the-cutoff injection is covered by the unit tests in
    # test_site_trends.py.
    _, out = build_canned(tmp_path, keywords_yaml=_KEYWORDS_WITH_PIN)
    index_html = (out / "index.html").read_text(encoding="utf-8")
    assert index_html == (GOLDEN / "index_pin.html").read_text(encoding="utf-8")
    assert "dbt" not in index_html


def test_home_rising_strip_hidden_when_nothing_rising(tmp_path: Path) -> None:
    # The canned cloud keywords all stay below rising_min_count (3), so nothing
    # is rising: the strip renders no chrome at all, not even its own heading.
    _, out = build_canned(tmp_path)
    index_html = (out / "index.html").read_text(encoding="utf-8")
    assert "Rising this week" not in index_html
    assert "rising-strip" not in index_html


def test_home_rising_strip_matches_golden_populated(tmp_path: Path) -> None:
    # 3 items tagged "riseterm" inside the window, none prior, clears
    # rising_min_count from a standing start: exercises the strip's populated
    # state end to end against a committed golden.
    _, out = build_canned(tmp_path, rising_items=3)
    index_html = (out / "index.html").read_text(encoding="utf-8")
    assert index_html == (GOLDEN / "index_rising.html").read_text(encoding="utf-8")
    assert "Rising this week" in index_html
    assert 'class="rising-chip"' in index_html
    assert "/grepify/keyword/riseterm-" in index_html


def test_items_matches_golden(tmp_path: Path) -> None:
    _, out = build_canned(tmp_path)
    assert (out / "items" / "index.html").read_text(encoding="utf-8") == (
        GOLDEN / "items_index.html"
    ).read_text(encoding="utf-8")


def test_sources_matches_golden(tmp_path: Path) -> None:
    _, out = build_canned(tmp_path)
    assert (out / "sources" / "index.html").read_text(encoding="utf-8") == (
        GOLDEN / "sources_index.html"
    ).read_text(encoding="utf-8")


def test_health_matches_golden(tmp_path: Path) -> None:
    _, out = build_canned(tmp_path)
    assert (out / "health" / "index.html").read_text(encoding="utf-8") == (
        GOLDEN / "health_index.html"
    ).read_text(encoding="utf-8")


def test_page_json_matches_golden(tmp_path: Path) -> None:
    _, out = build_canned(tmp_path)
    assert (out / "items" / "page-1.json").read_text(encoding="utf-8") == (
        GOLDEN / "page-1.json"
    ).read_text(encoding="utf-8")


# --- cache-busting (stale-deploy defence) ------------------------------------


def test_static_refs_are_content_hashed(tmp_path: Path) -> None:
    # Every referenced static asset must carry ?v=<short sha256 of its bytes>,
    # which forces GitHub Pages onto a fresh URL after a deploy so a fixed asset
    # reaches readers instead of a stale cached copy.
    _, out = build_canned(tmp_path)
    index_html = (out / "index.html").read_text(encoding="utf-8")
    items_html = (out / "items" / "index.html").read_text(encoding="utf-8")
    both = index_html + items_html

    refs = re.findall(r"/grepify/static/([\w.]+)\?v=([0-9a-f]+)", both)
    seen = {name: ver for name, ver in refs}
    # the chrome (style.css, theme.js) plus a page-specific script (filters.js)
    assert {"style.css", "theme.js", "filters.js"} <= set(seen)

    for name, ver in seen.items():
        data = (out / "static" / name).read_bytes()
        expected = hashlib.sha256(data).hexdigest()[:8]
        assert ver == expected, f"{name} ?v= must match its content hash"


def test_no_unversioned_static_refs(tmp_path: Path) -> None:
    # No page may reference a static asset without the cache-buster; an
    # unversioned URL is the stale-cache hole this closes.
    _, out = build_canned(tmp_path)
    for page in out.rglob("*.html"):
        html = page.read_text(encoding="utf-8")
        bare = re.findall(r'/grepify/static/[\w.]+(?<!\.json)(?=["\'])', html)
        assert not bare, f"{page} has unversioned static refs: {bare}"


def test_asset_version_is_deterministic_and_content_derived(tmp_path: Path) -> None:
    # Same content -> same URL (byte-stable goldens); this pins the ?v= scheme.
    first_html = (build_canned(tmp_path / "a")[1] / "digest" / "index.html").read_text(
        encoding="utf-8"
    )
    second_html = (build_canned(tmp_path / "b")[1] / "digest" / "index.html").read_text(
        encoding="utf-8"
    )
    assert first_html == second_html


# --- determinism -------------------------------------------------------------


def test_build_is_deterministic_twice_in_a_row(tmp_path: Path) -> None:
    _, first = build_canned(tmp_path / "a")
    _, second = build_canned(tmp_path / "b")
    first_files = {p.relative_to(first): p.read_bytes() for p in first.rglob("*") if p.is_file()}
    second_files = {p.relative_to(second): p.read_bytes() for p in second.rglob("*") if p.is_file()}
    assert first_files == second_files


# --- emission window + collapse ----------------------------------------------


def test_trailing_90d_emission(tmp_path: Path) -> None:
    result, out = build_canned(tmp_path)
    # 'old' (>90d) is in truth (items_total=4) but not emitted (3 recent items)
    assert result.items_total == 4
    assert result.items_emitted == 3
    items_html = (out / "items" / "index.html").read_text(encoding="utf-8")
    assert "Ancient news" not in items_html


def test_near_dup_collapsed_in_output(tmp_path: Path) -> None:
    _, out = build_canned(tmp_path)
    items_html = (out / "items" / "index.html").read_text(encoding="utf-8")
    assert "1 similar" in items_html  # i1 collapsed under i2


def test_data_keywords_attribute_is_valid_json(tmp_path: Path) -> None:
    # filters.js contract: data-keywords is a JSON array of whole phrases, so
    # multi-word keywords survive the round-trip.
    _, out = build_canned(tmp_path)
    items_html = (out / "items" / "index.html").read_text(encoding="utf-8")
    attrs = re.findall(r"data-keywords='([^']*)'", items_html)
    assert attrs, "no data-keywords attributes rendered"
    all_tags = []
    for raw in attrs:
        parsed = json.loads(raw)
        assert isinstance(parsed, list)
        all_tags.extend(parsed)
    # the multi-word phrase survives as one element, not split on the space
    assert "agentic coding" in all_tags


def test_pagination_emits_multiple_pages(tmp_path: Path) -> None:
    # 3 base recent groups + 25 filler → 28 groups → 2 pages of 20
    result, out = build_canned(tmp_path, extra_recent_items=25)
    assert (out / "items" / "index.html").exists()
    assert (out / "items" / "page-2" / "index.html").exists()
    assert (out / "items" / "page-2.json").exists()
    # home + digest index + 2 item pages + sources + health
    # (no digests/keyword pages here)
    assert result.pages_written == 6


# --- static assets + resilience ----------------------------------------------


def test_static_assets_written(tmp_path: Path) -> None:
    _, out = build_canned(tmp_path)
    assert (out / "static" / "style.css").is_file()
    assert (out / "static" / "filters.js").is_file()
    assert (out / "static" / "theme.js").is_file()  # referenced by every page head


def test_missing_health_renders_empty_page(tmp_path: Path) -> None:
    _, out = build_canned(tmp_path, with_health=False)
    health_html = (out / "health" / "index.html").read_text(encoding="utf-8")
    assert "No health snapshot available" in health_html


def test_rebuild_clears_stale_output(tmp_path: Path) -> None:
    _, out = build_canned(tmp_path)
    stale = out / "stale.html"
    stale.write_text("stale", encoding="utf-8")
    build_canned(tmp_path)  # rebuild into the same tmp_path/public
    assert not stale.exists()
