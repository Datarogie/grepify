"""Next-scheduled-digest-run for the site (T4, one of the three v1.0.0 gates).

Surfaces :mod:`grepify.digest.gating`'s morning opening (GRP-45/GRP-63,
05:00 America/Edmonton, no closing hour) as a "next scheduled digest run"
instant for the health page, without touching that module -
:data:`grepify.digest.gating.MORNING_START_HOUR` and ``MONDAY`` are read, not
redefined. This is a pure render of the clock + a caller-supplied existence
flag: the next occurrence of the gate's opening hour after the injected
instant, so the same inputs always yield the same next-run (F-SIT-08 / S8),
and a build never reads the wall clock or truth a second time to compute it.

What "next" means
------------------
The candidate is today's Edmonton-local ``MORNING_START_HOUR:00``. If that
instant is already at or before "now" *and* ``daily_exists`` is true (the
window opened today and the digest already ran), the candidate rolls to
tomorrow. If the window opened but the digest is still missing, the candidate
stays at today's opening - the same "at or past open, still missing" retry
condition the gate itself uses (GRP-63), so a display in that state
legitimately shows a past time: the digest is overdue, not yet scheduled.
``zoneinfo`` resolves the UTC offset for whichever day the candidate lands on,
so a rollover across a DST transition picks up the correct new offset. On a
Monday the rollover still keys off ``daily_exists`` only, not the weekly
digest's own existence - a Monday where daily is done but weekly is still
pending is not separately reflected in this display.

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


def next_scheduled_run(instant: datetime, *, daily_exists: bool) -> NextDigestRun:
    """Return the next Edmonton-local digest-gate opening after ``instant``.

    ``daily_exists`` reports whether today's daily digest is already in truth
    (the caller queries this, mirroring :func:`grepify.digest.gating.digest_gate`).
    """
    if instant.tzinfo is None:
        raise ValueError("next_scheduled_run requires a timezone-aware instant")
    local = instant.astimezone(EDMONTON)
    candidate = local.replace(hour=MORNING_START_HOUR, minute=0, second=0, microsecond=0)
    if candidate <= local and daily_exists:
        candidate += timedelta(days=1)
    offset = candidate.utcoffset()
    # S101: type-narrowing; zoneinfo always resolves an offset for a real instant.
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
