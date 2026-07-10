"""Clock abstraction.

Wall-clock access is funneled through :class:`Clock` so that timestamp
generation is injectable and tests are deterministic. The render path (E3) must
never call ``datetime.now()`` directly - it receives a clock (PRD §5, playbook
S8). Run-id and run-manifest generation (E0) use it too.

Failure modes
-------------
None intrinsic. :class:`SystemClock` reads the OS clock and always returns a
timezone-aware UTC datetime; a misconfigured OS clock is out of scope.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    """Source of the current instant. Implementations return tz-aware UTC."""

    def now(self) -> datetime:  # pragma: no cover - protocol
        ...


class SystemClock:
    """Real clock backed by the OS. Returns tz-aware UTC."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """Deterministic clock for tests. Returns the instant it was constructed with."""

    def __init__(self, instant: datetime) -> None:
        if instant.tzinfo is None:
            raise ValueError("FixedClock requires a timezone-aware datetime")
        self._instant = instant

    def now(self) -> datetime:
        return self._instant


def to_iso(instant: datetime) -> str:
    """Serialize an instant to a stable ISO-8601 string (UTC, second precision).

    Timestamps are stored as text everywhere (PRD §6 schema), keeping JSONL
    diffs readable and Postgres-swappable (Postgres accepts ISO text into
    ``timestamptz``).
    """
    return instant.astimezone(UTC).replace(microsecond=0).isoformat()
