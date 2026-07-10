"""Digest period math (E4, GRP-41/42): America/Edmonton day + ISO-week windows.

The daily digest covers the **just-completed** Edmonton day (yesterday relative
to the injected instant); the weekly digest covers the **just-completed** ISO
week (last Monday-Sunday). Boundaries are computed in America/Edmonton, then
serialized to UTC ISO strings so they compare lexicographically against the
cache's ``published_at`` text (PRD §5 timezone decision; §6 timestamps-as-text).

Determinism (F-SIT-08 / S8)
---------------------------
Every function takes the instant as an argument - there is no clock read here.
``zoneinfo`` is used for the Edmonton offset (DST-aware); ``tzdata`` is a
dependency so the zone is available on any runner. Same instant in -> same
period out.

Failure modes
-------------
Pure computation. A naive (tz-unaware) instant raises ``ValueError`` (a
programming error); everything else is arithmetic that never raises.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from grepify.clock import to_iso

EDMONTON = ZoneInfo("America/Edmonton")


@dataclass(frozen=True)
class Period:
    """A half-open ``[start, end)`` digest period, UTC ISO strings + a label.

    ``key`` is the period component of a ``digest_id`` (``YYYY-MM-DD`` for daily,
    ``YYYY-Www`` for weekly, PRD §6); ``days`` is the span for the trend window.
    """

    start: str
    end: str
    key: str
    days: int


def _local_date(instant: datetime) -> datetime:
    if instant.tzinfo is None:
        raise ValueError("period math requires a timezone-aware instant")
    return instant.astimezone(EDMONTON)


def _midnight(local_day: datetime) -> datetime:
    """Edmonton midnight at the start of ``local_day`` (offset resolved for DST)."""
    return local_day.replace(hour=0, minute=0, second=0, microsecond=0)


def previous_day(instant: datetime) -> Period:
    """The Edmonton day that ended most recently before ``instant`` (F-DIG-01)."""
    local = _local_date(instant)
    day_start = _midnight(local) - timedelta(days=1)
    day_end = _midnight(local)
    return Period(
        start=to_iso(day_start),
        end=to_iso(day_end),
        key=day_start.strftime("%Y-%m-%d"),
        days=1,
    )


def previous_iso_week(instant: datetime) -> Period:
    """The most-recently-completed ISO week (Mon-Sun) in Edmonton (F-DIG-02)."""
    local = _local_date(instant)
    # Monday of the current local week, then step back one full week.
    this_monday = _midnight(local) - timedelta(days=local.isoweekday() - 1)
    week_start = this_monday - timedelta(days=7)
    iso_year, iso_week, _ = week_start.isocalendar()
    return Period(
        start=to_iso(week_start),
        end=to_iso(this_monday),
        key=f"{iso_year}-W{iso_week:02d}",
        days=7,
    )
