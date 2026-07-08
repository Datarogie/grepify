"""GRP-14: simhash content_hash, near-dup grouping, and ingest idempotency."""

from __future__ import annotations

from pathlib import Path

import pytest

from grepify.ingest import (
    RawItem,
    compute_content_hash,
    group_near_duplicates,
    hamming_distance,
    normalize_batch,
)
from grepify.models import SourceKind
from grepify.repository import JsonlSqliteRepository
from tests.conftest import make_item, make_source

_FETCHED = "2026-07-08T12:00:00+00:00"


# --- content_hash (simhash) -------------------------------------------------


def test_content_hash_is_deterministic_and_16_hex() -> None:
    h1 = compute_content_hash("OpenAI ships GPT-5 to developers")
    h2 = compute_content_hash("OpenAI ships GPT-5 to developers")
    assert h1 == h2
    assert len(h1) == 16
    int(h1, 16)  # valid hex


def test_content_hash_ignores_case_punctuation_and_html() -> None:
    a = compute_content_hash("OpenAI ships GPT-5!")
    b = compute_content_hash("<b>openai</b> ships gpt 5")
    assert a == b


def test_empty_title_hashes_to_zero() -> None:
    assert compute_content_hash("") == "0" * 16
    assert compute_content_hash("!!! --- ...") == "0" * 16


def test_near_dup_titles_are_close_distinct_titles_are_far() -> None:
    base = compute_content_hash("Anthropic releases Claude with new reasoning tools")
    near = compute_content_hash("Anthropic releases Claude with new reasoning features")
    far = compute_content_hash("Weekly recipe roundup: twelve summer salads to try")
    d_near = hamming_distance(base, near)
    d_far = hamming_distance(base, far)
    # A minor headline edit stays within the default group threshold; an unrelated
    # headline is far outside it, with a comfortable margin between the two.
    assert d_near <= 12
    assert d_far >= 20
    assert d_far - d_near >= 12


def test_hamming_distance_identity_and_width_guard() -> None:
    h = compute_content_hash("something")
    assert hamming_distance(h, h) == 0
    with pytest.raises(ValueError, match="width mismatch"):
        hamming_distance("00", "0000")


# --- group_near_duplicates --------------------------------------------------


def test_group_near_duplicates_clusters_reposts() -> None:
    title = "Google announces Gemini 3 with agentic capabilities today"
    near = "Google announces Gemini 3 with agentic capabilities now"
    other = "PyTorch 3.0 released with compiler improvements"
    items = [
        make_item(
            "i1", content_hash=compute_content_hash(title), published_at="2026-07-08T01:00:00+00:00"
        ),
        make_item(
            "i2", content_hash=compute_content_hash(near), published_at="2026-07-08T02:00:00+00:00"
        ),
        make_item(
            "i3", content_hash=compute_content_hash(other), published_at="2026-07-08T03:00:00+00:00"
        ),
    ]
    groups = group_near_duplicates(items)
    sizes = sorted(len(g) for g in groups)
    assert sizes == [1, 2]
    dup_group = next(g for g in groups if len(g) == 2)
    assert {it.item_id for it in dup_group} == {"i1", "i2"}


def test_group_near_duplicates_is_deterministic_and_ordered() -> None:
    items = [
        make_item(
            "z",
            content_hash=compute_content_hash("alpha beta"),
            published_at="2026-07-08T03:00:00+00:00",
        ),
        make_item(
            "a",
            content_hash=compute_content_hash("gamma delta"),
            published_at="2026-07-08T01:00:00+00:00",
        ),
    ]
    g1 = group_near_duplicates(items)
    g2 = group_near_duplicates(list(reversed(items)))
    # Ordered by (published_at, item_id): the earlier-published item leads.
    assert [c[0].item_id for c in g1] == ["a", "z"]
    assert g1 == g2


def test_all_singletons_when_no_near_dups() -> None:
    titles = [
        "OpenAI ships a new developer platform update",
        "Rust foundation announces quarterly grant recipients",
        "Best hiking trails in the Rocky Mountains this summer",
        "Quantum error correction milestone reported by researchers",
    ]
    items = [make_item(f"i{n}", content_hash=compute_content_hash(t)) for n, t in enumerate(titles)]
    groups = group_near_duplicates(items)
    assert all(len(g) == 1 for g in groups)
    assert len(groups) == 4


# --- idempotency: normalize -> add_items twice == zero new rows (GRP-14 AC) --


def _feed() -> list[RawItem]:
    return [
        RawItem(
            url="https://blog.example.com/a?utm_source=rss",
            title="First post",
            external_id="guid-a",
        ),
        RawItem(url="https://blog.example.com/b", title="Second post", external_id="guid-b"),
        RawItem(url="https://blog.example.com/c", title="Third post"),  # no guid -> url identity
    ]


def test_double_run_writes_zero_new_rows(tmp_path: Path) -> None:
    repo = JsonlSqliteRepository(tmp_path)
    source = make_source("blog", kind=SourceKind.RSS)

    first = normalize_batch(_feed(), source, fetched_at="2026-07-08T08:00:00+00:00")
    assert repo.add_items(first) == 3

    # Re-fetch later: same entries, a fresh fetched_at, tracking param on url A.
    second = normalize_batch(_feed(), source, fetched_at="2026-07-09T08:00:00+00:00")
    assert [i.item_id for i in second] == [i.item_id for i in first]  # stable identity
    assert repo.add_items(second) == 0  # zero new rows

    repo.rebuild_cache()
    assert repo.count_items() == 3
    repo.close()


def test_same_guid_across_sources_dedups_to_one_item(tmp_path: Path) -> None:
    repo = JsonlSqliteRepository(tmp_path)
    raw = RawItem(url="https://a.com/x", title="Cross-posted", external_id="shared-guid")
    other = RawItem(url="https://b.com/y", title="Cross-posted", external_id="shared-guid")

    a = normalize_batch([raw], make_source("s1"), fetched_at=_FETCHED)
    b = normalize_batch([other], make_source("s2"), fetched_at=_FETCHED)
    assert a[0].item_id == b[0].item_id  # (kind, external_id) identity -> one item

    assert repo.add_items(a) == 1
    assert repo.add_items(b) == 0  # unique (kind, external_id) upheld by construction
    repo.rebuild_cache()
    assert repo.count_items() == 1
    repo.close()
