"""Cron-gating tests (GRP-45): pure Edmonton/DST-aware digest gate.

The 13:00 UTC cron is the one that must land in the 05:00-08:00 Edmonton morning
window across both DST offsets (06:00 MST in winter, 07:00 MDT in summer); the
other configured hours (19:00, 01:00 UTC) must not. Weekly rides the Monday
morning slot.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from grepify.digest.gating import DigestGate, digest_gate, format_gate


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        # 13:00 UTC lands in the morning window both seasons:
        (datetime(2026, 1, 5, 13, 0, tzinfo=UTC), DigestGate(True, True)),  # winter Mon 06:00 MST
        (datetime(2026, 7, 8, 13, 0, tzinfo=UTC), DigestGate(True, False)),  # summer Wed 07:00 MDT
        (datetime(2026, 7, 6, 13, 0, tzinfo=UTC), DigestGate(True, True)),  # summer Mon 07:00 MDT
        # the other two cron hours are outside the window:
        (datetime(2026, 7, 8, 19, 0, tzinfo=UTC), DigestGate(False, False)),  # 13:00 MDT
        (datetime(2026, 7, 8, 1, 0, tzinfo=UTC), DigestGate(False, False)),  # prev 19:00 MDT
        # window edges (05:00 and 08:00 inclusive), winter (13Z=06:00, 12Z=05:00, 15Z=08:00):
        (
            datetime(2026, 1, 6, 12, 0, tzinfo=UTC),
            DigestGate(True, False),
        ),  # 05:00 MST (lower edge)
        (
            datetime(2026, 1, 6, 15, 0, tzinfo=UTC),
            DigestGate(True, False),
        ),  # 08:00 MST (upper edge)
        (
            datetime(2026, 1, 6, 16, 0, tzinfo=UTC),
            DigestGate(False, False),
        ),  # 09:00 MST (just past)
    ],
)
def test_digest_gate(instant: datetime, expected: DigestGate) -> None:
    assert digest_gate(instant) == expected


def test_format_gate_shell_shape() -> None:
    assert format_gate(DigestGate(daily=True, weekly=False)) == "daily=true\nweekly=false"
    assert format_gate(DigestGate(daily=False, weekly=False)) == "daily=false\nweekly=false"


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        # spring-forward Sunday 2026-03-08: at 13:00 UTC DST has begun -> 07:00 MDT
        (datetime(2026, 3, 8, 13, 0, tzinfo=UTC), DigestGate(True, False)),  # Sunday, daily only
        # fall-back Sunday 2026-11-01: at 13:00 UTC clocks are back to MST -> 06:00 MST
        (datetime(2026, 11, 1, 13, 0, tzinfo=UTC), DigestGate(True, False)),  # Sunday, daily only
        # the ambiguous local hour around fall-back (01:30 local repeats) never
        # reaches the morning window, so the gate is unaffected by the fold
        (datetime(2026, 11, 1, 8, 0, tzinfo=UTC), DigestGate(False, False)),
    ],
)
def test_digest_gate_on_dst_transition_days(instant: datetime, expected: DigestGate) -> None:
    assert digest_gate(instant) == expected


def test_weekly_implies_daily() -> None:
    # weekly can never be true without daily (it rides the daily morning slot).
    for hour in range(24):
        gate = digest_gate(datetime(2026, 1, 5, hour, 0, tzinfo=UTC))  # a Monday
        assert not (gate.weekly and not gate.daily)


def test_naive_instant_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        digest_gate(datetime(2026, 7, 8, 13, 0))  # deliberately naive (no tzinfo)
