"""T5: ``doctor`` report - per-source status + error-class triage view (GRP-30)."""

from __future__ import annotations

from grepify.doctor import build_doctor_report, format_doctor_report
from grepify.health import ErrorClass, HealthSnapshot, SourceHealth, compute_health
from grepify.models import FetchLogEntry, FetchStatus, SourceKind
from tests.conftest import make_source


def _fetch_entry(source_id: str, status: FetchStatus, *, error: str | None = None) -> FetchLogEntry:
    return FetchLogEntry(
        source_id=source_id,
        run_id="r1",
        started_at="2026-07-11T00:00:00+00:00",
        status=status,
        error=error,
    )


def test_report_joins_config_and_health_by_source_id() -> None:
    sources = [
        make_source("dead-feed", kind=SourceKind.RSS),
        make_source("healthy-feed", kind=SourceKind.RSS),
    ]
    snapshot = compute_health(
        [
            _fetch_entry("dead-feed", FetchStatus.ERROR, error="dead-feed: HTTP 403"),
            _fetch_entry("healthy-feed", FetchStatus.OK),
        ],
        run_id="r1",
        generated_at="2026-07-11T00:00:00+00:00",
    )

    rows = build_doctor_report(sources, snapshot)

    by_id = {row.source_id: row for row in rows}
    assert by_id["dead-feed"].last_status is FetchStatus.ERROR
    assert by_id["dead-feed"].error_class is ErrorClass.HTTP_4XX
    assert by_id["healthy-feed"].last_status is FetchStatus.OK
    assert by_id["healthy-feed"].error_class is None


def test_report_sorted_by_source_id_regardless_of_input_order() -> None:
    sources = [make_source("zzz"), make_source("aaa")]
    snapshot = HealthSnapshot(run_id="r1", generated_at="2026-07-11T00:00:00+00:00", sources=[])
    rows = build_doctor_report(sources, snapshot)
    assert [row.source_id for row in rows] == ["aaa", "zzz"]


def test_source_never_fetched_still_gets_a_row() -> None:
    sources = [make_source("brand-new")]
    snapshot = HealthSnapshot(run_id="r1", generated_at="2026-07-11T00:00:00+00:00", sources=[])
    rows = build_doctor_report(sources, snapshot)
    assert len(rows) == 1
    assert rows[0].last_status is None
    assert rows[0].error_class is None
    assert rows[0].consecutive_failures == 0
    assert rows[0].flagged is False


def test_report_reflects_the_current_enabled_flag() -> None:
    source = make_source("disabled-feed").model_copy(update={"enabled": False})
    snapshot = HealthSnapshot(run_id="r1", generated_at="2026-07-11T00:00:00+00:00", sources=[])
    rows = build_doctor_report([source], snapshot)
    assert rows[0].enabled is False


def test_flagged_reflects_five_or_more_consecutive_errors() -> None:
    sources = [make_source("s1")]
    snapshot = compute_health(
        [_fetch_entry("s1", FetchStatus.ERROR, error="s1: HTTP 500") for _ in range(5)],
        run_id="r1",
        generated_at="2026-07-11T00:00:00+00:00",
    )
    rows = build_doctor_report(sources, snapshot)
    assert rows[0].flagged is True
    assert rows[0].consecutive_failures == 5


# --- format_doctor_report -----------------------------------------------------


def test_format_empty_report() -> None:
    assert format_doctor_report([]) == "no sources configured"


def test_format_includes_summary_and_per_source_rows() -> None:
    sources = [make_source("dead-feed"), make_source("healthy-feed")]
    snapshot = compute_health(
        [
            _fetch_entry("dead-feed", FetchStatus.ERROR, error="dead-feed: HTTP 403"),
            _fetch_entry("healthy-feed", FetchStatus.OK),
        ],
        run_id="r1",
        generated_at="2026-07-11T00:00:00+00:00",
    )
    report = format_doctor_report(build_doctor_report(sources, snapshot))

    assert "2 sources, 1 last-run error, 0 flagged" in report
    assert "dead-feed" in report
    assert "http_4xx" in report
    assert "healthy-feed" in report
    assert "ok" in report


def test_format_is_deterministic_across_calls() -> None:
    sources = [make_source("b"), make_source("a")]
    snapshot = HealthSnapshot(run_id="r1", generated_at="2026-07-11T00:00:00+00:00", sources=[])
    rows = build_doctor_report(sources, snapshot)
    assert format_doctor_report(rows) == format_doctor_report(rows)


def test_source_health_carries_error_class_used_by_report() -> None:
    # Sanity: SourceHealth (health.py) already exposes error_class - the report
    # just forwards it, it does not reclassify.
    health = SourceHealth(
        source_id="s1",
        attempts=1,
        last_status=FetchStatus.ERROR,
        last_started_at="2026-07-11T00:00:00+00:00",
        last_error="s1: unparseable feed: boom",
        error_class=ErrorClass.UNPARSEABLE,
        consecutive_failures=1,
        flagged=False,
    )
    assert health.error_class is ErrorClass.UNPARSEABLE
