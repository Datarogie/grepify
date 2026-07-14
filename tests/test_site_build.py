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

import pytest

from grepify.clock import FixedClock
from grepify.config.filesystem import FilesystemConfigProvider
from grepify.errors import ConfigError
from grepify.health import ErrorClass, HealthSnapshot, SourceHealth
from grepify.ingest import RawItem, normalize
from grepify.models import ExtractionMethod, FetchStatus, Item, ItemKeyword, SourceKind
from grepify.path_safety import ContainedPath, ensure_safe_output_dir
from grepify.paths import DataLayout
from grepify.repository.jsonl_sqlite import JsonlSqliteRepository
from grepify.site.build import BuildResult, _capture_destination_state, _replace_output, build_site

GOLDEN = Path(__file__).parent / "fixtures" / "site" / "pages"
_CLOCK = FixedClock(datetime(2026, 7, 8, 13, 0, tzinfo=UTC))  # 07:00 MDT
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


def test_normalized_ipv6_item_url_reaches_generated_html_and_json(tmp_path: Path) -> None:
    conf = tmp_path / "sources"
    (conf / "groups").mkdir(parents=True, exist_ok=True)
    (conf / "settings.yml").write_text(_SETTINGS, encoding="utf-8")
    (conf / "keywords.yml").write_text(_KEYWORDS, encoding="utf-8")
    (conf / "groups" / "ipv6.yml").write_text(
        textwrap.dedent(
            """
            group: ipv6
            name: IPv6 Sources
            category: ai
            sources:
              - {id: s-ipv6, kind: rss, name: IPv6 Source, url: 'https://feeds.example/rss.xml'}
            """
        ).strip(),
        encoding="utf-8",
    )

    source = FilesystemConfigProvider(conf).sources()[0]
    raw = RawItem(
        url="http://[2001:db8::1]:8080/post?q=1&utm_source=x#fragment",
        title="Valid IPv6 story",
    )
    item = normalize(raw, source, fetched_at="2026-07-08T11:00:00+00:00")
    assert item.canonical_url == "http://[2001:db8::1]:8080/post?q=1"

    data = tmp_path / "data"
    repo = JsonlSqliteRepository(data)
    repo.add_items([item])
    repo.close()

    out = build_site(
        config=FilesystemConfigProvider(conf),
        repository=JsonlSqliteRepository(data),
        layout=DataLayout(data),
        clock=_CLOCK,
        run_id=_RUN_ID,
        output_dir=tmp_path / "public",
        base_path="/grepify/",
    ).output_dir

    items_html = (out / "items/index.html").read_text(encoding="utf-8")
    assert '<a href="http://[2001:db8::1]:8080/post?q=1" rel="noopener noreferrer">' in items_html
    assert "Valid IPv6 story" in items_html

    payload = json.loads((out / "items/page-1.json").read_text(encoding="utf-8"))
    assert payload["items"][0]["url"] == "http://[2001:db8::1]:8080/post?q=1"


def test_generated_output_omits_unsafe_href_schemes_but_keeps_titles(tmp_path: Path) -> None:
    conf = tmp_path / "sources"
    (conf / "groups").mkdir(parents=True, exist_ok=True)
    (conf / "settings.yml").write_text(
        _SETTINGS.replace(
            "windows:\n  cloud_days: 7", "windows:\n  cloud_days: 7\n  keyword_min_mentions: 2"
        ),
        encoding="utf-8",
    )
    (conf / "keywords.yml").write_text(_KEYWORDS, encoding="utf-8")
    (conf / "groups" / "security.yml").write_text(
        textwrap.dedent(
            """
            group: security
            name: Security Sources
            category: ai
            sources:
              - {id: s-safe, kind: rss, name: Safe Source, url: 'https://ex.com/feed/main.xml'}
              - id: s-unsafe-feed
                kind: rss
                name: Unsafe Feed Source
                url: 'javascript:alert(99)'
              - id: s-relative-feed
                kind: rss
                name: Relative Feed Source
                url: '/relative/feed.xml'
            """
        ).strip(),
        encoding="utf-8",
    )

    data = tmp_path / "data"
    repo = JsonlSqliteRepository(data)
    common = {
        "source_id": "s-safe",
        "kind": SourceKind.RSS,
        "summary": None,
        "fetched_at": "2026-07-08T11:00:00+00:00",
    }
    repo.add_items(
        [
            Item(
                **common,
                item_id="rep-js",
                external_id="rep-js",
                canonical_url="javascript:alert(1)",
                title="Readable representative JS title",
                published_at="2026-07-07T10:05:00+00:00",
                content_hash="1111111111111111",
            ),
            Item(
                **common,
                item_id="keyword-data",
                external_id="keyword-data",
                canonical_url="data:text/html,<h1>x</h1>",
                title="Readable keyword data title",
                published_at="2026-07-07T10:04:00+00:00",
                content_hash="2222222222222222",
            ),
            Item(
                **common,
                item_id="relative-item",
                external_id="relative-item",
                canonical_url="../relative-post?x=1",
                title="Resolved relative title",
                published_at="2026-07-07T10:03:00+00:00",
                content_hash="3333333333333333",
            ),
            Item(
                **common,
                item_id="valid-https",
                external_id="valid-https",
                canonical_url="https://safe.example/post",
                title="Valid HTTPS title",
                published_at="2026-07-07T10:02:00+00:00",
                content_hash="4444444444444444",
            ),
            Item(
                **common,
                item_id="valid-dup-rep",
                external_id="valid-dup-rep",
                canonical_url="http://safe.example/duplicate",
                title="Valid duplicate representative title",
                published_at="2026-07-07T10:01:00+00:00",
                content_hash="aaaaaaaaaaaaaaaa",
            ),
            Item(
                **common,
                item_id="dup-js",
                external_id="dup-js",
                canonical_url="javascript:alert(2)",
                title="Readable duplicate JS title",
                published_at="2026-07-07T10:00:00+00:00",
                content_hash="aaaaaaaaaaaaaaab",
            ),
        ]
    )
    repo.add_item_keywords([_kw("keyword-data", "genai"), _kw("relative-item", "genai")])
    repo.close()

    result = build_site(
        config=FilesystemConfigProvider(conf),
        repository=JsonlSqliteRepository(data),
        layout=DataLayout(data),
        clock=_CLOCK,
        run_id=_RUN_ID,
        output_dir=tmp_path / "public",
        base_path="/grepify/",
    )
    out = result.output_dir
    keyword_page = next((out / "keyword").glob("genai-*/index.html"))

    generated = {
        "home": (out / "index.html").read_text(encoding="utf-8"),
        "items": (out / "items/index.html").read_text(encoding="utf-8"),
        "keyword": keyword_page.read_text(encoding="utf-8"),
        "sources": (out / "sources/index.html").read_text(encoding="utf-8"),
    }
    combined = "\n".join(generated.values())

    for html in generated.values():
        assert 'href="javascript:' not in html.lower()
        assert 'href="data:' not in html.lower()
    assert "Readable representative JS title" in generated["items"]
    assert "Readable duplicate JS title" in generated["items"]
    assert "Readable keyword data title" in generated["keyword"]
    assert "feed" in generated["sources"]
    assert "Valid HTTPS title" in combined
    assert 'href="https://safe.example/post"' in combined
    assert 'href="http://safe.example/duplicate"' in generated["items"]
    assert 'href="https://ex.com/relative-post?x=1"' in combined
    assert 'href="javascript:alert(99)"' not in generated["sources"].lower()
    assert 'href="/relative/feed.xml"' not in generated["sources"]
    assert (
        'href="https://ex.com/feed/main.xml" rel="noopener noreferrer">feed</a>'
        in generated["sources"]
    )

    payload = json.loads((out / "items/page-1.json").read_text(encoding="utf-8"))
    urls = {item["item_id"]: item["url"] for item in payload["items"]}
    assert urls["rep-js"] is None
    assert urls["keyword-data"] is None
    assert urls["relative-item"] == "https://ex.com/relative-post?x=1"
    assert urls["valid-https"] == "https://safe.example/post"

    rebuilt = build_site(
        config=FilesystemConfigProvider(conf),
        repository=JsonlSqliteRepository(data),
        layout=DataLayout(data),
        clock=_CLOCK,
        run_id=_RUN_ID,
        output_dir=tmp_path / "public-again",
        base_path="/grepify/",
    ).output_dir
    rebuilt_files = {
        p.relative_to(rebuilt): p.read_bytes() for p in rebuilt.rglob("*") if p.is_file()
    }
    first_files = {p.relative_to(out): p.read_bytes() for p in out.rglob("*") if p.is_file()}
    assert rebuilt_files == first_files


def test_generated_pages_include_meta_csp_and_required_assets_are_allowed(tmp_path: Path) -> None:
    _, out = build_canned(tmp_path)
    html = (out / "index.html").read_text(encoding="utf-8")
    csp_pos = html.index('http-equiv="Content-Security-Policy"')
    assert csp_pos < html.index('rel="stylesheet"') < html.index("<script src=")
    csp = re.search(r'<meta http-equiv="Content-Security-Policy" content="([^"]+)">', html)
    assert csp is not None
    policy = csp.group(1)
    directives = [
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "font-src 'self' data:",
        "img-src 'self' data:",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'none'",
    ]
    for directive in directives:
        assert directive in policy
    assert "'unsafe-inline'" not in policy
    assert (out / "static/theme.js").is_file()
    assert (out / "static/style.css").is_file()


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


def test_build_rejects_unsafe_output_directories(tmp_path: Path) -> None:
    build_canned(tmp_path)
    data = tmp_path / "data"
    conf = tmp_path / "sources"
    non_directory = tmp_path / "not-a-directory"
    non_directory.write_text("not a directory", encoding="utf-8")
    unsafe_paths = [Path("/"), tmp_path, data, conf, tmp_path / ".." / "outside", non_directory]

    for unsafe in unsafe_paths:
        with pytest.raises(ValueError, match="unsafe output directory"):
            build_site(
                config=FilesystemConfigProvider(conf),
                repository=JsonlSqliteRepository(data),
                layout=DataLayout(data),
                clock=_CLOCK,
                run_id=_RUN_ID,
                output_dir=unsafe,
                protected_roots=(conf,),
            )


def test_build_rejects_output_symlink_escape(tmp_path: Path) -> None:
    build_canned(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "public-link"
    link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="unsafe output directory"):
        build_site(
            config=FilesystemConfigProvider(tmp_path / "sources"),
            repository=JsonlSqliteRepository(tmp_path / "data"),
            layout=DataLayout(tmp_path / "data"),
            clock=_CLOCK,
            run_id=_RUN_ID,
            output_dir=link,
            protected_roots=(tmp_path / "sources",),
        )
    assert outside.exists()


def test_failed_build_preserves_previous_output(tmp_path: Path) -> None:
    _, out = build_canned(tmp_path)
    previous = (out / "index.html").read_text(encoding="utf-8")
    (tmp_path / "sources" / "keywords.yml").write_text("aliases: []\n", encoding="utf-8")

    with pytest.raises(ConfigError):
        build_site(
            config=FilesystemConfigProvider(tmp_path / "sources"),
            repository=JsonlSqliteRepository(tmp_path / "data"),
            layout=DataLayout(tmp_path / "data"),
            clock=_CLOCK,
            run_id=_RUN_ID,
            output_dir=out,
            protected_roots=(tmp_path / "sources",),
        )

    assert (out / "index.html").read_text(encoding="utf-8") == previous


def test_generated_path_containment_rejects_traversal(tmp_path: Path) -> None:
    root = ContainedPath.create(tmp_path / "public")
    with pytest.raises(ValueError, match="escapes output root"):
        root.resolve(Path("../outside.html"))
    with pytest.raises(ValueError, match="relative"):
        root.resolve(Path("/outside.html"))


def test_output_parent_of_cwd_is_rejected_with_external_roots(tmp_path: Path) -> None:
    cwd = tmp_path / "repo" / "work"
    cwd.mkdir(parents=True)
    external_data = tmp_path / "external-data"
    external_config = tmp_path / "external-config"

    with pytest.raises(ValueError, match="unsafe output directory"):
        ensure_safe_output_dir(
            cwd.parent, cwd=cwd, protected_roots=(external_data, external_config)
        )


def test_default_public_output_under_cwd_is_safe() -> None:
    repo_root = Path.cwd()
    safe = ensure_safe_output_dir(
        Path("public"),
        cwd=repo_root,
        protected_roots=(repo_root / "data", repo_root / "sources"),
    )
    assert safe == repo_root / "public"


def test_first_replacement_failure_leaves_previous_output(tmp_path: Path) -> None:
    output = tmp_path / "public"
    output.mkdir()
    (output / "index.html").write_text("previous", encoding="utf-8")
    stage = tmp_path / ".public.stage"
    stage.mkdir()
    expected = _capture_destination_state(output)

    def fail_first_replace(_src: Path, _dst: Path) -> None:
        raise OSError("backup failed")

    with pytest.raises(OSError, match="backup failed"):
        _replace_output(stage, output, expected_output_state=expected, replace=fail_first_replace)

    assert (output / "index.html").read_text(encoding="utf-8") == "previous"
    assert not stage.exists()


def test_publish_failure_restores_previous_output_when_destination_absent(tmp_path: Path) -> None:
    output = tmp_path / "public"
    output.mkdir()
    (output / "index.html").write_text("previous", encoding="utf-8")
    stage = tmp_path / ".public.stage"
    stage.mkdir()
    expected = _capture_destination_state(output)
    calls = 0

    def replace_with_publish_failure(src: Path, dst: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("publish failed")
        src.replace(dst)

    with pytest.raises(OSError, match="publish failed"):
        _replace_output(
            stage, output, expected_output_state=expected, replace=replace_with_publish_failure
        )

    assert (output / "index.html").read_text(encoding="utf-8") == "previous"
    assert not stage.exists()


def test_publish_failure_preserves_backup_when_destination_is_occupied(tmp_path: Path) -> None:
    output = tmp_path / "public"
    output.mkdir()
    (output / "index.html").write_text("previous", encoding="utf-8")
    stage = tmp_path / ".public.stage"
    stage.mkdir()
    expected = _capture_destination_state(output)
    calls = 0

    def replace_with_publish_conflict(src: Path, dst: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            output.mkdir()
            (output / "index.html").write_text("interloper", encoding="utf-8")
            raise OSError("publish failed")
        src.replace(dst)

    with pytest.raises(RuntimeError, match="destination is occupied"):
        _replace_output(
            stage, output, expected_output_state=expected, replace=replace_with_publish_conflict
        )

    backups = list(tmp_path.glob(".public.*.previous"))
    assert (output / "index.html").read_text(encoding="utf-8") == "interloper"
    assert len(backups) == 1
    assert (backups[0] / "index.html").read_text(encoding="utf-8") == "previous"
    assert stage.exists()


def test_staged_render_failure_preserves_output_and_removes_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, out = build_canned(tmp_path)
    previous = (out / "index.html").read_text(encoding="utf-8")

    def fail_write(*_args: object, **_kwargs: object) -> None:
        raise OSError("render write failed")

    monkeypatch.setattr("grepify.site.build._write", fail_write)

    with pytest.raises(OSError, match="render write failed"):
        build_site(
            config=FilesystemConfigProvider(tmp_path / "sources"),
            repository=JsonlSqliteRepository(tmp_path / "data"),
            layout=DataLayout(tmp_path / "data"),
            clock=_CLOCK,
            run_id=_RUN_ID,
            output_dir=out,
            protected_roots=(tmp_path / "sources",),
        )

    assert (out / "index.html").read_text(encoding="utf-8") == previous
    assert not list(tmp_path.glob(".public.*.tmp"))


def test_keyboard_interrupt_during_first_replace_preserves_output(tmp_path: Path) -> None:
    output = tmp_path / "public"
    output.mkdir()
    (output / "index.html").write_text("previous", encoding="utf-8")
    stage = tmp_path / ".public.stage"
    stage.mkdir()
    expected = _capture_destination_state(output)

    def interrupt_first_replace(_src: Path, _dst: Path) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _replace_output(
            stage, output, expected_output_state=expected, replace=interrupt_first_replace
        )

    assert (output / "index.html").read_text(encoding="utf-8") == "previous"
    assert not stage.exists()


def test_publish_preserves_broken_symlink_occupant(tmp_path: Path) -> None:
    output = tmp_path / "public"
    output.mkdir()
    (output / "index.html").write_text("previous", encoding="utf-8")
    stage = tmp_path / ".public.stage"
    stage.mkdir()
    expected = _capture_destination_state(output)
    missing_target = tmp_path / "missing-target"

    def occupy_with_broken_symlink(src: Path, dst: Path) -> None:
        src.replace(dst)
        output.symlink_to(missing_target, target_is_directory=True)

    with pytest.raises(RuntimeError, match="destination is occupied"):
        _replace_output(
            stage, output, expected_output_state=expected, replace=occupy_with_broken_symlink
        )

    backups = list(tmp_path.glob(".public.*.previous"))
    assert output.is_symlink()
    assert not output.exists()
    assert output.readlink() == missing_target
    assert len(backups) == 1
    assert (backups[0] / "index.html").read_text(encoding="utf-8") == "previous"
    assert stage.exists()


def test_absent_output_created_before_publish_is_preserved(tmp_path: Path) -> None:
    output = tmp_path / "public"
    expected = _capture_destination_state(output)
    stage = tmp_path / ".public.stage"
    stage.mkdir()
    (stage / "index.html").write_text("stage", encoding="utf-8")
    output.mkdir()
    (output / "index.html").write_text("new occupant", encoding="utf-8")

    with pytest.raises(RuntimeError, match="changed during build"):
        _replace_output(stage, output, expected_output_state=expected)

    assert (output / "index.html").read_text(encoding="utf-8") == "new occupant"
    assert not stage.exists()


def test_existing_output_replaced_before_publish_is_preserved(tmp_path: Path) -> None:
    output = tmp_path / "public"
    output.mkdir()
    (output / "index.html").write_text("previous", encoding="utf-8")
    expected = _capture_destination_state(output)
    stage = tmp_path / ".public.stage"
    stage.mkdir()
    (stage / "index.html").write_text("stage", encoding="utf-8")
    moved_previous = tmp_path / "moved-previous"
    output.replace(moved_previous)
    output.mkdir()
    (output / "index.html").write_text("replacement", encoding="utf-8")

    with pytest.raises(RuntimeError, match="changed during build"):
        _replace_output(stage, output, expected_output_state=expected)

    assert (output / "index.html").read_text(encoding="utf-8") == "replacement"
    assert (moved_previous / "index.html").read_text(encoding="utf-8") == "previous"
    assert not stage.exists()


def test_stale_concurrent_publish_refuses_to_overwrite_newer_result(tmp_path: Path) -> None:
    output = tmp_path / "public"
    stale_expected = _capture_destination_state(output)
    newer_expected = _capture_destination_state(output)
    stale_stage = tmp_path / ".public.stale"
    stale_stage.mkdir()
    (stale_stage / "index.html").write_text("stale", encoding="utf-8")
    newer_stage = tmp_path / ".public.newer"
    newer_stage.mkdir()
    (newer_stage / "index.html").write_text("newer", encoding="utf-8")

    _replace_output(newer_stage, output, expected_output_state=newer_expected)
    with pytest.raises(RuntimeError, match="changed during build"):
        _replace_output(stale_stage, output, expected_output_state=stale_expected)

    assert (output / "index.html").read_text(encoding="utf-8") == "newer"
    assert not stale_stage.exists()


def test_keyboard_interrupt_during_second_replace_restores_previous_output(tmp_path: Path) -> None:
    output = tmp_path / "public"
    output.mkdir()
    (output / "index.html").write_text("previous", encoding="utf-8")
    expected = _capture_destination_state(output)
    stage = tmp_path / ".public.stage"
    stage.mkdir()
    calls = 0

    def interrupt_second_replace(src: Path, dst: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        src.replace(dst)

    with pytest.raises(KeyboardInterrupt):
        _replace_output(
            stage, output, expected_output_state=expected, replace=interrupt_second_replace
        )

    assert (output / "index.html").read_text(encoding="utf-8") == "previous"
    assert not stage.exists()


def test_backup_identity_mismatch_preserves_interloper_and_stage_not_published(
    tmp_path: Path,
) -> None:
    output = tmp_path / "public"
    output.mkdir()
    (output / "index.html").write_text("previous", encoding="utf-8")
    expected = _capture_destination_state(output)
    stage = tmp_path / ".public.stage"
    stage.mkdir()
    (stage / "index.html").write_text("stage", encoding="utf-8")
    interloper = tmp_path / "interloper"
    interloper.mkdir()
    (interloper / "index.html").write_text("interloper", encoding="utf-8")
    original_saved = tmp_path / "original-saved"

    def swap_before_backup_rename(src: Path, dst: Path) -> None:
        if src == output:
            src.replace(original_saved)
            interloper.replace(src)
        src.replace(dst)

    with pytest.raises(RuntimeError, match="backup identity mismatch"):
        _replace_output(
            stage, output, expected_output_state=expected, replace=swap_before_backup_rename
        )

    assert (output / "index.html").read_text(encoding="utf-8") == "interloper"
    assert (original_saved / "index.html").read_text(encoding="utf-8") == "previous"
    assert not stage.exists()


def test_absent_output_created_during_claim_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "public"
    expected = _capture_destination_state(output)
    stage = tmp_path / ".public.stage"
    stage.mkdir()
    (stage / "index.html").write_text("stage", encoding="utf-8")
    original_mkdir = Path.mkdir

    def occupy_before_claim(self: Path, *args: object, **kwargs: object) -> None:
        if self == output:
            original_mkdir(self)
            (self / "index.html").write_text("occupant", encoding="utf-8")
            raise FileExistsError(str(self))
        original_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", occupy_before_claim)

    with pytest.raises(RuntimeError, match="changed during build"):
        _replace_output(stage, output, expected_output_state=expected)

    assert (output / "index.html").read_text(encoding="utf-8") == "occupant"
    assert not stage.exists()
