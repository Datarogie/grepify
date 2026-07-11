"""T6, GRP-31: per-source-kind fetch cadence (best-effort Reddit scheduling)."""

from __future__ import annotations

from datetime import UTC, datetime

from grepify.ingest.cadence import last_real_attempt_at, split_by_cadence
from grepify.models import FetchLogEntry, FetchStatus, Source, SourceKind


def _entry(source_id: str, started_at: str, status: FetchStatus) -> FetchLogEntry:
    return FetchLogEntry(
        source_id=source_id, run_id="r", started_at=started_at, status=status, items_new=0
    )


def _source(source_id: str, kind: SourceKind) -> Source:
    return Source(
        source_id=source_id,
        name=source_id,
        kind=kind,
        url=f"https://example.com/{source_id}",
        url_hash="hash",
        group_id="g",
        added_at="2026-01-01T00:00:00+00:00",
    )


# --- last_real_attempt_at -----------------------------------------------------


def test_last_real_attempt_at_takes_the_latest_entry_per_source() -> None:
    entries = [
        _entry("s1", "2026-07-01T00:00:00+00:00", FetchStatus.ERROR),
        _entry("s1", "2026-07-02T00:00:00+00:00", FetchStatus.OK),
    ]
    result = last_real_attempt_at(entries)
    assert result["s1"] == datetime(2026, 7, 2, tzinfo=UTC)


def test_last_real_attempt_at_ignores_skipped_entries() -> None:
    # Regression: if a `skipped` entry counted as the "last real attempt", a
    # cadence skip logged every run would keep pushing the reference forward
    # by only the run's own gap each time, and the source would never
    # accumulate enough elapsed time to become due again.
    entries = [
        _entry("s1", "2026-07-01T00:00:00+00:00", FetchStatus.ERROR),
        _entry("s1", "2026-07-01T06:00:00+00:00", FetchStatus.SKIPPED),
        _entry("s1", "2026-07-01T12:00:00+00:00", FetchStatus.SKIPPED),
    ]
    result = last_real_attempt_at(entries)
    assert result["s1"] == datetime(2026, 7, 1, tzinfo=UTC)


def test_last_real_attempt_at_empty_history_for_unseen_source() -> None:
    assert last_real_attempt_at([]) == {}


# --- split_by_cadence ----------------------------------------------------------


def test_source_with_no_history_is_always_due() -> None:
    reddit = _source("r1", SourceKind.REDDIT)
    decision = split_by_cadence(
        [reddit],
        now=datetime(2026, 7, 1, tzinfo=UTC),
        last_real_attempt={},
        min_interval_hours={SourceKind.REDDIT: 20},
    )
    assert decision.due == [reddit]
    assert decision.skipped == []


def test_source_within_interval_is_skipped() -> None:
    reddit = _source("r1", SourceKind.REDDIT)
    decision = split_by_cadence(
        [reddit],
        now=datetime(2026, 7, 1, 6, tzinfo=UTC),
        last_real_attempt={"r1": datetime(2026, 7, 1, 0, tzinfo=UTC)},
        min_interval_hours={SourceKind.REDDIT: 20},
    )
    assert decision.due == []
    assert decision.skipped == [reddit]


def test_source_past_interval_is_due_again() -> None:
    reddit = _source("r1", SourceKind.REDDIT)
    decision = split_by_cadence(
        [reddit],
        now=datetime(2026, 7, 2, 0, tzinfo=UTC),  # exactly 24h later
        last_real_attempt={"r1": datetime(2026, 7, 1, 0, tzinfo=UTC)},
        min_interval_hours={SourceKind.REDDIT: 20},
    )
    assert decision.due == [reddit]
    assert decision.skipped == []


def test_unconfigured_kind_is_always_due() -> None:
    rss = _source("s1", SourceKind.RSS)
    decision = split_by_cadence(
        [rss],
        now=datetime(2026, 7, 1, 6, tzinfo=UTC),
        last_real_attempt={"s1": datetime(2026, 7, 1, 0, tzinfo=UTC)},
        min_interval_hours={SourceKind.REDDIT: 20},  # rss absent -> default 0
    )
    assert decision.due == [rss]


def test_zero_or_negative_interval_is_always_due() -> None:
    reddit = _source("r1", SourceKind.REDDIT)
    decision = split_by_cadence(
        [reddit],
        now=datetime(2026, 7, 1, 0, 1, tzinfo=UTC),
        last_real_attempt={"r1": datetime(2026, 7, 1, 0, 0, tzinfo=UTC)},
        min_interval_hours={SourceKind.REDDIT: 0},
    )
    assert decision.due == [reddit]


def test_mixed_kinds_split_independently() -> None:
    reddit = _source("r1", SourceKind.REDDIT)
    rss = _source("s1", SourceKind.RSS)
    now = datetime(2026, 7, 1, 6, tzinfo=UTC)
    last_real_attempt = {
        "r1": datetime(2026, 7, 1, 0, tzinfo=UTC),
        "s1": datetime(2026, 7, 1, 0, tzinfo=UTC),
    }
    decision = split_by_cadence(
        [reddit, rss],
        now=now,
        last_real_attempt=last_real_attempt,
        min_interval_hours={SourceKind.REDDIT: 20},
    )
    assert decision.due == [rss]
    assert decision.skipped == [reddit]
