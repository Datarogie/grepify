"""Cron-gating tests (GRP-45/GRP-63): pure Edmonton/DST-aware digest gate.

The gate opens at 05:00 America/Edmonton and stays open - there is no closing
hour - until the period's own digest exists: a run before the opening never
fires; a run at or after it fires exactly when the period is still missing.
That is what makes a cron run that lands late in the day (or any retry) a
natural do-over instead of a silent miss (the production incident this issue
fixes: a 13:00 UTC cron slipping to 15:41 UTC, 09:41 Edmonton, silently skipped
the daily digest under the old fixed window). Weekly rides the same opening,
gated to Monday.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from grepify.digest.gating import DigestGate, digest_gate, format_gate


def _gate(
    instant: datetime, *, daily_exists: bool = False, weekly_exists: bool = False
) -> DigestGate:
    return digest_gate(instant, daily_exists=daily_exists, weekly_exists=weekly_exists)


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        # 13:00 UTC is inside the morning opening both seasons, nothing generated yet:
        (datetime(2026, 1, 5, 13, 0, tzinfo=UTC), DigestGate(True, True)),  # winter Mon 06:00 MST
        (datetime(2026, 7, 8, 13, 0, tzinfo=UTC), DigestGate(True, False)),  # summer Wed 07:00 MDT
        (datetime(2026, 7, 6, 13, 0, tzinfo=UTC), DigestGate(True, True)),  # summer Mon 07:00 MDT
        # before 05:00 Edmonton the gate never fires, regardless of existence:
        (datetime(2026, 7, 8, 10, 0, tzinfo=UTC), DigestGate(False, False)),  # 04:00 MDT
        # the opening edge (05:00 inclusive):
        (datetime(2026, 1, 6, 12, 0, tzinfo=UTC), DigestGate(True, False)),  # 05:00 MST
        # late-in-the-day cron slots: still due when missing - GRP-63's whole
        # point is that there is no closing hour to slip past anymore.
        (datetime(2026, 7, 8, 19, 0, tzinfo=UTC), DigestGate(True, False)),  # 13:00 MDT
        (datetime(2026, 7, 9, 1, 0, tzinfo=UTC), DigestGate(True, False)),  # prev-day 19:00 MDT
    ],
)
def test_digest_gate_when_nothing_generated_yet(instant: datetime, expected: DigestGate) -> None:
    assert _gate(instant) == expected


def test_late_run_generates_when_daily_still_missing() -> None:
    # The incident this issue fixes: a 13:00 UTC cron drifting to 15:41 UTC
    # (09:41 Edmonton) must still fire when today's digest never landed.
    late = datetime(2026, 7, 13, 15, 41, tzinfo=UTC)
    assert _gate(late, daily_exists=False).daily is True


def test_late_run_skips_when_daily_already_present() -> None:
    # Same late instant, but the idempotent skip (digest/pipeline.py) already
    # ran today: the gate must not ask for a second run.
    late = datetime(2026, 7, 13, 15, 41, tzinfo=UTC)
    assert _gate(late, daily_exists=True).daily is False


def test_before_window_never_generates_even_when_missing() -> None:
    # 04:00 Edmonton is before the opening; a missing digest still waits for it.
    before_open = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)  # 04:00 MDT
    assert _gate(before_open, daily_exists=False).daily is False


def test_weekly_follows_the_same_rule_gated_to_monday() -> None:
    monday_open = datetime(2026, 7, 6, 13, 0, tzinfo=UTC)  # Monday 07:00 MDT
    assert _gate(monday_open, weekly_exists=False).weekly is True
    assert _gate(monday_open, weekly_exists=True).weekly is False
    # a non-Monday day past the opening never sets weekly, whatever the existence:
    wednesday_open = datetime(2026, 7, 8, 13, 0, tzinfo=UTC)
    assert _gate(wednesday_open, weekly_exists=False).weekly is False


def test_daily_and_weekly_are_independent_existence_checks() -> None:
    # A Monday where daily already landed but weekly has not must still fire
    # weekly - they are separate (kind, period) pairs (GRP-63).
    monday = datetime(2026, 7, 6, 13, 0, tzinfo=UTC)
    gate = _gate(monday, daily_exists=True, weekly_exists=False)
    assert gate == DigestGate(daily=False, weekly=True)


def test_format_gate_shell_shape() -> None:
    assert format_gate(DigestGate(daily=True, weekly=False)) == "daily=true\nweekly=false"
    assert format_gate(DigestGate(daily=False, weekly=False)) == "daily=false\nweekly=false"


@pytest.mark.parametrize(
    ("instant", "expected_daily"),
    [
        # spring-forward Sunday 2026-03-08: 13:00 UTC is already 07:00 MDT (open)
        (datetime(2026, 3, 8, 13, 0, tzinfo=UTC), True),
        # fall-back Sunday 2026-11-01: 13:00 UTC is 06:00 MST (open)
        (datetime(2026, 11, 1, 13, 0, tzinfo=UTC), True),
        # the ambiguous local hour around fall-back (01:30 local repeats) is
        # still well before the opening under either resolved offset
        (datetime(2026, 11, 1, 8, 0, tzinfo=UTC), False),
    ],
)
def test_digest_gate_on_dst_transition_days(instant: datetime, expected_daily: bool) -> None:
    assert _gate(instant, daily_exists=False).daily is expected_daily


def test_weekly_never_true_without_daily_also_open() -> None:
    # weekly can never be true unless daily is also open (same opening hour);
    # with nothing generated yet, they move together across a full day.
    for hour in range(24):
        instant = datetime(2026, 1, 5, hour, 0, tzinfo=UTC)  # a Monday
        gate = _gate(instant)
        assert not (gate.weekly and not gate.daily)


def test_naive_instant_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        digest_gate(datetime(2026, 7, 8, 13, 0), daily_exists=False, weekly_exists=False)
