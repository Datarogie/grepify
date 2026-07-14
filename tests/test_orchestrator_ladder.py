"""GRP-66: orchestrator lifecycle dispatch + served-rung recording (ADR 0002)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from grepify.clock import FixedClock
from grepify.config.filesystem import FilesystemConfigProvider
from grepify.ingest.base import Fetcher, FetchOutcome, RawItem
from grepify.ingest.fake import FakeFetcher
from grepify.ingest.orchestrator import IngestServices, run_ingest
from grepify.ingest.registry import FetcherRegistry
from grepify.models import FetchStatus, Rung, Source, SourceKind
from grepify.repository import JsonlSqliteRepository
from tests.conftest import write_config

_GROUP = """
    group: g
    name: G
    category: ai
    sources:
      - {id: active-src, kind: rss, url: 'https://x/active/feed'}
      - id: degraded-src
        kind: rss
        url: https://x/degraded/feed
        status: degraded
        active_url: https://x/alt.xml
      - id: dead-src
        kind: rss
        url: https://x/dead/feed
        status: dead
        evidence: "#66: full ladder failed; recheck 30d"
      - id: paywall-src
        kind: rss
        url: https://x/pay/feed
        status: paywalled
        message: "Subscriber-only feed; not attempted."
      - {id: bare-off, kind: rss, url: 'https://x/off/feed', enabled: false}
"""


def _raw(n: int) -> RawItem:
    return RawItem(url=f"https://x/item-{n}", title=f"item {n}", external_id=f"e{n}")


class _DegradedFetcher(Fetcher):
    """RSS-kind fetcher that serves ``degraded-src`` from a fallback rung."""

    @property
    def kind(self) -> SourceKind:
        return SourceKind.RSS

    def fetch(self, source: Source) -> list[RawItem]:
        return self.acquire(source).items

    def acquire(self, source: Source) -> FetchOutcome:
        if source.source_id == "degraded-src":
            return FetchOutcome([_raw(1)], Rung.ALT_ENDPOINT, "https://x/alt.xml")
        return FetchOutcome([_raw(1)], Rung.DIRECT)


def _services(tmp_path: Path, clock: FixedClock, fetcher: Fetcher) -> IngestServices:
    registry = FetcherRegistry()
    registry.register(fetcher)
    return IngestServices(
        config=FilesystemConfigProvider(
            write_config(tmp_path / "sources", groups={"g.yml": _GROUP})
        ),
        repository=JsonlSqliteRepository(tmp_path / "data"),
        registry=registry,
        clock=clock,
    )


def test_mixed_class_run_dispatches_only_fetchable_sources(tmp_path: Path) -> None:
    services = _services(
        tmp_path, FixedClock(datetime(2026, 7, 8, 12, tzinfo=UTC)), _DegradedFetcher()
    )
    summary = run_ingest(services, run_id="run-1")
    dispatched = {r.source_id for r in summary.results}
    # active + degraded + explicitly-dead (re-check) are dispatched; paywalled
    # (terminal) and a bare legacy `enabled: false` are excluded entirely.
    assert dispatched == {"active-src", "degraded-src", "dead-src"}
    assert "paywall-src" not in dispatched
    assert "bare-off" not in dispatched


def test_served_rung_is_recorded_on_the_result_and_fetch_log(tmp_path: Path) -> None:
    services = _services(
        tmp_path, FixedClock(datetime(2026, 7, 8, 12, tzinfo=UTC)), _DegradedFetcher()
    )
    summary = run_ingest(services, run_id="run-1")
    by_id = {r.source_id: r for r in summary.results}
    assert by_id["active-src"].rung is Rung.DIRECT
    assert by_id["degraded-src"].rung is Rung.ALT_ENDPOINT

    log = {e.source_id: e for e in services.repository.iter_fetch_log()}
    assert log["degraded-src"].rung is Rung.ALT_ENDPOINT


def test_dead_recheck_is_gated_to_the_slow_cadence(tmp_path: Path) -> None:
    t0 = datetime(2026, 7, 8, 12, tzinfo=UTC)
    fetcher = FakeFetcher(SourceKind.RSS, default=[_raw(1)])
    run_ingest(_services(tmp_path, FixedClock(t0), fetcher), run_id="run-1")

    # 6h later - far inside the 30-day dead re-check interval.
    second = _services(
        tmp_path,
        FixedClock(t0 + timedelta(hours=6)),
        FakeFetcher(SourceKind.RSS, default=[_raw(1)]),
    )
    summary = run_ingest(second, run_id="run-2")
    by_id = {r.source_id: r for r in summary.results}
    assert by_id["dead-src"].status is FetchStatus.SKIPPED  # not re-probed yet
    assert by_id["active-src"].status is FetchStatus.OK  # rss active has no cadence limit


def test_double_run_is_idempotent_across_classes(tmp_path: Path) -> None:
    services = _services(
        tmp_path, FixedClock(datetime(2026, 7, 8, 12, tzinfo=UTC)), _DegradedFetcher()
    )
    run_ingest(services, run_id="run-1")
    second = run_ingest(services, run_id="run-2")
    assert second.items_new == 0
