"""Health-page next-digest-run + last-digest-per-category tests (T4).

Builds a canned site with stored digests across two categories (one category
with a daily *and* a later weekly digest, to pin "latest wins regardless of
kind") and snapshots the rendered health page against a golden fixture, plus
asserts the specific next-run/per-category content the AC requires. The
DST-edge behavior of the underlying rollover is unit-tested directly in
``tests/test_site_next_digest.py``; this file only covers the render.
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
from grepify.site.urls import digest_slug

GOLDEN = Path(__file__).parent / "fixtures" / "site" / "pages" / "health_index_with_digests.html"

# Same instant as the other site-build tests: 2026-07-08T00:00:00Z ==
# 2026-07-07 18:00 MDT, a Wednesday - past today's 05:00 opening, so the next
# scheduled run is 2026-07-08 05:00 MDT (not a Monday, so daily-only).
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

_GROUP_POLICY = textwrap.dedent(
    """
    group: policy-watch
    name: Policy Watch
    category: policy
    sources:
      - {id: s2, kind: rss, name: Source Two, url: 'https://ex.com/two/feed'}
    """
).strip()


def _digest(
    digest_id: str,
    *,
    kind: DigestKind,
    category: str,
    title: str,
    created_at: str,
) -> Digest:
    return Digest(
        digest_id=digest_id,
        kind=kind,
        category=category,
        period_start="2026-07-06T06:00:00+00:00",
        period_end="2026-07-07T06:00:00+00:00",
        title=title,
        body_md="body",
        top_keywords=json.dumps([]),
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
    (conf / "groups" / "policy-watch.yml").write_text(_GROUP_POLICY, encoding="utf-8")

    data = tmp_path / "data"
    repo = JsonlSqliteRepository(data)
    repo.add_digest(
        _digest(
            "daily-ai-2026-07-07",
            kind=DigestKind.DAILY,
            category="ai",
            title="AI moved fast today",
            created_at="2026-07-07T13:05:00+00:00",
        )
    )
    repo.add_digest(
        _digest(
            "weekly-ai-2026-W27",
            kind=DigestKind.WEEKLY,
            category="ai",
            title="AI weekly roundup",
            created_at="2026-07-07T13:10:00+00:00",  # newer than the daily above
        )
    )
    repo.add_digest(
        _digest(
            "daily-policy-2026-07-06",
            kind=DigestKind.DAILY,
            category="policy",
            title="Policy digest note",
            created_at="2026-07-06T13:00:00+00:00",
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


def test_health_matches_golden_with_digests(tmp_path: Path) -> None:
    out = _build(tmp_path)
    html = (out / "health" / "index.html").read_text(encoding="utf-8")
    assert html == GOLDEN.read_text(encoding="utf-8")


def test_next_run_time_is_shown(tmp_path: Path) -> None:
    out = _build(tmp_path)
    html = (out / "health" / "index.html").read_text(encoding="utf-8")
    assert "2026-07-08 05:00" in html
    assert "America/Edmonton" in html
    assert "UTC-06:00" in html


def test_latest_digest_per_category_shown_and_deduped(tmp_path: Path) -> None:
    out = _build(tmp_path)
    html = (out / "health" / "index.html").read_text(encoding="utf-8")
    # the newer weekly digest wins for "ai", the older daily for it is not shown
    assert "AI weekly roundup" in html
    assert "AI moved fast today" not in html
    assert "Policy digest note" in html
    slug = digest_slug("weekly-ai-2026-W27", "weekly")
    assert f"/grepify/digest/weekly/{slug}/" in html
