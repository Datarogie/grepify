"""Next-scheduled-digest-run for the site (T4, one of the three v1.0.0 gates).

Surfaces :mod:`grepify.digest.gating`'s morning window (GRP-45, 05:00-08:59
America/Edmonton) as a "next scheduled digest run" instant for the health page,
without touching that module - :data:`grepify.digest.gating.MORNING_START_HOUR`
and ``MONDAY`` are read, not redefined. This is a pure render of the clock: the
next occurrence of the gate's opening hour after the injected instant, so the
same instant always yields the same next-run (F-SIT-08 / S8), and a build never
reads the wall clock a second time to compute it.

What "next" means
------------------
The candidate is today's Edmonton-local ``MORNING_START_HOUR:00``. If that
instant is already at or before "now" (the gate has opened for today, or the
build is running inside/after the window), the candidate rolls to tomorrow -
the pipeline's own build step never runs before the gate check it follows in
the same job, so "today's" window, once reached, is always already handled.
``zoneinfo`` resolves the UTC offset for whichever day the candidate lands on,
so a rollover across a DST transition picks up the correct new offset.

Failure modes
-------------
A naive (tz-unaware) instant raises ``ValueError`` (mirrors the gating module's
contract); everything else is pure arithmetic that never raises.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from grepify.digest.gating import MONDAY, MORNING_START_HOUR
from grepify.digest.periods import EDMONTON


@dataclass(frozen=True)
class NextDigestRun:
    """The next Edmonton-local instant the digest gate opens."""

    date_label: str  # "2026-07-12"
    time_label: str  # "05:00"
    utc_offset_label: str  # "-06:00" (MDT) / "-07:00" (MST), for the tooltip
    is_weekly: bool  # this occurrence also fires the weekly digest (Monday)


def next_scheduled_run(instant: datetime) -> NextDigestRun:
    """Return the next Edmonton-local digest-gate opening after ``instant``."""
    if instant.tzinfo is None:
        raise ValueError("next_scheduled_run requires a timezone-aware instant")
    local = instant.astimezone(EDMONTON)
    candidate = local.replace(hour=MORNING_START_HOUR, minute=0, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    offset = candidate.utcoffset()
    # Reason for the ignore below: type-narrowing invariant, not a runtime input check -
    # zoneinfo always resolves an offset for a real wall-clock instant.
    assert offset is not None  # noqa: S101
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hh, mm = divmod(abs(total_minutes), 60)
    return NextDigestRun(
        date_label=candidate.strftime("%Y-%m-%d"),
        time_label=candidate.strftime("%H:%M"),
        utc_offset_label=f"{sign}{hh:02d}:{mm:02d}",
        is_weekly=candidate.isoweekday() == MONDAY,
    )
