"""GRP-16: health snapshot — consecutive-failure computation (PRD §8 F-ING-08)."""

from __future__ import annotations

from pathlib import Path

from grepify.health import (
    CONSECUTIVE_FAILURE_THRESHOLD,
    HealthSnapshot,
    compute_health,
    write_health_snapshot,
)
from grepify.models import FetchLogEntry, FetchStatus
from grepify.paths import DataLayout


def _entry(
    source_id: str,
    run_id: str,
    started_at: str,
    status: FetchStatus,
    *,
    error: str | None = None,
) -> FetchLogEntry:
    return FetchLogEntry(
        source_id=source_id,
        run_id=run_id,
        started_at=started_at,
        status=status,
        items_new=0,
        error=error,
        duration_ms=10,
    )


def test_flags_source_after_five_consecutive_errors() -> None:
    entries = [
        _entry("s1", f"r{n}", f"2026-07-0{n}T00:00:00+00:00", FetchStatus.ERROR, error="boom")
        for n in range(1, 6)
    ]
    snapshot = compute_health(entries, run_id="r5", generated_at="2026-07-06T00:00:00+00:00")

    assert len(snapshot.sources) == 1
    health = snapshot.sources[0]
    assert health.consecutive_failures == 5
    assert health.flagged is True
    assert health.attempts == 5
    assert health.last_status is FetchStatus.ERROR
    assert health.last_error == "boom"


def test_below_threshold_is_not_flagged() -> None:
    entries = [
        _entry("s1", f"r{n}", f"2026-07-0{n}T00:00:00+00:00", FetchStatus.ERROR)
        for n in range(1, CONSECUTIVE_FAILURE_THRESHOLD)
    ]
    snapshot = compute_health(entries, run_id="r", generated_at="2026-07-06T00:00:00+00:00")
    assert snapshot.sources[0].flagged is False
    assert snapshot.sources[0].consecutive_failures == CONSECUTIVE_FAILURE_THRESHOLD - 1


def test_a_success_resets_the_consecutive_count() -> None:
    entries = [
        _entry("s1", "r1", "2026-07-01T00:00:00+00:00", FetchStatus.ERROR),
        _entry("s1", "r2", "2026-07-02T00:00:00+00:00", FetchStatus.ERROR),
        _entry("s1", "r3", "2026-07-03T00:00:00+00:00", FetchStatus.OK),
    ]
    snapshot = compute_health(entries, run_id="r3", generated_at="2026-07-03T00:00:00+00:00")
    assert snapshot.sources[0].consecutive_failures == 0
    assert snapshot.sources[0].last_status is FetchStatus.OK


def test_empty_status_also_resets_consecutive_count() -> None:
    entries = [
        _entry("s1", "r1", "2026-07-01T00:00:00+00:00", FetchStatus.ERROR),
        _entry("s1", "r2", "2026-07-02T00:00:00+00:00", FetchStatus.EMPTY),
    ]
    snapshot = compute_health(entries, run_id="r2", generated_at="2026-07-02T00:00:00+00:00")
    assert snapshot.sources[0].consecutive_failures == 0


def test_flagged_source_is_never_auto_disabled() -> None:
    # v1 non-goal (PRD §2): no disabled/auto-disable concept exists at all -
    # flagged is purely informational, still retried every run.
    entries = [
        _entry("s1", f"r{n}", f"2026-07-0{n}T00:00:00+00:00", FetchStatus.ERROR)
        for n in range(1, 8)
    ]
    snapshot = compute_health(entries, run_id="r7", generated_at="2026-07-07T00:00:00+00:00")
    assert snapshot.sources[0].flagged is True
    assert not hasattr(snapshot.sources[0], "disabled")


def test_multiple_sources_sorted_by_id() -> None:
    entries = [
        _entry("zzz", "r1", "2026-07-01T00:00:00+00:00", FetchStatus.OK),
        _entry("aaa", "r1", "2026-07-01T00:00:00+00:00", FetchStatus.OK),
    ]
    snapshot = compute_health(entries, run_id="r1", generated_at="2026-07-01T00:00:00+00:00")
    assert [s.source_id for s in snapshot.sources] == ["aaa", "zzz"]


def test_empty_history_yields_empty_snapshot() -> None:
    snapshot = compute_health([], run_id="r1", generated_at="2026-07-01T00:00:00+00:00")
    assert snapshot.sources == []


def test_write_health_snapshot_writes_json(tmp_path: Path) -> None:
    layout = DataLayout(tmp_path)
    entries = [
        _entry("s1", "r1", "2026-07-01T00:00:00+00:00", FetchStatus.OK),
        _entry("s2", "r1", "2026-07-01T00:00:00+00:00", FetchStatus.ERROR, error="dead"),
    ]
    write_health_snapshot(entries, layout, run_id="r1", generated_at="2026-07-01T00:05:00+00:00")

    assert layout.health_file.exists()
    written = HealthSnapshot.model_validate_json(layout.health_file.read_text(encoding="utf-8"))
    assert written.run_id == "r1"
    assert written.generated_at == "2026-07-01T00:05:00+00:00"
    by_id = {s.source_id: s for s in written.sources}
    assert by_id["s1"].last_status is FetchStatus.OK
    assert by_id["s1"].consecutive_failures == 0
    assert by_id["s2"].last_status is FetchStatus.ERROR
    assert by_id["s2"].last_error == "dead"
    assert by_id["s2"].consecutive_failures == 1
    assert by_id["s2"].flagged is False
