"""Clock helpers: ``to_iso`` / ``from_iso`` round-trip (T8 audit).

``from_iso`` is the single guarded parse site for stored timestamps (mirrors
``to_iso``); these lock in that it inverts ``to_iso`` exactly and stays
timezone-aware, and that a non-ISO string still raises ``ValueError`` (the same
failure the ad-hoc ``datetime.fromisoformat`` call sites used to raise).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from grepify.clock import from_iso, to_iso


def test_from_iso_inverts_to_iso() -> None:
    # to_iso drops sub-second precision, so the round-trip lands on the
    # second-truncated instant.
    instant = datetime(2026, 7, 8, 12, 30, 15, tzinfo=UTC)
    assert from_iso(to_iso(instant)) == instant


def test_from_iso_is_timezone_aware() -> None:
    parsed = from_iso(to_iso(datetime(2026, 1, 1, tzinfo=UTC)))
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


def test_from_iso_normalizes_a_non_utc_instant_through_to_iso() -> None:
    # An instant in another zone is stored as UTC by to_iso and parses back to
    # the same absolute instant.
    mountain = datetime(2026, 7, 8, 6, 0, tzinfo=timezone(timedelta(hours=-6)))
    assert from_iso(to_iso(mountain)) == datetime(2026, 7, 8, 12, 0, tzinfo=UTC)


def test_from_iso_raises_on_garbage() -> None:
    with pytest.raises(ValueError):
        from_iso("not-a-timestamp")
