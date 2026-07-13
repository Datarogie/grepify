"""GRP-63: data-branch size guardrail - threshold math + directory summation.

Fixture directory trees use tiny warn/fail thresholds (bytes, not real MB) so
the under/warn-band/over cases exercise real files on disk without writing
hundreds of megabytes in a test run.
"""

from __future__ import annotations

from pathlib import Path

from grepify.datasize import (
    DEFAULT_FAIL_BYTES,
    DEFAULT_WARN_BYTES,
    SizeLevel,
    classify_size,
    compute_data_size,
    format_report,
)
from grepify.paths import DataLayout

# --- classify_size: pure threshold math --------------------------------------


def test_classify_under_warn_is_ok() -> None:
    assert classify_size(9, warn_bytes=10, fail_bytes=20) is SizeLevel.OK


def test_classify_at_warn_boundary_is_warn() -> None:
    assert classify_size(10, warn_bytes=10, fail_bytes=20) is SizeLevel.WARN


def test_classify_in_warn_band_is_warn() -> None:
    assert classify_size(15, warn_bytes=10, fail_bytes=20) is SizeLevel.WARN


def test_classify_at_fail_boundary_is_fail() -> None:
    assert classify_size(20, warn_bytes=10, fail_bytes=20) is SizeLevel.FAIL


def test_classify_over_fail_is_fail() -> None:
    assert classify_size(1_000, warn_bytes=10, fail_bytes=20) is SizeLevel.FAIL


def test_classify_zero_is_ok() -> None:
    assert classify_size(0, warn_bytes=10, fail_bytes=20) is SizeLevel.OK


def test_default_thresholds_are_100mb_and_200mb() -> None:
    assert DEFAULT_WARN_BYTES == 100 * 1024 * 1024
    assert DEFAULT_FAIL_BYTES == 200 * 1024 * 1024


# --- compute_data_size: fixture directory trees ------------------------------


def _write(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def test_missing_data_root_is_zero_bytes(tmp_path: Path) -> None:
    layout = DataLayout(tmp_path / "does-not-exist")
    report = compute_data_size(layout, warn_bytes=10, fail_bytes=20)
    assert report.total_bytes == 0
    assert report.level is SizeLevel.OK


def test_under_warn_band(tmp_path: Path) -> None:
    layout = DataLayout(tmp_path)
    _write(layout.items_dir / "2026" / "07" / "13.jsonl", 3)
    _write(layout.keywords_dir / "2026" / "07" / "13.jsonl", 2)

    report = compute_data_size(layout, warn_bytes=10, fail_bytes=20)
    assert report.total_bytes == 5
    assert report.level is SizeLevel.OK


def test_warn_band(tmp_path: Path) -> None:
    layout = DataLayout(tmp_path)
    _write(layout.items_dir / "2026" / "07" / "13.jsonl", 8)
    _write(layout.keywords_dir / "2026" / "07" / "13.jsonl", 4)
    _write(layout.transcripts_dir / "item-1.txt.gz", 2)

    report = compute_data_size(layout, warn_bytes=10, fail_bytes=20)
    assert report.total_bytes == 14
    assert report.level is SizeLevel.WARN


def test_over_fail_threshold(tmp_path: Path) -> None:
    layout = DataLayout(tmp_path)
    _write(layout.items_dir / "2026" / "07" / "13.jsonl", 15)
    _write(layout.keywords_dir / "2026" / "07" / "13.jsonl", 10)

    report = compute_data_size(layout, warn_bytes=10, fail_bytes=20)
    assert report.total_bytes == 25
    assert report.level is SizeLevel.FAIL


def test_only_items_keywords_transcripts_are_summed(tmp_path: Path) -> None:
    """Other data-root directories (logs, digests, runs, health.json, the
    gitignored cache) must not count towards the guardrail (issue #62 scope)."""
    layout = DataLayout(tmp_path)
    _write(layout.items_dir / "2026" / "07" / "13.jsonl", 5)
    _write(layout.fetch_log_dir / "2026" / "07" / "13.jsonl", 1_000)
    _write(layout.llm_log_dir / "2026" / "07" / "13.jsonl", 1_000)
    _write(layout.digests_dir / "d1.json", 1_000)
    _write(layout.runs_dir / "r1.json", 1_000)
    _write(layout.cache_db, 1_000)
    layout.health_file.write_text("{}", encoding="utf-8")

    report = compute_data_size(layout, warn_bytes=10, fail_bytes=20)
    assert report.total_bytes == 5
    assert report.items_bytes == 5
    assert report.keywords_bytes == 0
    assert report.transcripts_bytes == 0


def test_per_directory_breakdown_is_independent(tmp_path: Path) -> None:
    layout = DataLayout(tmp_path)
    _write(layout.items_dir / "2026" / "07" / "13.jsonl", 3)
    _write(layout.keywords_dir / "2026" / "07" / "13.jsonl", 4)
    _write(layout.transcripts_dir / "item-1.txt.gz", 5)

    report = compute_data_size(layout)
    assert report.items_bytes == 3
    assert report.keywords_bytes == 4
    assert report.transcripts_bytes == 5
    assert report.total_bytes == 12


# --- format_report ------------------------------------------------------------


def test_format_ok_has_no_warn_or_fail_prefix(tmp_path: Path) -> None:
    layout = DataLayout(tmp_path)
    report = compute_data_size(layout, warn_bytes=10, fail_bytes=20)
    line = format_report(report)
    assert line.startswith("data size:")
    assert "WARN" not in line
    assert "FAIL" not in line


def test_format_warn_line_is_prefixed(tmp_path: Path) -> None:
    layout = DataLayout(tmp_path)
    _write(layout.items_dir / "2026" / "07" / "13.jsonl", 15)

    report = compute_data_size(layout, warn_bytes=10, fail_bytes=20)
    line = format_report(report)
    assert line.startswith("WARN: data size:")


def test_format_fail_line_is_prefixed(tmp_path: Path) -> None:
    layout = DataLayout(tmp_path)
    _write(layout.items_dir / "2026" / "07" / "13.jsonl", 25)

    report = compute_data_size(layout, warn_bytes=10, fail_bytes=20)
    line = format_report(report)
    assert line.startswith("FAIL: data size:")


def test_format_includes_per_directory_breakdown(tmp_path: Path) -> None:
    layout = DataLayout(tmp_path)
    _write(layout.items_dir / "2026" / "07" / "13.jsonl", 1024 * 1024)
    _write(layout.keywords_dir / "2026" / "07" / "13.jsonl", 2 * 1024 * 1024)
    _write(layout.transcripts_dir / "item-1.txt.gz", 3 * 1024 * 1024)

    report = compute_data_size(layout)
    line = format_report(report)
    assert "items 1.0 MB" in line
    assert "keywords 2.0 MB" in line
    assert "transcripts 3.0 MB" in line
    assert "6.0 MB" in line


def test_format_is_deterministic(tmp_path: Path) -> None:
    layout = DataLayout(tmp_path)
    _write(layout.items_dir / "2026" / "07" / "13.jsonl", 5)
    report = compute_data_size(layout)
    assert format_report(report) == format_report(report)
