"""GRP-60 renormalize data-remediation core (grepify.maintenance).

Exercises the pure clean-and-drop pass against a real JSONL+SQLite repository
with a dirty-summary fixture: it must rewrite only the summaries the current
cleaner changes, drop exactly those items' keyword rows, leave clean items and
their rows untouched, and be idempotent on a second run.
"""

from __future__ import annotations

from pathlib import Path

from grepify.maintenance import RenormalizeResult, renormalize_summaries
from grepify.models import Item
from grepify.repository import JsonlSqliteRepository
from tests.conftest import make_item, make_keyword


def _with_summary(item_id: str, summary: str | None) -> Item:
    return make_item(item_id).model_copy(update={"summary": summary})


def test_renormalize_cleans_dirty_summaries_and_drops_their_keywords(tmp_path: Path) -> None:
    repo = JsonlSqliteRepository(tmp_path)
    repo.add_items(
        [_with_summary("dirty", '<div class="x">Real news</div>'), _with_summary("clean", "plain")]
    )
    # "div" is exactly the YAKE-fallback noise the remediation exists to kill.
    repo.add_item_keywords([make_keyword("dirty", "div"), make_keyword("clean", "genai")])

    result = renormalize_summaries(repo)

    assert result.items_scanned == 2
    assert result.items_rewritten == 1
    assert result.keyword_rows_deleted == 1
    assert result.changed_item_ids == ["dirty"]

    by_id = {i.item_id: i for i in repo.iter_items()}
    assert by_id["dirty"].summary == "Real news"  # markup stripped
    assert by_id["clean"].summary == "plain"  # untouched

    remaining = [(k.item_id, k.keyword) for k in repo.iter_item_keywords()]
    assert remaining == [("clean", "genai")]  # dirty's stale row gone, clean's survives
    repo.close()


def test_renormalize_is_idempotent(tmp_path: Path) -> None:
    repo = JsonlSqliteRepository(tmp_path)
    repo.add_items([_with_summary("d", "<p>Hello &amp; welcome</p>")])

    first = renormalize_summaries(repo)
    assert first.items_rewritten == 1

    second = renormalize_summaries(repo)
    assert second == RenormalizeResult(items_scanned=1, items_rewritten=0, keyword_rows_deleted=0)
    assert [i.summary for i in repo.iter_items()] == ["Hello & welcome"]
    repo.close()


def test_renormalize_skips_null_summary(tmp_path: Path) -> None:
    repo = JsonlSqliteRepository(tmp_path)
    repo.add_items([_with_summary("n", None)])

    result = renormalize_summaries(repo)

    assert result.items_scanned == 1
    assert result.items_rewritten == 0
    assert result.changed_item_ids == []
    repo.close()


def test_renormalize_empty_corpus_is_noop(tmp_path: Path) -> None:
    repo = JsonlSqliteRepository(tmp_path)
    assert renormalize_summaries(repo) == RenormalizeResult(0, 0, 0)
    repo.close()
