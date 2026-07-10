"""Rising-keyword detection (E4, GRP-40, PRD §8 F-TRD-03).

A pure, config-driven predicate: a keyword is "rising" in a window when it is
both common enough (``count >= min_count``) and accelerating window-over-window
(``count / previous_count >= ratio``). A keyword surging from nothing
(``previous_count == 0``) is rising as soon as it clears ``min_count``. Feeds the
digest prompt and a cloud badge; the thresholds live in ``settings.digest``
(``rising_min_count`` / ``rising_ratio``) so they are tunable without a code
change (F-TRD-03: "config-driven").

Failure modes
-------------
Pure arithmetic over non-negative counts; never raises or performs I/O. A
negative ``previous_count`` is impossible (counts are set cardinalities) and is
treated as zero for safety.
"""

from __future__ import annotations


def is_rising(count: int, previous_count: int, *, min_count: int, ratio: float) -> bool:
    """Return whether ``count`` is rising vs ``previous_count`` (F-TRD-03)."""
    if count < min_count:
        return False
    if previous_count <= 0:
        return True  # surged from nothing - rising once it clears min_count
    return count / previous_count >= ratio
