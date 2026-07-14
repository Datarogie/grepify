"""GRP-16: health snapshot - consecutive-failure computation (PRD §8 F-ING-08)."""

from __future__ import annotations

from pathlib import Path

import pytest

from grepify.health import (
    CONSECUTIVE_FAILURE_THRESHOLD,
    ErrorClass,
    HealthSnapshot,
    classify_error,
    compute_health,
    write_health_snapshot,
)
from grepify.models import FetchLogEntry, FetchStatus
from grepify.paths import DataLayout


def _entry(
    source_id: str,
    run_id: str,
    started_at: str,
    status: FetchStatus,
    *,
    error: str | None = None,
) -> FetchLogEntry:
    return FetchLogEntry(
        source_id=source_id,
        run_id=run_id,
        started_at=started_at,
        status=status,
        items_new=0,
        error=error,
        duration_ms=10,
    )


def test_is_real_attempt_predicate_treats_only_skipped_as_a_non_attempt() -> None:
    # The single definition both the cadence math and this health rollup filter
    # on: SKIPPED is the one non-attempt status, everything else counts. A new
    # non-attempt status must be added here so cadence and health can never
    # disagree about what an attempt is.
    assert FetchStatus.SKIPPED.is_real_attempt is False
    assert all(
        status.is_real_attempt for status in (FetchStatus.OK, FetchStatus.EMPTY, FetchStatus.ERROR)
    )


def test_flags_source_after_five_consecutive_errors() -> None:
    entries = [
        _entry("s1", f"r{n}", f"2026-07-0{n}T00:00:00+00:00", FetchStatus.ERROR, error="boom")
        for n in range(1, 6)
    ]
    snapshot = compute_health(entries, run_id="r5", generated_at="2026-07-06T00:00:00+00:00")

    assert len(snapshot.sources) == 1
    health = snapshot.sources[0]
    assert health.consecutive_failures == 5
    assert health.flagged is True
    assert health.attempts == 5
    assert health.last_status is FetchStatus.ERROR
    assert health.last_error == "boom"


def test_below_threshold_is_not_flagged() -> None:
    entries = [
        _entry("s1", f"r{n}", f"2026-07-0{n}T00:00:00+00:00", FetchStatus.ERROR)
        for n in range(1, CONSECUTIVE_FAILURE_THRESHOLD)
    ]
    snapshot = compute_health(entries, run_id="r", generated_at="2026-07-06T00:00:00+00:00")
    assert snapshot.sources[0].flagged is False
    assert snapshot.sources[0].consecutive_failures == CONSECUTIVE_FAILURE_THRESHOLD - 1


def test_a_success_resets_the_consecutive_count() -> None:
    entries = [
        _entry("s1", "r1", "2026-07-01T00:00:00+00:00", FetchStatus.ERROR),
        _entry("s1", "r2", "2026-07-02T00:00:00+00:00", FetchStatus.ERROR),
        _entry("s1", "r3", "2026-07-03T00:00:00+00:00", FetchStatus.OK),
    ]
    snapshot = compute_health(entries, run_id="r3", generated_at="2026-07-03T00:00:00+00:00")
    assert snapshot.sources[0].consecutive_failures == 0
    assert snapshot.sources[0].last_status is FetchStatus.OK


def test_empty_status_also_resets_consecutive_count() -> None:
    entries = [
        _entry("s1", "r1", "2026-07-01T00:00:00+00:00", FetchStatus.ERROR),
        _entry("s1", "r2", "2026-07-02T00:00:00+00:00", FetchStatus.EMPTY),
    ]
    snapshot = compute_health(entries, run_id="r2", generated_at="2026-07-02T00:00:00+00:00")
    assert snapshot.sources[0].consecutive_failures == 0


def test_flagged_source_is_never_auto_disabled() -> None:
    # No disabled/auto-disable concept exists: flagged is purely informational,
    # the source is still retried every run.
    entries = [
        _entry("s1", f"r{n}", f"2026-07-0{n}T00:00:00+00:00", FetchStatus.ERROR)
        for n in range(1, 8)
    ]
    snapshot = compute_health(entries, run_id="r7", generated_at="2026-07-07T00:00:00+00:00")
    assert snapshot.sources[0].flagged is True
    assert not hasattr(snapshot.sources[0], "disabled")


# --- cadence-skip transparency -------------------------------------------------


def test_skipped_entries_do_not_reset_the_failure_streak() -> None:
    # A chronically-failing Reddit source: real ERRORs, then cadence SKIPs
    # interleaved and trailing. The skips must be transparent - the streak
    # still counts the real failures and the source still flags (if not quiet).
    entries = [
        _entry("s1", "r1", "2026-07-01T00:00:00+00:00", FetchStatus.ERROR, error="boom1"),
        _entry("s1", "r2", "2026-07-01T06:00:00+00:00", FetchStatus.SKIPPED),
        _entry("s1", "r3", "2026-07-01T12:00:00+00:00", FetchStatus.SKIPPED),
        _entry("s1", "r4", "2026-07-02T00:00:00+00:00", FetchStatus.ERROR, error="boom2"),
        _entry("s1", "r5", "2026-07-02T06:00:00+00:00", FetchStatus.ERROR, error="boom3"),
        _entry("s1", "r6", "2026-07-02T12:00:00+00:00", FetchStatus.SKIPPED),
    ]
    snapshot = compute_health(entries, run_id="r6", generated_at="2026-07-02T13:00:00+00:00")
    health = snapshot.sources[0]
    assert health.consecutive_failures == 3  # three real ERRORs, skips ignored
    assert health.last_status is FetchStatus.ERROR  # last *real* attempt, not the skip
    assert health.last_error == "boom3"  # not blanked by the trailing skip
    assert health.error_class is ErrorClass.OTHER
    assert health.attempts == 3  # skips excluded from the attempt count
    assert health.flagged is False  # 3 < threshold


def test_quiet_reddit_keeps_error_streak_across_skips_but_stays_unflagged() -> None:
    entries = [
        _entry("reddit-1", f"r{n}", f"2026-07-0{n}T00:00:00+00:00", FetchStatus.ERROR, error="boom")
        for n in range(1, 6)
    ] + [_entry("reddit-1", "r6", "2026-07-06T00:00:00+00:00", FetchStatus.SKIPPED)]
    snapshot = compute_health(
        entries,
        run_id="r6",
        generated_at="2026-07-06T06:00:00+00:00",
        quiet_source_ids={"reddit-1"},
    )
    health = snapshot.sources[0]
    # Auditability preserved: the 5-failure streak and last error survive the skip.
    assert health.consecutive_failures == 5
    assert health.last_status is FetchStatus.ERROR
    assert health.last_error == "boom"
    assert health.flagged is False  # quiet: never flags despite the streak


def test_multiple_sources_sorted_by_id() -> None:
    entries = [
        _entry("zzz", "r1", "2026-07-01T00:00:00+00:00", FetchStatus.OK),
        _entry("aaa", "r1", "2026-07-01T00:00:00+00:00", FetchStatus.OK),
    ]
    snapshot = compute_health(entries, run_id="r1", generated_at="2026-07-01T00:00:00+00:00")
    assert [s.source_id for s in snapshot.sources] == ["aaa", "zzz"]


def test_last_error_is_classified_on_the_snapshot() -> None:
    entries = [
        _entry("s1", "r1", "2026-07-01T00:00:00+00:00", FetchStatus.ERROR, error="s1: HTTP 403"),
    ]
    snapshot = compute_health(entries, run_id="r1", generated_at="2026-07-01T00:00:00+00:00")
    assert snapshot.sources[0].error_class is ErrorClass.HTTP_4XX


def test_non_error_status_has_no_error_class() -> None:
    entries = [_entry("s1", "r1", "2026-07-01T00:00:00+00:00", FetchStatus.OK)]
    snapshot = compute_health(entries, run_id="r1", generated_at="2026-07-01T00:00:00+00:00")
    assert snapshot.sources[0].error_class is None


# --- classify_error (error-class triage report) --------------------------------


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (None, None),
        ("ai-techpark: HTTP 403", ErrorClass.HTTP_4XX),
        ("yt-futurepedia: HTTP 500", ErrorClass.HTTP_5XX),
        ("r-openai: reddit json blocked and .rss fallback returned HTTP 429", ErrorClass.HTTP_4XX),
        (
            "aim-ai: unparseable feed: SAXParseException('syntax error')",
            ErrorClass.UNPARSEABLE,
        ),
        ("s1: malformed reddit json: Expecting value", ErrorClass.UNPARSEABLE),
        (
            "GET https://insideainews.com/feed/ failed: "
            "[SSL: SSLV3_ALERT_HANDSHAKE_FAILURE] sslv3 alert handshake failure (_ssl.c:1000)",
            ErrorClass.TLS,
        ),
        (
            "GET https://bigdataanalyticsnews.com/feed/ failed: [Errno 111] Connection refused",
            ErrorClass.CONNECTION,
        ),
        (
            "GET https://yatter.in/feed/ failed: [Errno 101] Network is unreachable",
            ErrorClass.CONNECTION,
        ),
        (
            "GET https://www.artificiallawyer.com/feed/ failed: timed out",
            ErrorClass.CONNECTION,
        ),
        ("s1: something unrecognized happened", ErrorClass.OTHER),
    ],
)
def test_classify_error(error: str | None, expected: ErrorClass | None) -> None:
    assert classify_error(error) is expected


def test_empty_history_yields_empty_snapshot() -> None:
    snapshot = compute_health([], run_id="r1", generated_at="2026-07-01T00:00:00+00:00")
    assert snapshot.sources == []


# --- quiet_source_ids (Reddit best-effort/quiet) --------------------------------
def test_quiet_source_never_flags_despite_consecutive_failures() -> None:
    entries = [
        _entry("reddit-1", f"r{n}", f"2026-07-0{n}T00:00:00+00:00", FetchStatus.ERROR, error="boom")
        for n in range(1, 6)
    ]
    snapshot = compute_health(
        entries,
        run_id="r5",
        generated_at="2026-07-06T00:00:00+00:00",
        quiet_source_ids={"reddit-1"},
    )
    health = snapshot.sources[0]
    # The count is still fully computed/visible - only the boolean is suppressed.
    assert health.consecutive_failures == 5
    assert health.flagged is False


def test_non_quiet_source_still_flags_normally_when_quiet_ids_given() -> None:
    entries = [
        _entry("rss-1", f"r{n}", f"2026-07-0{n}T00:00:00+00:00", FetchStatus.ERROR, error="boom")
        for n in range(1, 6)
    ]
    snapshot = compute_health(
        entries,
        run_id="r5",
        generated_at="2026-07-06T00:00:00+00:00",
        quiet_source_ids={"reddit-1"},  # a different source - rss-1 is unaffected
    )
    assert snapshot.sources[0].flagged is True


def test_quiet_source_ids_defaults_to_empty_and_behaves_like_before() -> None:
    entries = [
        _entry("s1", f"r{n}", f"2026-07-0{n}T00:00:00+00:00", FetchStatus.ERROR)
        for n in range(1, 6)
    ]
    snapshot = compute_health(entries, run_id="r5", generated_at="2026-07-06T00:00:00+00:00")
    assert snapshot.sources[0].flagged is True


def test_write_health_snapshot_passes_through_quiet_source_ids(tmp_path: Path) -> None:
    layout = DataLayout(tmp_path)
    entries = [
        _entry("reddit-1", f"r{n}", f"2026-07-0{n}T00:00:00+00:00", FetchStatus.ERROR)
        for n in range(1, 6)
    ]
    snapshot = write_health_snapshot(
        entries,
        layout,
        run_id="r5",
        generated_at="2026-07-06T00:00:00+00:00",
        quiet_source_ids={"reddit-1"},
    )
    assert snapshot.sources[0].flagged is False
    written = HealthSnapshot.model_validate_json(layout.health_file.read_text(encoding="utf-8"))
    assert written.sources[0].flagged is False


def test_write_health_snapshot_writes_json(tmp_path: Path) -> None:
    layout = DataLayout(tmp_path)
    entries = [
        _entry("s1", "r1", "2026-07-01T00:00:00+00:00", FetchStatus.OK),
        _entry("s2", "r1", "2026-07-01T00:00:00+00:00", FetchStatus.ERROR, error="dead"),
    ]
    write_health_snapshot(entries, layout, run_id="r1", generated_at="2026-07-01T00:05:00+00:00")

    assert layout.health_file.exists()
    written = HealthSnapshot.model_validate_json(layout.health_file.read_text(encoding="utf-8"))
    assert written.run_id == "r1"
    assert written.generated_at == "2026-07-01T00:05:00+00:00"
    by_id = {s.source_id: s for s in written.sources}
    assert by_id["s1"].last_status is FetchStatus.OK
    assert by_id["s1"].consecutive_failures == 0
    assert by_id["s2"].last_status is FetchStatus.ERROR
    assert by_id["s2"].last_error == "dead"
    assert by_id["s2"].consecutive_failures == 1
    assert by_id["s2"].flagged is False


def test_drilldown_statistics_ignore_skipped_and_track_items_and_urls() -> None:
    entries = [
        FetchLogEntry(
            source_id="s1",
            run_id="r0",
            started_at="2026-07-01T00:00:00+00:00",
            status=FetchStatus.SKIPPED,
        ),
        FetchLogEntry(
            source_id="s1",
            run_id="r1",
            started_at="2026-07-01T01:00:00+00:00",
            status=FetchStatus.OK,
            items_new=2,
        ),
        FetchLogEntry(
            source_id="s1",
            run_id="r2",
            started_at="2026-07-02T01:00:00+00:00",
            status=FetchStatus.EMPTY,
            items_new=0,
        ),
        FetchLogEntry(
            source_id="s1",
            run_id="r3",
            started_at="2026-07-03T01:00:00+00:00",
            status=FetchStatus.ERROR,
            error="HTTP 500",
        ),
        FetchLogEntry(
            source_id="s1",
            run_id="r4",
            started_at="2026-07-04T01:00:00+00:00",
            status=FetchStatus.OK,
            items_new=4,
            resolved_url="https://example.com/alt.xml",
        ),
    ]
    health = compute_health(entries, run_id="r4", generated_at="2026-07-04T02:00:00+00:00").sources[
        0
    ]
    assert health.attempts == 4
    assert health.ok_attempts == 2
    assert health.empty_attempts == 1
    assert health.failed_attempts == 1
    assert health.successful_attempts == 3
    assert health.last_successful_at == "2026-07-04T01:00:00+00:00"
    assert health.last_failed_at == "2026-07-03T01:00:00+00:00"
    assert health.last_error == "HTTP 500"
    assert health.error_class is ErrorClass.HTTP_5XX
    assert health.consecutive_failures == 0
    assert health.total_items_new == 6
    assert health.latest_items_new == 4
    assert health.last_resolved_url == "https://example.com/alt.xml"


def test_only_skipped_history_yields_no_source_rollup() -> None:
    entries = [_entry("s1", "r1", "2026-07-01T00:00:00+00:00", FetchStatus.SKIPPED)]
    snapshot = compute_health(entries, run_id="r1", generated_at="2026-07-01T00:00:00+00:00")
    assert snapshot.sources == []


def test_old_health_snapshot_without_new_fields_still_loads() -> None:
    raw = {
        "run_id": "r1",
        "generated_at": "2026-07-01T00:00:00+00:00",
        "sources": [
            {
                "source_id": "s1",
                "attempts": 1,
                "last_status": "ok",
                "last_started_at": "2026-07-01T00:00:00+00:00",
                "consecutive_failures": 0,
                "flagged": False,
            }
        ],
    }
    snapshot = HealthSnapshot.model_validate(raw)
    assert snapshot.sources[0].successful_attempts == 0
    assert snapshot.sources[0].total_items_new == 0
