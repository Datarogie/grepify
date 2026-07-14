"""Trend-window + rising-detection primitives shared by digest and site (GRP-72).

``Window``, :func:`previous_window`, :func:`window_ending_at`, and
:func:`is_rising` are consumed by both :mod:`grepify.digest.assemble` (the
per-category digest input) and :mod:`grepify.site.trends` (the home page's
keyword cloud). Neither of those packages may import from the other, so these
primitives live here instead - a leaf module importing only
:mod:`grepify.digest.periods` for Edmonton day arithmetic, never anything from
:mod:`grepify.site`.

Determinism (F-SIT-08 / S8)
---------------------------
``window_ending_at`` takes the instant as an argument and aligns the window's
end to Edmonton midnight (GRP-71), so two builds on the same local day produce
an identical window and therefore identical counts.

Failure modes
-------------
Pure arithmetic; never raises except ``window_ending_at(..., days=<=0)``, which
raises ``ValueError`` (propagated from :func:`grepify.digest.periods.trailing_days`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from grepify.clock import from_iso, to_iso


@dataclass(frozen=True)
class Window:
    """A half-open ``[start, end)`` window of ISO-8601 UTC timestamp strings."""

    start: str
    end: str
    days: int


def previous_window(window: Window) -> Window:
    """The immediately-preceding window of the same day-length (for deltas).

    Both bounds are resolved in Edmonton local time, so the current and previous
    windows span the same number of lived days even across a DST transition
    (keeping deltas/rising ratios comparable, GRP-71).
    """
    end_local = from_iso(window.start).astimezone(EDMONTON)
    start_local = end_local - timedelta(days=window.days)
    return Window(start=to_iso(start_local), end=window.start, days=window.days)


def is_rising(count: int, previous_count: int, *, min_count: int, ratio: float) -> bool:
    """Return whether ``count`` is rising vs ``previous_count`` (F-TRD-03).

    A keyword is rising when it is both common enough (``count >= min_count``)
    and accelerating window-over-window (``count / previous_count >= ratio``); a
    keyword surging from nothing (``previous_count == 0``) is rising as soon as
    it clears ``min_count``.
    """
    if count < min_count:
        return False
    if previous_count <= 0:
        return True  # surged from nothing - rising once it clears min_count
    return count / previous_count >= ratio


# grepify.digest.assemble imports Window/previous_window/is_rising from this
# module at load time, so this import must come after they are defined above:
# if grepify.windows is imported before grepify.digest, this line re-enters
# grepify.digest.assemble while it is still waiting on those names.
from grepify.digest.periods import EDMONTON, trailing_days  # noqa: E402


def window_ending_at(instant: datetime, *, days: int) -> Window:
    """Window of ``days`` Edmonton calendar days ending at the local midnight at
    or before ``instant`` (injected - never a clock read).

    Aligning the end to Edmonton midnight rather than ``instant`` (GRP-71) makes
    counts identical across same-day rebuilds and makes each sparkline bucket
    exactly one lived day. ``days <= 0`` raises ``ValueError``.
    """
    period = trailing_days(instant, days)
    return Window(start=period.start, end=period.end, days=days)
