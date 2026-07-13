"""Digests page tests (GRP-38/47/50/69): server-rendered All-view baseline.

Builds a canned site with digests across two categories (``ai`` with a daily and
a later weekly, plus ``data-eng``) and snapshots the server-rendered
``digest/index.html`` against a golden. The client-side All/Following tab,
topic-follow filter, and since-your-last-visit delta (``digests.js``,
localStorage + ``?view=``/``?topics=``) are NOT exercised here - they only hide
rows / switch views / mark rows in the browser, exactly as the daily/weekly kind
filter is left to the client. The tested surface is the progressive-enhancement
baseline: every digest, newest-first by period, which is the full ``All``
archive the page degrades to with JS off. Per GRP-50 the filter controls (kind
form, topic chips, Share) ship server-rendered ``hidden`` - they belong to the
Following view and digests.js reveals them there - so the baseline carries no
visible controls. Per GRP-69 the same holds for the "N new digests since"
summary line, plus each row now carries ``data-created-at`` so digests.js can
compute the delta client-side without a server round trip. A determinism check
(build twice -> identical bytes) guards the S8 rule for the page.
"""

from __future__ import annotations

import json
import textwrap
from datetime import UTC, datetime
from pathlib import Path

from grepify.clock import FixedClock
from grepify.config.filesystem import FilesystemConfigProvider
from grepify.models import Digest, DigestKind
from grepify.paths import DataLayout
from grepify.repository.jsonl_sqlite import JsonlSqliteRepository
from grepify.site.build import build_site

GOLDEN = Path(__file__).parent / "fixtures" / "site" / "pages" / "digest_index.html"
_CLOCK = FixedClock(datetime(2026, 7, 8, tzinfo=UTC))
_RUN_ID = "20260709T120000Z-testrun"

_SETTINGS = textwrap.dedent(
    """
    llm:
      active_profile: p
      profiles:
        p: {endpoint: openai-compat, model: m, max_calls_per_run: 40}
    """
).strip()

_GROUP_AI = textwrap.dedent(
    """
    group: ai-research
    name: AI Research
    category: ai
    sources:
      - {id: s1, kind: rss, name: Source One, url: 'https://ex.com/one/feed'}
    """
).strip()

_GROUP_DATA = textwrap.dedent(
    """
    group: data-eng
    name: Data Engineering
    category: data-eng
    sources:
      - {id: s2, kind: rss, name: Source Two, url: 'https://ex.com/two/feed'}
    """
).strip()


def _digest(  # noqa: PLR0913 - test fixture builder, each field pins a column
    digest_id: str,
    *,
    kind: DigestKind,
    category: str,
    title: str,
    period_start: str,
    created_at: str,
    top_keywords: str = "[]",
) -> Digest:
    return Digest(
        digest_id=digest_id,
        kind=kind,
        category=category,
        period_start=period_start,
        period_end="2026-07-08T06:00:00+00:00",
        title=title,
        body_md="body",
        top_keywords=top_keywords,
        model="digest-model",
        prompt_version="digest-v1",
        created_at=created_at,
    )


def _build(tmp_path: Path) -> Path:
    conf = tmp_path / "sources"
    (conf / "groups").mkdir(parents=True, exist_ok=True)
    (conf / "settings.yml").write_text(_SETTINGS, encoding="utf-8")
    (conf / "keywords.yml").write_text("aliases: {}\nmute: []\n", encoding="utf-8")
    (conf / "groups" / "ai-research.yml").write_text(_GROUP_AI, encoding="utf-8")
    (conf / "groups" / "data-eng.yml").write_text(_GROUP_DATA, encoding="utf-8")

    data = tmp_path / "data"
    repo = JsonlSqliteRepository(data)
    repo.add_digest(
        _digest(
            "daily-ai-2026-07-07",
            kind=DigestKind.DAILY,
            category="ai",
            title="AI moved fast today",
            period_start="2026-07-07T06:00:00+00:00",
            created_at="2026-07-08T13:05:00+00:00",
            top_keywords=json.dumps([{"keyword": "genai", "count": 3}]),
        )
    )
    repo.add_digest(
        _digest(
            "weekly-ai-2026-W27",
            kind=DigestKind.WEEKLY,
            category="ai",
            title="AI weekly roundup",
            period_start="2026-06-29T06:00:00+00:00",
            created_at="2026-07-08T13:10:00+00:00",
        )
    )
    repo.add_digest(
        _digest(
            "daily-data-eng-2026-07-07",
            kind=DigestKind.DAILY,
            category="data-eng",
            title="Data pipelines roundup",
            period_start="2026-07-07T06:00:00+00:00",
            created_at="2026-07-08T13:00:00+00:00",
        )
    )
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


def test_digest_index_matches_golden(tmp_path: Path) -> None:
    out = _build(tmp_path)
    html = (out / "digest" / "index.html").read_text(encoding="utf-8")
    assert html == GOLDEN.read_text(encoding="utf-8")


def test_digest_index_is_all_topics_newest_first(tmp_path: Path) -> None:
    out = _build(tmp_path)
    html = (out / "digest" / "index.html").read_text(encoding="utf-8")
    # every category is present server-side (All baseline shows all, unfiltered)
    assert 'data-category="ai"' in html
    assert 'data-category="data-eng"' in html
    # newest period first: the 2026-07-07 dailies precede the 2026-06-29 weekly
    assert html.index("AI moved fast today") < html.index("AI weekly roundup")
    assert html.index("Data pipelines roundup") < html.index("AI weekly roundup")
    # progressive-enhancement hooks are present (All/Following tab + chips + share)
    assert 'id="digest-views"' in html
    assert 'data-view="following"' in html
    assert 'id="topic-chips"' in html
    assert 'id="share-topics"' in html
    assert "static/digests.js" in html
    # The filter controls belong to the Following view - server-rendered
    # hidden (like the tablist) and revealed by digests.js only under Following,
    # so the All baseline is the fully unfiltered archive with no controls.
    assert '<div class="tabs" id="digest-views" role="tablist"' in html
    assert 'aria-label="Digest view" hidden>' in html
    assert '<form class="filters" id="digest-filters" aria-label="Filter digests" hidden>' in html
    assert '<div class="topic-follow" id="topic-follow" hidden>' in html


def test_digest_index_has_last_visit_delta_hooks(tmp_path: Path) -> None:
    # Each row carries data-created-at (when the digest was generated, not the
    # period it covers) so digests.js can mark rows newer than the reader's
    # stored last-visit timestamp. The "N new digests since" summary ships
    # hidden, like the other Following-only controls. The marking and summary
    # text are client-only, not exercised in this baseline (see module docstring).
    out = _build(tmp_path)
    html = (out / "digest" / "index.html").read_text(encoding="utf-8")
    assert 'data-created-at="2026-07-08T13:05:00+00:00"' in html  # daily-ai
    assert 'data-created-at="2026-07-08T13:00:00+00:00"' in html  # daily-data-eng
    assert 'data-created-at="2026-07-08T13:10:00+00:00"' in html  # weekly-ai
    assert '<p class="muted" id="digest-new-since" hidden></p>' in html


def test_digest_index_is_deterministic(tmp_path: Path) -> None:
    first = _build(tmp_path / "a")
    second = _build(tmp_path / "b")
    a = (first / "digest" / "index.html").read_bytes()
    b = (second / "digest" / "index.html").read_bytes()
    assert a == b
