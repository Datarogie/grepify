"""GRP-15: ingest orchestrator — per-source isolation, caps, fetch_log, idempotency."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from grepify.clock import FixedClock
from grepify.config.filesystem import FilesystemConfigProvider
from grepify.errors import FetchError
from grepify.ingest.base import Fetcher, RawItem
from grepify.ingest.fake import FakeFetcher
from grepify.ingest.orchestrator import IngestServices, run_ingest
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
