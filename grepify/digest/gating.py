"""Cron gating for the digest steps (E4, GRP-45/GRP-63): which digests are due now?

The pipeline cron runs 3x/day. This is the tested, DST-aware pure function
that replaces GRP-06's coarse bash placeholder (``scripts/digest-gate.sh``):
it converts the injected instant to America/Edmonton local time and answers
whether daily/weekly digest steps are due (PRD §5 timezone decision).

A step is due once local time reaches its opening hour *and* its digest does
not already exist for the current period (GRP-63): existence, not a closing
hour, ends the window. GitHub cron commonly drifts past a fixed window; with
no closing hour, a later-in-the-day run (the pipeline's 19:00/01:00 UTC slots)
becomes a natural retry for a morning run that never landed, and the
idempotent skip in :mod:`grepify.digest.pipeline` keeps a retry from ever
duplicating a digest. ``weekly`` uses the same rule, gated to Monday.

Determinism (F-SIT-08 / S8)
---------------------------
Pure: the instant and the existence flags are both injected (``grepify
digest-gate`` passes ``SystemClock`` plus a truth lookup, see
:mod:`grepify.cli`); ``zoneinfo`` supplies the offset and the window bound is
a constant. Same inputs in -> same gate out; DST-transition edges are covered
by unit tests.

Failure modes
-------------
A naive (tz-unaware) instant raises ``ValueError`` (a programming error); no
I/O, nothing else raises.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from grepify.digest.periods import EDMONTON

MORNING_START_HOUR = 5  # inclusive, Edmonton local; open-ended (GRP-63)
MONDAY = 1  # datetime.isoweekday()


@dataclass(frozen=True)
class DigestGate:
    """Whether the daily / weekly digest steps are due for a given instant."""

    daily: bool
    weekly: bool


def digest_gate(instant: datetime, *, daily_exists: bool, weekly_exists: bool) -> DigestGate:
    """Return which digest steps are due at ``instant`` (America/Edmonton).

    ``daily_exists``/``weekly_exists`` report whether the current period's
    digest is already in truth (the caller queries this, keeping this
    function a pure fold over its inputs); a step is due once local time is
    at or past :data:`MORNING_START_HOUR` and its own digest is still
    missing.
    """
    if instant.tzinfo is None:
        raise ValueError("digest gating requires a timezone-aware instant")
    local = instant.astimezone(EDMONTON)
    past_window_start = local.hour >= MORNING_START_HOUR
    daily = past_window_start and not daily_exists
    weekly = past_window_start and local.isoweekday() == MONDAY and not weekly_exists
    return DigestGate(daily=daily, weekly=weekly)


def format_gate(gate: DigestGate) -> str:
    """Render the gate as two ``key=value`` lines (``$GITHUB_OUTPUT`` / ``eval``).

    Lowercase ``true``/``false`` so a shell ``[ "$daily" = "true" ]`` test and a
    GitHub Actions ``steps.gate.outputs.daily == 'true'`` check both work
    unchanged (the bash placeholder emitted the same shape).
    """
    return f"daily={str(gate.daily).lower()}\nweekly={str(gate.weekly).lower()}"
