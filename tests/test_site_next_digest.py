"""Next-scheduled-digest-run tests (T4): pure Edmonton/DST-aware rollover.

Mirrors the DST-edge coverage style of ``tests/test_digest_gating.py`` - same
2026 transition dates (spring-forward March 8, fall-back November 1) - so the
two gate-adjacent pure functions are tested the same way.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from grepify.site.next_digest import NextDigestRun, next_scheduled_run


def test_next_run_rolls_to_tomorrow_when_today_already_past() -> None:
    # 2026-07-08 00:00 UTC = 2026-07-07 18:00 MDT (well past the 05:00 gate)
    result = next_scheduled_run(datetime(2026, 7, 8, tzinfo=UTC))
    assert result == NextDigestRun(
        date_label="2026-07-08",
        time_label="05:00",
        utc_offset_label="-06:00",
        is_weekly=False,  # 2026-07-08 is a Wednesday
    )


def test_next_run_stays_today_when_before_the_gate() -> None:
    # 2026-07-08 09:00 UTC = 2026-07-08 03:00 MDT (before today's 05:00 opening)
    result = next_scheduled_run(datetime(2026, 7, 8, 9, 0, tzinfo=UTC))
    assert result.date_label == "2026-07-08"
    assert result.time_label == "05:00"
    assert result.utc_offset_label == "-06:00"


def test_next_run_at_exact_gate_open_rolls_to_tomorrow() -> None:
    # 2026-07-08 11:00 UTC == 2026-07-08 05:00 MDT exactly: the gate has opened
    # for today, so "next" is tomorrow's opening, not the instant itself.
    result = next_scheduled_run(datetime(2026, 7, 8, 11, 0, tzinfo=UTC))
    assert result.date_label == "2026-07-09"
    assert result.time_label == "05:00"


def test_next_run_flags_monday_as_also_weekly() -> None:
    # 2026-07-06 is a Monday; asking from the Sunday before rolls onto it.
    result = next_scheduled_run(datetime(2026, 7, 6, 0, 0, tzinfo=UTC))  # Sun 18:00 MDT prior day
    assert result.date_label == "2026-07-06"
    assert result.is_weekly is True


def test_next_run_crosses_spring_forward_with_correct_offset() -> None:
    # 2026-03-08 is spring-forward day (DST begins). Asking late on 03-07
    # (still MST, -07:00) rolls onto 03-08's opening, already in MDT (-06:00).
    result = next_scheduled_run(datetime(2026, 3, 8, 3, 0, tzinfo=UTC))  # 2026-03-07 20:00 MST
    assert result.date_label == "2026-03-08"
    assert result.time_label == "05:00"
    assert result.utc_offset_label == "-06:00"


def test_next_run_crosses_fall_back_with_correct_offset() -> None:
    # 2026-11-01 is fall-back day (DST ends). Asking late on 10-31 (still MDT,
    # -06:00) rolls onto 11-01's opening, already back to MST (-07:00).
    result = next_scheduled_run(datetime(2026, 11, 1, 4, 0, tzinfo=UTC))  # 2026-10-31 22:00 MDT
    assert result.date_label == "2026-11-01"
    assert result.time_label == "05:00"
    assert result.utc_offset_label == "-07:00"


def test_naive_instant_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        next_scheduled_run(datetime(2026, 7, 8, 13, 0))  # deliberately naive
