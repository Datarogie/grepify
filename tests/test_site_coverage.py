"""Coverage-view snapshot tests (GRP-70): "sources you are no longer hearing from".

Builds a site with four live-or-not sources chosen to pin the exact
distinctions the issue calls for:

- ``s-fresh``: a recent item, healthy fetches - the baseline "everything's
  fine" row.
- ``s-quiet``: a stale item (long past the quiet threshold), but its fetches
  are healthy too - a *quiet* source is not an *erroring* one.
- ``s-erroring``: a recent item, but flagged fetch errors - the inverse case,
  an erroring source that is still contributing and so is not quiet.
- ``s-dead``: disabled (dead), no items ever - excluded from the quiet math
  entirely (its silence is already explained by its lifecycle class, GRP-66).

Snapshots the sources + health pages against committed goldens, plus asserts
the specific visual-distinction ACs directly (quiet vs erroring vs dead).
"""

from __future__ import annotations

import hashlib
import textwrap
from datetime import UTC, datetime
from pathlib import Path

from grepify.clock import FixedClock
from grepify.config.filesystem import FilesystemConfigProvider
from grepify.health import HealthSnapshot, SourceHealth
from grepify.models import FetchStatus, Item, SourceKind
from grepify.paths import DataLayout
from grepify.repository.jsonl_sqlite import JsonlSqliteRepository
from grepify.site.build import build_site

GOLDEN = Path(__file__).parent / "fixtures" / "site" / "pages"
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

_GROUP = textwrap.dedent(
    """
    group: g
    name: Coverage Group
    category: ai
    sources:
      - {id: s-fresh, kind: rss, name: Fresh One, url: 'https://ex.com/fresh/feed'}
      - {id: s-quiet, kind: rss, name: Quiet One, url: 'https://ex.com/quiet/feed'}
      - {id: s-erroring, kind: rss, name: Erroring One, url: 'https://ex.com/err/feed'}
      - id: s-dead
        kind: rss
        name: Dead One
        url: https://ex.com/dead/feed
        status: dead
        evidence: "#66: full ladder failed; recheck 30d"
    """
).strip()


def _item(item_id: str, *, source_id: str, published_at: str) -> Item:
    return Item(
        item_id=item_id,
        source_id=source_id,
        kind=SourceKind.RSS,
        external_id=item_id,
        canonical_url=f"https://ex.com/{item_id}",
        title=f"title {item_id}",
        summary="a summary",
        published_at=published_at,
        fetched_at="2026-07-08T11:00:00+00:00",
        content_hash=hashlib.blake2b(item_id.encode(), digest_size=8).hexdigest(),
    )


def _build(tmp_path: Path) -> Path:
    conf = tmp_path / "sources"
    (conf / "groups").mkdir(parents=True, exist_ok=True)
    (conf / "settings.yml").write_text(_SETTINGS, encoding="utf-8")
    (conf / "keywords.yml").write_text("aliases: {}\n", encoding="utf-8")
    (conf / "groups" / "g.yml").write_text(_GROUP, encoding="utf-8")

    data = tmp_path / "data"
    repo = JsonlSqliteRepository(data)
    repo.add_items(
        [
            _item("i-fresh", source_id="s-fresh", published_at="2026-07-07T10:00:00+00:00"),
            _item("i-quiet", source_id="s-quiet", published_at="2026-01-01T10:00:00+00:00"),
            _item("i-err", source_id="s-erroring", published_at="2026-07-06T10:00:00+00:00"),
        ]
    )
    repo.close()

    snapshot = HealthSnapshot(
        run_id="prior-run",
        generated_at="2026-07-08T09:00:00+00:00",
        sources=[
            SourceHealth(
                source_id="s-fresh",
                attempts=10,
                last_status=FetchStatus.OK,
                last_started_at="2026-07-08T08:00:00+00:00",
                consecutive_failures=0,
                flagged=False,
            ),
            SourceHealth(
                source_id="s-quiet",
                attempts=10,
                last_status=FetchStatus.OK,
                last_started_at="2026-07-08T08:00:00+00:00",
                consecutive_failures=0,
                flagged=False,
            ),
            SourceHealth(
                source_id="s-erroring",
                attempts=10,
                last_status=FetchStatus.ERROR,
                last_started_at="2026-07-08T08:00:00+00:00",
                last_error="s-erroring: HTTP 500",
                consecutive_failures=6,
                flagged=True,
            ),
        ],
    )
    layout = DataLayout(data)
    layout.health_file.parent.mkdir(parents=True, exist_ok=True)
    layout.health_file.write_text(snapshot.model_dump_json(indent=2) + "\n", encoding="utf-8")

    build_site(
        config=FilesystemConfigProvider(conf),
        repository=JsonlSqliteRepository(data),
        layout=layout,
        clock=_CLOCK,
        run_id=_RUN_ID,
        output_dir=tmp_path / "public",
        base_path="/",
    )
    return tmp_path / "public"


def test_sources_page_matches_golden(tmp_path: Path) -> None:
    html = (_build(tmp_path) / "sources" / "index.html").read_text(encoding="utf-8")
    assert html == (GOLDEN / "sources_index_coverage.html").read_text(encoding="utf-8")


def test_health_page_matches_golden(tmp_path: Path) -> None:
    html = (_build(tmp_path) / "health" / "index.html").read_text(encoding="utf-8")
    assert html == (GOLDEN / "health_index_coverage.html").read_text(encoding="utf-8")


def test_quiet_source_shows_recency_and_is_visually_distinct_from_erroring(
    tmp_path: Path,
) -> None:
    html = (_build(tmp_path) / "sources" / "index.html").read_text(encoding="utf-8")
    idx = html.index("<td>Quiet One</td>")
    row_block = html[idx : idx + 400]
    assert 'class="status-quiet"' in row_block
    assert "188 days ago" in row_block  # 2026-01-01 -> 2026-07-08

    err_idx = html.index("<td>Erroring One</td>")
    err_block = html[err_idx : err_idx + 400]
    assert 'class="status-quiet"' not in err_block
    assert "2 days ago" in err_block  # still contributing despite fetch errors


def test_dead_source_excluded_from_quiet_math(tmp_path: Path) -> None:
    html = (_build(tmp_path) / "sources" / "index.html").read_text(encoding="utf-8")
    dead_idx = html.index("<td>Dead One</td>")
    dead_block = html[dead_idx : dead_idx + 400]
    assert 'class="status-quiet"' not in dead_block
    assert "never" in dead_block
    # the callout only names live sources
    callout = next(line for line in html.splitlines() if "have not contributed" in line)
    assert "Dead One" not in callout
    assert "Quiet One" in callout


def test_health_rollup_counts_only_live_sources(tmp_path: Path) -> None:
    html = (_build(tmp_path) / "health" / "index.html").read_text(encoding="utf-8")
    assert "1 of 3 live sources contributed nothing in 30 days." in html
