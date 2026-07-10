"""Rising-detection tests (GRP-40, F-TRD-03): the deterministic predicate."""

from __future__ import annotations

import pytest

from grepify.digest.rising import is_rising


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
