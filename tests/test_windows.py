"""Shared window arithmetic + rising-detection tests (GRP-71/GRP-40, GRP-72).

Covers :func:`~grepify.windows.window_ending_at` / :func:`~grepify.windows.previous_window`
(Edmonton-midnight alignment, DST transitions) and :func:`~grepify.windows.is_rising`
(F-TRD-03's config-driven predicate) - the primitives shared by
:mod:`grepify.digest.assemble` and :mod:`grepify.site.trends`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from grepify.windows import Window, is_rising, previous_window, window_ending_at

# 07-08 07:00 MDT: the window ends at the most recent Edmonton midnight
# (2026-07-08T06:00Z), so current is [2026-07-01, 2026-07-08) Edmonton days and
# previous is [2026-06-24, 2026-07-01) - the same items as before alignment.
_NOW = datetime(2026, 7, 8, 13, 0, tzinfo=UTC)


# --- window arithmetic -------------------------------------------------------


def test_window_ending_at_and_previous() -> None:
    window = window_ending_at(_NOW, days=7)
    # ends at Edmonton midnight (MDT, -06:00), not the raw instant
    assert window == Window(
        start="2026-07-01T06:00:00+00:00", end="2026-07-08T06:00:00+00:00", days=7
    )
    prev = previous_window(window)
    assert prev == Window(
        start="2026-06-24T06:00:00+00:00", end="2026-07-01T06:00:00+00:00", days=7
    )


def test_window_ending_at_is_stable_within_a_local_day() -> None:
    # Two instants on the same Edmonton day (07:00 vs 23:00 MDT) yield the same
    # window, so same-day rebuilds produce identical counts (GRP-71 AC).
    morning = window_ending_at(datetime(2026, 7, 8, 13, 0, tzinfo=UTC), days=7)
    evening = window_ending_at(datetime(2026, 7, 9, 4, 59, tzinfo=UTC), days=7)
    assert morning == evening


def test_window_construction_across_spring_forward() -> None:
    # The 7-day window ending Mon 2026-03-09 contains the Sun Mar 8 spring-
    # forward, so its bounds sit on different UTC offsets (MST -07:00 -> MDT
    # -06:00) yet stay exactly 7 Edmonton days, as does the previous window.
    window = window_ending_at(datetime(2026, 3, 9, 13, 0, tzinfo=UTC), days=7)
    assert window == Window(
        start="2026-03-02T07:00:00+00:00", end="2026-03-09T06:00:00+00:00", days=7
    )
    prev = previous_window(window)
    assert prev == Window(
        start="2026-02-23T07:00:00+00:00", end="2026-03-02T07:00:00+00:00", days=7
    )


def test_window_construction_across_fall_back() -> None:
    # The window ending Mon 2026-11-02 contains the Sun Nov 1 fall-back
    # (MDT -06:00 -> MST -07:00); both bounds stay Edmonton midnight.
    window = window_ending_at(datetime(2026, 11, 2, 13, 0, tzinfo=UTC), days=7)
    assert window == Window(
        start="2026-10-26T06:00:00+00:00", end="2026-11-02T07:00:00+00:00", days=7
    )


def test_window_ending_at_rejects_nonpositive_days() -> None:
    with pytest.raises(ValueError, match="positive"):
        window_ending_at(_NOW, days=0)


# --- rising detection ---------------------------------------------------------


@pytest.mark.parametrize(
    ("count", "previous", "expected"),
    [
        (3, 1, True),  # >= min_count(3) and ratio 3.0 >= 3.0
        (9, 3, True),  # ratio 3.0 exactly
        (10, 4, False),  # ratio 2.5 < 3.0
        (2, 0, False),  # below min_count even though it surged from nothing
        (3, 0, True),  # surged from nothing, clears min_count
        (5, 5, False),  # flat
        (2, 1, False),  # below min_count
    ],
)
def test_is_rising(count: int, previous: int, expected: bool) -> None:
    assert is_rising(count, previous, min_count=3, ratio=3.0) is expected


def test_thresholds_are_config_driven() -> None:
    # a stricter ratio flips a borderline case; a looser min_count admits a small one
    assert is_rising(4, 2, min_count=3, ratio=2.0) is True
    assert is_rising(4, 2, min_count=3, ratio=3.0) is False
    assert is_rising(2, 0, min_count=2, ratio=3.0) is True
