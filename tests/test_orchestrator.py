"""GRP-15: ingest orchestrator - per-source isolation, caps, fetch_log, idempotency."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from grepify.clock import FixedClock
from grepify.config.filesystem import FilesystemConfigProvider
from grepify.errors import FetchError
from grepify.ingest.base import Fetcher, RawItem
from grepify.ingest.fake import FakeFetcher
from grepify.ingest.orchestrator import ITEM_CAP_DEFAULT, IngestServices, run_ingest
from grepify.ingest.reddit import _ITEM_CAP as _REDDIT_CLIENT_SIDE_CAP
from grepify.ingest.registry import FetcherRegistry
from grepify.models import FetchStatus, Source, SourceKind
from grepify.repository import JsonlSqliteRepository
from tests.conftest import write_config

_GROUPS = {
    "g1.yml": """
        group: g1
        name: G1
        category: ai
        sources:
          - id: good-src
            kind: rss
            url: https://example.com/good/feed
          - id: bad-src
            kind: rss
            url: https://example.com/bad/feed
          - id: boom-src
            kind: reddit
            subreddit: boom
          - id: empty-src
            kind: rss
            url: https://example.com/empty/feed
          - id: off-src
            kind: rss
            url: https://example.com/off/feed
            enabled: false
    """,
    "g2-disabled.yml": """
        group: g2
        name: G2 disabled
        category: ai
        enabled: false
        sources:
          - id: group-off-src
            kind: reddit
            subreddit: groupoff
    """,
}


def _raw(n: int) -> RawItem:
    return RawItem(url=f"https://example.com/item-{n}", title=f"item {n}", external_id=f"e{n}")


class _ExplodingFetcher(Fetcher):
    """A fetcher that raises a non-``FetchError`` exception (unexpected-failure path)."""

    @property
    def kind(self) -> SourceKind:
        return SourceKind.REDDIT

    def fetch(self, source: Source) -> list[RawItem]:
        raise ValueError("boom-unexpected")


def _registry() -> FetcherRegistry:
    reg = FetcherRegistry()
    reg.register(
        FakeFetcher(
            SourceKind.RSS,
            results={"bad-src": FetchError("dead feed"), "empty-src": []},
            default=[_raw(1)],
        )
    )
    reg.register(_ExplodingFetcher())
    return reg


def _services(tmp_path: Path, registry: FetcherRegistry) -> IngestServices:
    cfg_root = write_config(tmp_path / "sources", groups=_GROUPS)
    return IngestServices(
        config=FilesystemConfigProvider(cfg_root),
        repository=JsonlSqliteRepository(tmp_path / "data"),
        registry=registry,
        clock=FixedClock(datetime(2026, 7, 8, 12, 0, tzinfo=UTC)),
    )


def test_isolates_fetch_error_and_unexpected_exception(tmp_path: Path) -> None:
    services = _services(tmp_path, _registry())
    summary = run_ingest(services, run_id="run-1")

    by_id = {r.source_id: r for r in summary.results}
    assert by_id["good-src"].status is FetchStatus.OK
    assert by_id["good-src"].items_new == 1
    assert by_id["bad-src"].status is FetchStatus.ERROR
    assert by_id["bad-src"].error is not None and "dead feed" in by_id["bad-src"].error
    assert by_id["boom-src"].status is FetchStatus.ERROR
    assert by_id["boom-src"].error is not None and "boom-unexpected" in by_id["boom-src"].error
    assert by_id["empty-src"].status is FetchStatus.EMPTY
    assert by_id["empty-src"].items_new == 0

    assert summary.sources_attempted == 4  # off-src + group-off-src excluded
    assert summary.sources_ok == 1
    assert summary.sources_empty == 1
    assert summary.sources_error == 2
    assert summary.items_new == 1


def test_disabled_source_and_disabled_group_are_skipped(tmp_path: Path) -> None:
    services = _services(tmp_path, _registry())
    summary = run_ingest(services, run_id="run-1")
    fetched_ids = {r.source_id for r in summary.results}
    assert "off-src" not in fetched_ids
    assert "group-off-src" not in fetched_ids


def test_rerun_is_idempotent(tmp_path: Path) -> None:
    services = _services(tmp_path, _registry())
    run_ingest(services, run_id="run-1")
    second = run_ingest(services, run_id="run-2")

    by_id = {r.source_id: r for r in second.results}
    assert by_id["good-src"].status is FetchStatus.OK
    assert by_id["good-src"].items_new == 0
    assert second.items_new == 0


def test_per_run_cap_truncates_raw_items(tmp_path: Path) -> None:
    reg = FetcherRegistry()
    reg.register(FakeFetcher(SourceKind.RSS, default=[_raw(n) for n in range(80)]))
    reg.register(_ExplodingFetcher())
    services = _services(tmp_path, reg)

    summary = run_ingest(services, run_id="run-1", item_cap=10)
    by_id = {r.source_id: r for r in summary.results}
    assert by_id["good-src"].items_new == 10


def test_unregistered_source_kind_raises_and_stops_the_run(tmp_path: Path) -> None:
    groups = {
        "g.yml": """
            group: g
            name: G
            category: ai
            sources:
              - id: x-src
                kind: x
                handle: someone
        """
    }
    cfg_root = write_config(tmp_path / "sources", groups=groups)
    services = IngestServices(
        config=FilesystemConfigProvider(cfg_root),
        repository=JsonlSqliteRepository(tmp_path / "data"),
        registry=FetcherRegistry(),  # nothing registered for `x` - systemic, not a FetchError
        clock=FixedClock(datetime(2026, 7, 8, 12, 0, tzinfo=UTC)),
    )
    with pytest.raises(KeyError):
        run_ingest(services, run_id="run-1")


def test_item_cap_matches_reddit_fetchers_client_side_cap() -> None:
    # Guards against the two 50-item caps (orchestrator's ITEM_CAP_DEFAULT and
    # RedditFetcher's own client-side _ITEM_CAP) silently drifting apart -
    # the module docstring's "no double-truncation" claim depends on them
    # agreeing.
    assert ITEM_CAP_DEFAULT == _REDDIT_CLIENT_SIDE_CAP


def test_fetch_log_written_with_expected_fields(tmp_path: Path) -> None:
    services = _services(tmp_path, _registry())
    run_ingest(services, run_id="run-1")

    entries = list(services.repository.iter_fetch_log())
    by_id = {e.source_id: e for e in entries}
    assert by_id["good-src"].status is FetchStatus.OK
    assert by_id["good-src"].run_id == "run-1"
    assert by_id["good-src"].items_new == 1
    assert by_id["good-src"].duration_ms is not None
    assert by_id["bad-src"].status is FetchStatus.ERROR
    assert by_id["bad-src"].error is not None
    assert by_id["empty-src"].status is FetchStatus.EMPTY


# --- cadence (T6, GRP-31: Reddit best-effort scheduling) ---------------------


def _services_at(tmp_path: Path, registry: FetcherRegistry, instant: datetime) -> IngestServices:
    """Like ``_services`` but with an injectable clock instant, and a fresh
    repository/config handle pointed at the *same* on-disk data/config roots -
    so successive calls can simulate separate pipeline runs at different times
    while sharing the same persisted fetch_log history."""
    cfg_root = tmp_path / "sources"
    if not cfg_root.exists():
        write_config(cfg_root, groups=_GROUPS)
    return IngestServices(
        config=FilesystemConfigProvider(cfg_root),
        repository=JsonlSqliteRepository(tmp_path / "data"),
        registry=registry,
        clock=FixedClock(instant),
    )


def test_reddit_source_is_skipped_on_a_run_within_the_cadence_interval(tmp_path: Path) -> None:
    t0 = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)
    registry = _registry()
    services = _services_at(tmp_path, registry, t0)
    run_ingest(services, run_id="run-1")

    # 6h later - well within the default 20h reddit interval. If the skip were
    # not honored, the registered Reddit fetcher (_ExplodingFetcher) would be
    # dispatched and raise, turning this into an ERROR rather than SKIPPED.
    second = _services_at(tmp_path, _registry(), t0 + timedelta(hours=6))
    summary = run_ingest(second, run_id="run-2")

    by_id = {r.source_id: r for r in summary.results}
    assert by_id["boom-src"].status is FetchStatus.SKIPPED
    assert summary.sources_skipped == 1
    assert summary.sources_attempted == 3  # good/bad/empty-src; boom-src excluded


def test_rss_source_is_not_cadence_limited(tmp_path: Path) -> None:
    # rss has no configured min_interval_hours (default 0) - it is due on
    # every run regardless of how recently it was last attempted.
    t0 = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)
    services = _services_at(tmp_path, _registry(), t0)
    run_ingest(services, run_id="run-1")

    second = _services_at(tmp_path, _registry(), t0 + timedelta(minutes=1))
    summary = run_ingest(second, run_id="run-2")
    by_id = {r.source_id: r for r in summary.results}
    assert by_id["good-src"].status is FetchStatus.OK


def test_reddit_source_becomes_due_again_after_the_interval_elapses(tmp_path: Path) -> None:
    t0 = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)
    services = _services_at(tmp_path, _registry(), t0)
    run_ingest(services, run_id="run-1")

    later = _services_at(tmp_path, _registry(), t0 + timedelta(hours=21))
    summary = run_ingest(later, run_id="run-2")
    by_id = {r.source_id: r for r in summary.results}
    assert by_id["boom-src"].status is FetchStatus.ERROR  # a real attempt, not skipped


def test_cadence_skip_is_logged_to_fetch_log(tmp_path: Path) -> None:
    t0 = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)
    services = _services_at(tmp_path, _registry(), t0)
    run_ingest(services, run_id="run-1")

    second = _services_at(tmp_path, _registry(), t0 + timedelta(hours=6))
    run_ingest(second, run_id="run-2")

    entries = [e for e in second.repository.iter_fetch_log() if e.source_id == "boom-src"]
    assert entries[-1].status is FetchStatus.SKIPPED
    assert entries[-1].run_id == "run-2"
    assert entries[-1].items_new == 0
    assert entries[-1].error is None
    # A skip has no attempt to time; it is routed through the same _record
    # helper as a real attempt (T8), so this stays a fixed zero rather than
    # drifting if the skip and finish paths ever re-diverge.
    assert entries[-1].duration_ms == 0
