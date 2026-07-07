"""Tests for run manifest I/O, clock, and path helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from grepify.clock import FixedClock, to_iso
from grepify.models import RunManifest
from grepify.paths import DataLayout, date_parts
from grepify.run import latest_manifest, new_run_id, write_manifest


def test_new_run_id_is_sortable_and_uses_entropy() -> None:
    clock = FixedClock(datetime(2026, 7, 7, 9, 30, 0, tzinfo=UTC))
    assert new_run_id(clock, entropy="abc123") == "20260707T093000Z-abc123"


def test_to_iso_truncates_microseconds() -> None:
    instant = datetime(2026, 7, 7, 9, 30, 0, 500000, tzinfo=UTC)
    assert to_iso(instant) == "2026-07-07T09:30:00+00:00"


def test_fixed_clock_requires_tzaware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        FixedClock(datetime(2026, 7, 7, 9, 30, 0))


def test_manifest_write_and_latest(tmp_path: Path) -> None:
    layout = DataLayout(tmp_path)
    assert latest_manifest(layout) is None

    older = RunManifest(run_id="20260707T090000Z-a", command="ingest", started_at="t")
    newer = RunManifest(run_id="20260707T100000Z-b", command="build", started_at="t")
    write_manifest(layout, older)
    write_manifest(layout, newer)

    latest = latest_manifest(layout)
    assert latest is not None
    assert latest.run_id == newer.run_id


def test_date_parts() -> None:
    assert date_parts("2026-07-07T10:00:00+00:00") == ("2026", "07", "07")
    with pytest.raises(ValueError, match="ISO-8601"):
        date_parts("not-a-date")
