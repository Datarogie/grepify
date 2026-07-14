"""Digest + keyword page rendering tests (GRP-43/44): end-to-end build output.

Builds a canned site with a stored digest and a keyword above the detail-page
threshold, then asserts the digest index/detail and keyword pages render, the
home cloud repoints to the real keyword page, and the build stays deterministic.
"""

from __future__ import annotations

import json
import textwrap
from datetime import UTC, datetime
from pathlib import Path

from grepify.clock import FixedClock
from grepify.config.filesystem import FilesystemConfigProvider
from grepify.models import Digest, DigestKind, ExtractionMethod, Item, ItemKeyword, SourceKind
from grepify.paths import DataLayout
from grepify.repository.jsonl_sqlite import JsonlSqliteRepository
from grepify.site.build import build_site
from grepify.site.urls import digest_slug, keyword_slug

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
      keyword_days: 30
      keyword_min_mentions: 3
    """
).strip()

_GROUP = textwrap.dedent(
    """
    group: ai-research
    name: AI Research
    category: ai
    sources:
      - {id: s1, kind: rss, name: Source One, url: 'https://ex.com/one/feed'}
    """
).strip()


def _item(item_id: str, published_at: str, content_hash: str) -> Item:
    return Item(
        item_id=item_id,
        source_id="s1",
        kind=SourceKind.RSS,
        external_id=item_id,
        canonical_url=f"https://ex.com/{item_id}",
        title=f"story {item_id}",
        summary="a summary",
        published_at=published_at,
        fetched_at="2026-07-08T00:00:00+00:00",
        content_hash=content_hash,
    )


def _kw(item_id: str) -> ItemKeyword:
    return ItemKeyword(
        item_id=item_id,
        keyword="genai",
        rank=1,
        method=ExtractionMethod.LLM,
        model="m",
        extracted_at="2026-07-08T00:00:00+00:00",
    )


def _digest() -> Digest:
    return Digest(
        digest_id="daily-ai-2026-07-07",
        kind=DigestKind.DAILY,
        category="ai",
        period_start="2026-07-07T06:00:00+00:00",
        period_end="2026-07-08T06:00:00+00:00",
        title="AI moved fast today",
        body_md="**TL;DR**\n\n- genai surged\n\nA short narrative about genai.",
        top_keywords=json.dumps([{"keyword": "genai", "count": 3}]),
        model="digest-model",
        prompt_version="digest-v1",
        created_at="2026-07-08T13:00:00+00:00",
    )


def _build(tmp_path: Path) -> Path:
    conf = tmp_path / "sources"
    (conf / "groups").mkdir(parents=True, exist_ok=True)
    (conf / "settings.yml").write_text(_SETTINGS, encoding="utf-8")
    (conf / "keywords.yml").write_text("aliases: {}\nmute: []\n", encoding="utf-8")
    (conf / "groups" / "ai-research.yml").write_text(_GROUP, encoding="utf-8")

    data = tmp_path / "data"
    repo = JsonlSqliteRepository(data)
    repo.add_items(
        [
            _item("i1", "2026-07-05T10:00:00+00:00", "0000000000000001"),
            _item("i2", "2026-07-06T10:00:00+00:00", "00000000000000ff"),
            _item("i3", "2026-07-07T10:00:00+00:00", "ffffffffffffffff"),
        ]
    )
    repo.add_item_keywords([_kw("i1"), _kw("i2"), _kw("i3")])  # genai x3 -> keyword page
    repo.add_digest(_digest())
    repo.close()

    build_site(
        config=FilesystemConfigProvider(conf),
        repository=JsonlSqliteRepository(data),
        layout=DataLayout(data),
        clock=_CLOCK,
        run_id=_RUN_ID,
        output_dir=tmp_path / "public",
        base_path="/grepify/",
    )
    return tmp_path / "public"


def test_digest_index_lists_the_digest(tmp_path: Path) -> None:
    out = _build(tmp_path)
    index = (out / "digest" / "index.html").read_text(encoding="utf-8")
    slug = digest_slug("daily-ai-2026-07-07", "daily")
    assert "AI moved fast today" in index
    assert f"/grepify/digest/daily/{slug}/" in index
    assert 'data-kind="daily"' in index  # kind filter hook
    assert 'data-category="ai"' in index  # topic filter hook


def test_digest_detail_renders_body_and_chips(tmp_path: Path) -> None:
    out = _build(tmp_path)
    slug = digest_slug("daily-ai-2026-07-07", "daily")
    detail = (out / "digest" / "daily" / slug / "index.html").read_text(encoding="utf-8")
    assert "AI moved fast today" in detail
    # markdown body rendered (TL;DR bold + bullet + narrative paragraph)
    assert "<strong>TL;DR</strong>" in detail
    assert "<li>genai surged</li>" in detail
    assert "A short narrative about genai." in detail
    # chip links to the keyword page
    assert f"/grepify/keyword/{keyword_slug('genai')}/" in detail


def test_keyword_page_has_sparkline_and_tabs(tmp_path: Path) -> None:
    out = _build(tmp_path)
    page = (out / "keyword" / keyword_slug("genai") / "index.html").read_text(encoding="utf-8")
    assert "#genai" in page
    assert "<svg" in page and "sparkline" in page  # timeline sparkline
    assert 'role="tablist"' in page  # tabbed latest content
    assert "story i3" in page  # a latest item


def test_home_cloud_links_to_keyword_page_when_above_threshold(tmp_path: Path) -> None:
    out = _build(tmp_path)
    home = (out / "index.html").read_text(encoding="utf-8")
    # genai has a page (3 mentions in 30d), so the cloud links to it, not the items filter
    assert f"/grepify/keyword/{keyword_slug('genai')}/" in home
    # and the latest-digests list shows the stored digest
    assert "AI moved fast today" in home


def test_digest_and_keyword_pages_are_deterministic(tmp_path: Path) -> None:
    first = _build(tmp_path / "a")
    second = _build(tmp_path / "b")
    a = {p.relative_to(first): p.read_bytes() for p in first.rglob("*") if p.is_file()}
    b = {p.relative_to(second): p.read_bytes() for p in second.rglob("*") if p.is_file()}
    assert a == b
