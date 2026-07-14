"""GRP-66: lifecycle rendering on the health + sources pages (pinned ACs).

Builds a site with one source per lifecycle class and a fetch-log snapshot that
includes a stale row for a since-removed (`gone`) source, then asserts the
pinned health-page ACs: disabled/dead sources land in a separate labelled
section (never a live flagged error), a degraded source is labelled with its
served rung, and a paywalled source carries its reader-facing message on the
sources page.
"""

from __future__ import annotations

import textwrap
from datetime import UTC, datetime
from pathlib import Path

from grepify.clock import FixedClock
from grepify.config.filesystem import FilesystemConfigProvider
from grepify.health import HealthSnapshot, SourceHealth
from grepify.models import FetchStatus, Rung
from grepify.paths import DataLayout
from grepify.repository.jsonl_sqlite import JsonlSqliteRepository
from grepify.site.build import build_site

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

_GROUP = textwrap.dedent(
    """
    group: g
    name: G
    category: ai
    sources:
      - {id: s-active, kind: rss, name: Active One, url: 'https://ex.com/a/feed'}
      - id: s-degraded
        kind: rss
        name: Degraded One
        url: https://ex.com/d/feed
        status: degraded
        active_url: https://ex.com/alt.xml
      - id: s-dead
        kind: rss
        name: Dead One
        url: https://ex.com/dead/feed
        status: dead
        evidence: "#66: full ladder failed; recheck 30d"
      - id: s-pay
        kind: rss
        name: Paywalled One
        url: https://ex.com/pay/feed
        status: paywalled
        message: "Subscriber-only feed. No free acquisition path; not attempted."
    """
).strip()


def _build(tmp_path: Path) -> Path:
    conf = tmp_path / "sources"
    (conf / "groups").mkdir(parents=True, exist_ok=True)
    (conf / "settings.yml").write_text(_SETTINGS, encoding="utf-8")
    (conf / "keywords.yml").write_text("aliases: {}\n", encoding="utf-8")
    (conf / "groups" / "g.yml").write_text(_GROUP, encoding="utf-8")

    data = tmp_path / "data"
    JsonlSqliteRepository(data).close()
    snapshot = HealthSnapshot(
        run_id="prior-run",
        generated_at="2026-07-08T09:00:00+00:00",
        sources=[
            SourceHealth(
                source_id="s-active",
                attempts=10,
                last_status=FetchStatus.OK,
                last_started_at="2026-07-08T08:00:00+00:00",
                consecutive_failures=0,
                flagged=False,
                last_rung=Rung.DIRECT,
            ),
            SourceHealth(
                source_id="s-degraded",
                attempts=10,
                last_status=FetchStatus.OK,
                last_started_at="2026-07-08T08:00:00+00:00",
                consecutive_failures=0,
                flagged=False,
                last_rung=Rung.AUTODISCOVERY,
            ),
            SourceHealth(
                source_id="s-dead",
                attempts=18,
                last_status=FetchStatus.ERROR,
                last_started_at="2026-07-08T08:00:00+00:00",
                last_error="s-dead: HTTP 403",
                consecutive_failures=18,
                flagged=True,
                last_rung=None,
            ),
            SourceHealth(
                source_id="ghost-removed",  # a `gone` source no longer in config
                attempts=18,
                last_status=FetchStatus.ERROR,
                last_started_at="2026-07-08T08:00:00+00:00",
                consecutive_failures=18,
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
        clock=FixedClock(datetime(2026, 7, 8, tzinfo=UTC)),
        run_id="run-1",
        output_dir=tmp_path / "public",
        base_path="/",
    )
    return tmp_path / "public"


def test_health_page_separates_disabled_from_live(tmp_path: Path) -> None:
    html = (_build(tmp_path) / "health" / "index.html").read_text(encoding="utf-8")
    live, _, disabled = html.partition("Disabled sources")
    assert disabled, "expected a separate disabled-sources section"
    # dead + paywalled render in the disabled section, not the live table.
    assert "Dead One" in disabled and "Paywalled One" in disabled
    assert "Dead One" not in live and "Paywalled One" not in live


def test_health_page_does_not_flag_dead_source_as_live_error(tmp_path: Path) -> None:
    html = (_build(tmp_path) / "health" / "index.html").read_text(encoding="utf-8")
    # The dead source's frozen 18-failure streak must not surface as a live
    # flagged (status-error) row - it lives in the collapsed section instead.
    assert "s-dead" not in html.split("Disabled sources")[0]


def test_health_page_labels_degraded_with_rung(tmp_path: Path) -> None:
    html = (_build(tmp_path) / "health" / "index.html").read_text(encoding="utf-8")
    assert "degraded (via autodiscovery)" in html


def test_gone_source_row_is_dropped_from_health(tmp_path: Path) -> None:
    html = (_build(tmp_path) / "health" / "index.html").read_text(encoding="utf-8")
    assert "ghost-removed" not in html


def test_sources_page_shows_paywalled_message(tmp_path: Path) -> None:
    html = (_build(tmp_path) / "sources" / "index.html").read_text(encoding="utf-8")
    assert "No free acquisition path; not attempted." in html
    assert "paywalled" in html


def test_health_drilldown_uses_native_details_and_escapes_hostile_values(tmp_path: Path) -> None:
    html = (_build(tmp_path) / "health" / "index.html").read_text(encoding="utf-8")
    assert "<details><summary>Active One</summary>" in html
    assert "<dt>Total real attempts</dt><dd>10</dd>" in html
    assert "filter items by source" in html
    assert "|safe" not in Path("grepify/site/templates/health.html").read_text(encoding="utf-8")


def test_health_page_is_deterministic(tmp_path: Path) -> None:
    first = (_build(tmp_path / "a") / "health" / "index.html").read_text(encoding="utf-8")
    second = (_build(tmp_path / "b") / "health" / "index.html").read_text(encoding="utf-8")
    assert first == second
