"""Tests for scripts/digest-gate.sh (GRP-06 coarse time-of-day gate)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "digest-gate.sh"


@pytest.mark.parametrize(
    ("hour", "weekday", "expected_daily", "expected_weekly"),
    [
        ("6", "1", "true", "true"),  # Monday morning: both due
        ("6", "3", "true", "false"),  # Wednesday morning: daily only
        ("12", "1", "false", "false"),  # midday: neither
        ("5", "1", "true", "true"),  # window lower edge
        ("8", "1", "true", "true"),  # window upper edge
        ("9", "1", "false", "false"),  # just past the window
        ("4", "1", "false", "false"),  # just before the window
        ("6", "7", "true", "false"),  # Sunday morning: daily only
    ],
)
def test_digest_gate(hour: str, weekday: str, expected_daily: str, expected_weekly: str) -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        check=True,
        env={
            "GREPIFY_FAKE_HOUR": hour,
            "GREPIFY_FAKE_WEEKDAY": weekday,
            "PATH": "/usr/bin:/bin",
        },
    )
    assert result.stdout.strip().splitlines() == [
        f"daily={expected_daily}",
        f"weekly={expected_weekly}",
    ]
