"""Storage layer tests (GRP-03): idempotency + deterministic rebuild."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from grepify.errors import RepositoryError
from grepify.models import (
    Digest,
    DigestKind,
    FetchLogEntry,
    FetchStatus,
    Source,
    SourceGroup,
    SourceKind,
)
from grepify.repository import JsonlSqliteRepository
from tests.conftest import make_item, make_keyword


def _rows(db: Path, sql: str) -> list[tuple[object, ...]]:
    conn = sqlite3.connect(db)
    try:
        return list(conn.execute(sql))
    finally:
        conn.close()


def test_add_items_is_idempotent(tmp_path: Path) -> None:
    repo = JsonlSqliteRepository(tmp_path)
    items = [make_item(f"i{n}") for n in range(3)]

    assert repo.add_items(items) == 3
    assert repo.add_items(items) == 0  # second run writes nothing
    assert repo.add_items([make_item("i0"), make_item("i9")]) == 1  # only the new one

    repo.rebuild_cache()
    assert repo.count_items() == 4
    repo.close()


def test_items_partitioned_by_published_date(tmp_path: Path) -> None:
    repo = JsonlSqliteRepository(tmp_path)
    repo.add_items([make_item("a", published_at="2026-07-07T10:00:00+00:00")])
    assert (tmp_path / "items" / "2026" / "07" / "07.jsonl").exists()


def test_rebuild_is_deterministic(tmp_path: Path) -> None:
    repo = JsonlSqliteRepository(tmp_path)
    repo.add_items([make_item(f"i{n}") for n in range(5)])
    repo.add_item_keywords([make_keyword(f"i{n}", f"kw{n}") for n in range(5)])

    # No `order by`: rely on insertion (rowid) order so the assertion catches a
    # nondeterministic insert order, not just same-set membership.
    repo.rebuild_cache()
    first = _rows(tmp_path / "grepify.db", "select item_id, title from items")
    repo.rebuild_cache()
    second = _rows(tmp_path / "grepify.db", "select item_id, title from items")

    assert first == second
    assert [r[0] for r in first] == [f"i{n}" for n in range(5)]
    assert len(first) == 5
    repo.close()


def test_keywords_idempotent_on_composite_key(tmp_path: Path) -> None:
    repo = JsonlSqliteRepository(tmp_path)
    kws = [make_keyword("i1", "genai"), make_keyword("i1", "llm", rank=2)]
    assert repo.add_item_keywords(kws) == 2
    assert repo.add_item_keywords(kws) == 0
    repo.rebuild_cache()
    assert repo.count_item_keywords() == 2
    repo.close()


def test_load_config_projects_into_cache(tmp_path: Path) -> None:
    repo = JsonlSqliteRepository(tmp_path)
    group = SourceGroup(group_id="g1", name="G1", category="ai")
    source = Source(
        source_id="s1",
        name="S1",
        kind=SourceKind.RSS,
        url="https://example.com/feed",
        url_hash="deadbeef",
        group_id="g1",
        added_at="2026-07-07T00:00:00+00:00",
    )
    repo.load_config([group], [source])
    repo.rebuild_cache()

    assert _rows(tmp_path / "grepify.db", "select group_id from source_groups") == [("g1",)]
    assert _rows(tmp_path / "grepify.db", "select source_id, enabled from sources") == [("s1", 1)]
    repo.close()


def test_digest_and_fetch_log_round_trip(tmp_path: Path) -> None:
    repo = JsonlSqliteRepository(tmp_path)
    repo.add_digest(
        Digest(
            digest_id="daily-ai-2026-07-07",
            kind=DigestKind.DAILY,
            category="ai",
            period_start="2026-07-06T00:00:00+00:00",
            period_end="2026-07-07T00:00:00+00:00",
            title="Daily AI",
            body_md="body",
            top_keywords="[]",
            model="test-model",
            created_at="2026-07-07T13:00:00+00:00",
        )
    )
    repo.log_fetch(
        FetchLogEntry(
            source_id="s1",
            run_id="run-1",
            started_at="2026-07-07T09:00:00+00:00",
            status=FetchStatus.OK,
            items_new=3,
        )
    )
    repo.rebuild_cache()
    assert _rows(tmp_path / "grepify.db", "select count(*) from digests") == [(1,)]
    assert _rows(tmp_path / "grepify.db", "select items_new from fetch_log") == [(3,)]
    repo.close()


def test_count_before_rebuild_raises(tmp_path: Path) -> None:
    repo = JsonlSqliteRepository(tmp_path)
    with pytest.raises(RepositoryError):
        repo.count_items()


def test_corrupt_truth_file_raises(tmp_path: Path) -> None:
    repo = JsonlSqliteRepository(tmp_path)
    repo.add_items([make_item("a")])
    bad = tmp_path / "items" / "2026" / "07" / "07.jsonl"
    bad.write_text("{not json}\n", encoding="utf-8")
    with pytest.raises(RepositoryError):
        repo.existing_item_ids()
