"""Next-scheduled-digest-run tests (T4/GRP-63): pure Edmonton/DST-aware rollover.

Mirrors the DST-edge coverage style of ``tests/test_digest_gating.py`` - same
2026 transition dates (spring-forward March 8, fall-back November 1) - so the
two gate-adjacent pure functions are tested the same way. ``daily_exists``
plays the same role here as in :func:`grepify.digest.gating.digest_gate`: the
candidate only rolls past today's opening once today's digest is actually done.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from grepify.site.next_digest import NextDigestRun, next_scheduled_run


def test_next_run_rolls_to_tomorrow_when_today_already_done() -> None:
    # 2026-07-08 00:00 UTC = 2026-07-07 18:00 MDT (well past the 05:00 opening)
    result = next_scheduled_run(datetime(2026, 7, 8, tzinfo=UTC), daily_exists=True)
    assert result == NextDigestRun(
        date_label="2026-07-08",
        time_label="05:00",
        utc_offset_label="-06:00",
        is_weekly=False,  # 2026-07-08 is a Wednesday
    )


def test_next_run_stays_at_todays_overdue_open_when_still_missing() -> None:
    # Same instant as above, but today's digest never landed: the display stays
    # at today's (already past) opening rather than rolling to tomorrow - the
    # digest is overdue, not yet scheduled (GRP-63).
    result = next_scheduled_run(datetime(2026, 7, 8, tzinfo=UTC), daily_exists=False)
    assert result.date_label == "2026-07-07"
    assert result.time_label == "05:00"


def test_next_run_stays_today_when_before_the_gate() -> None:
    # 2026-07-08 09:00 UTC = 2026-07-08 03:00 MDT (before today's 05:00 opening);
    # existence cannot matter yet, the candidate is still in the future either way.
    for daily_exists in (True, False):
        instant = datetime(2026, 7, 8, 9, 0, tzinfo=UTC)
        result = next_scheduled_run(instant, daily_exists=daily_exists)
        assert result.date_label == "2026-07-08"
        assert result.time_label == "05:00"
        assert result.utc_offset_label == "-06:00"


def test_next_run_at_exact_gate_open_rolls_to_tomorrow_when_done() -> None:
    # 2026-07-08 11:00 UTC == 2026-07-08 05:00 MDT exactly: the gate has opened
    # for today and today's digest is done, so "next" is tomorrow's opening.
    result = next_scheduled_run(datetime(2026, 7, 8, 11, 0, tzinfo=UTC), daily_exists=True)
    assert result.date_label == "2026-07-09"
    assert result.time_label == "05:00"


def test_next_run_at_exact_gate_open_stays_today_when_missing() -> None:
    # Same instant, but nothing generated yet: today's opening is still "next".
    result = next_scheduled_run(datetime(2026, 7, 8, 11, 0, tzinfo=UTC), daily_exists=False)
    assert result.date_label == "2026-07-08"
    assert result.time_label == "05:00"


def test_next_run_flags_monday_as_also_weekly() -> None:
    # 2026-07-06 is a Monday; asking late on the Sunday before (its own 05:00
    # opening already past, and done) rolls onto it.
    result = next_scheduled_run(datetime(2026, 7, 6, 0, 0, tzinfo=UTC), daily_exists=True)
    assert result.date_label == "2026-07-06"
    assert result.is_weekly is True


def test_next_run_stays_on_sunday_when_still_missing() -> None:
    # Same instant, but Sunday's own digest never landed: the overdue Sunday
    # opening is still "next", not Monday's.
    result = next_scheduled_run(datetime(2026, 7, 6, 0, 0, tzinfo=UTC), daily_exists=False)
    assert result.date_label == "2026-07-05"
    assert result.is_weekly is False


def test_next_run_crosses_spring_forward_with_correct_offset() -> None:
    # 2026-03-08 is spring-forward day (DST begins). Asking late on 03-07
    # (still MST, -07:00), 03-07's own digest already done, rolls onto 03-08's
    # opening, already in MDT (-06:00).
    result = next_scheduled_run(datetime(2026, 3, 8, 3, 0, tzinfo=UTC), daily_exists=True)
    assert result.date_label == "2026-03-08"
    assert result.time_label == "05:00"
    assert result.utc_offset_label == "-06:00"


def test_next_run_crosses_fall_back_with_correct_offset() -> None:
    # 2026-11-01 is fall-back day (DST ends). Asking late on 10-31 (still MDT,
    # -06:00), 10-31's own digest already done, rolls onto 11-01's opening,
    # already back to MST (-07:00).
    result = next_scheduled_run(datetime(2026, 11, 1, 4, 0, tzinfo=UTC), daily_exists=True)
    assert result.date_label == "2026-11-01"
    assert result.time_label == "05:00"
    assert result.utc_offset_label == "-07:00"


def test_naive_instant_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        next_scheduled_run(datetime(2026, 7, 8, 13, 0), daily_exists=False)  # deliberately naive
