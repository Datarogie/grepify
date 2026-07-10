"""Cron gating for the digest steps (E4, GRP-45): which digests are due now?

The pipeline cron runs 3x/day; exactly one of those runs should attempt the
daily digest, and one run per week the weekly. This is the tested, DST-aware
pure function that replaces GRP-06's coarse bash placeholder
(``scripts/digest-gate.sh``): it converts the injected instant to
America/Edmonton local time and answers whether daily/weekly digest steps are
due (PRD §5 timezone decision).

The morning window is 05:00-08:00 Edmonton local - the slot the ``13:00`` UTC
cron lands in across *both* DST offsets (06:00 MST in winter, 07:00 MDT in
summer), and which no other configured cron hour reaches. ``weekly`` piggybacks
on the Monday-morning daily slot.

Determinism (F-SIT-08 / S8)
---------------------------
Pure: the instant is injected (``grepify digest-gate`` passes ``SystemClock``),
``zoneinfo`` supplies the offset, and the window bounds are constants. Same
instant in -> same gate out; DST-transition edges are covered by unit tests.

Failure modes
-------------
A naive (tz-unaware) instant raises ``ValueError`` (a programming error); no
I/O, nothing else raises.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from grepify.digest.periods import EDMONTON

MORNING_START_HOUR = 5  # inclusive, Edmonton local
MORNING_END_HOUR = 8  # inclusive, Edmonton local
MONDAY = 1  # datetime.isoweekday()


@dataclass(frozen=True)
class DigestGate:
    """Whether the daily / weekly digest steps are due for a given instant."""

    daily: bool
    weekly: bool


def digest_gate(instant: datetime) -> DigestGate:
    """Return which digest steps are due at ``instant`` (America/Edmonton)."""
    if instant.tzinfo is None:
        raise ValueError("digest gating requires a timezone-aware instant")
    local = instant.astimezone(EDMONTON)
    daily = MORNING_START_HOUR <= local.hour <= MORNING_END_HOUR
    weekly = daily and local.isoweekday() == MONDAY
    return DigestGate(daily=daily, weekly=weekly)


def format_gate(gate: DigestGate) -> str:
    """Render the gate as two ``key=value`` lines (``$GITHUB_OUTPUT`` / ``eval``).

    Lowercase ``true``/``false`` so a shell ``[ "$daily" = "true" ]`` test and a
    GitHub Actions ``steps.gate.outputs.daily == 'true'`` check both work
    unchanged (the bash placeholder emitted the same shape).
    """
    return f"daily={str(gate.daily).lower()}\nweekly={str(gate.weekly).lower()}"
