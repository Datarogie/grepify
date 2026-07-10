"""Period math tests (GRP-41/42): Edmonton day + ISO-week windows, DST edges.

Deterministic (instant injected); the interesting cases are the DST transitions,
where local-midnight boundaries land on different UTC offsets (MST -07:00 vs
MDT -06:00). America/Edmonton 2026: spring-forward Sun Mar 8, fall-back Sun Nov 1.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from grepify.digest.periods import previous_day, previous_iso_week


def test_previous_day_summer() -> None:
    # Wed 2026-07-08 07:00 MDT -> the day that just ended is 2026-07-07.
    period = previous_day(datetime(2026, 7, 8, 13, 0, tzinfo=UTC))
    assert period.key == "2026-07-07"
    assert period.days == 1
    assert period.start == "2026-07-07T06:00:00+00:00"  # local midnight, MDT (-06:00)
    assert period.end == "2026-07-08T06:00:00+00:00"


def test_previous_day_spans_fall_back_is_25h() -> None:
    # 2026-11-01 is the fall-back day (clocks go MDT->MST); the local day is 25h.
    period = previous_day(datetime(2026, 11, 2, 13, 0, tzinfo=UTC))
    assert period.key == "2026-11-01"
    assert period.start == "2026-11-01T06:00:00+00:00"  # MDT midnight
    assert period.end == "2026-11-02T07:00:00+00:00"  # MST midnight (offset changed)


def test_previous_iso_week_summer() -> None:
    # Mon 2026-07-13 -> previous ISO week is 2026-W28 (Mon 2026-07-06 .. Mon 07-13).
    period = previous_iso_week(datetime(2026, 7, 13, 13, 0, tzinfo=UTC))
    assert period.key == "2026-W28"
    assert period.days == 7
    assert period.start == "2026-07-06T06:00:00+00:00"
    assert period.end == "2026-07-13T06:00:00+00:00"


def test_previous_iso_week_spanning_dst_has_mixed_offsets() -> None:
    # The week Mon 2026-03-02 .. Mon 2026-03-09 contains the Sun Mar 8 spring-
    # forward, so its two local-midnight bounds sit on different UTC offsets.
    period = previous_iso_week(datetime(2026, 3, 9, 13, 0, tzinfo=UTC))
    assert period.key == "2026-W10"
    assert period.start == "2026-03-02T07:00:00+00:00"  # MST (-07:00)
    assert period.end == "2026-03-09T06:00:00+00:00"  # MDT (-06:00)


def test_previous_day_uses_edmonton_not_utc_boundary() -> None:
    # 2026-07-08 01:00 UTC is still 2026-07-07 19:00 in Edmonton, so "yesterday"
    # is 2026-07-06, not 2026-07-07 - the boundary is local, not UTC (PRD §5).
    period = previous_day(datetime(2026, 7, 8, 1, 0, tzinfo=UTC))
    assert period.key == "2026-07-06"


def test_naive_instant_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        previous_day(datetime(2026, 7, 8, 13, 0))  # deliberately naive (no tzinfo)
